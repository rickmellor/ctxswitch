"""Agents messaging other sessions as themselves: who the MCP server says is sending, the hub's checks, the header
the collector delivers it with, and the turn recorded as a peer's."""
import json
from pathlib import Path

import httpx
from starlette.testclient import TestClient
from test_messages import FakeSocket, Hub2, _collector

from ctxswitch.client.hubclient import HubClient
from ctxswitch.client.mcp_server import Server, own_session
from ctxswitch.collector import deliver
from ctxswitch.common.config import Config
from ctxswitch.common.schema import Liveness
from ctxswitch.hub import store as store_mod
from ctxswitch.hub.api import Hub
from ctxswitch.hub.store import Store

H = {"Authorization": "Bearer t"}
A, B, P = "laptop/claude/s1", "devbox/claude/s2", "laptop/pi/p1"
T0 = "2026-10-29T10:00:00Z"


def _store(tmp_path):
    store = Store(tmp_path / "hub.db")
    for key, title in ((A, "docs work"), (B, "api work"), (P, "pi side")):
        h, harness, sid = key.split("/")
        store.upsert_session({"key": key, "host_id": h, "harness": harness, "session_id": sid, "title": title, "cwd": "/r", "last_activity": T0})
    for h, keys in (("laptop", (A, P)), ("devbox", (B,))):                     # a snapshot per host: all its live sessions
        store.set_liveness(h, [{"key": k, "status": "idle", "status_since": T0, "pid": 1, "socket": "/x"} for k in keys])
    return store


def _turn(idx, user, agent="", origin="peer", asks=(), complete=False, t=T0):
    return {"idx": idx, "started_at": t, "ended_at": t, "user_text": user, "user_origin": origin, "assistant_text": agent,
            "tally": {}, "asks": list(asks), "events": [], "complete": complete, "offset_end": idx * 100 + 10}


def _post(c, to, **body):
    return c.post(f"/v1/sessions/{to}/messages", headers=H, json={"body": "hi", **body})


def test_hub_checks_the_sender_and_keeps_two_agents_from_looping(tmp_path, monkeypatch):
    store = _store(tmp_path)
    c = TestClient(Hub(store, token="t").app())
    assert _post(c, B, from_key="nope/claude/x").status_code == 400
    assert _post(c, B, from_key=B).json()["error"] == "a session can't message itself"
    assert _post(c, B, from_key=A, answers="0:plain").status_code == 400      # only the operator answers its questions
    mid = _post(c, B, from_key=A).json()["id"]
    assert store.messages(B)[0]["from_key"] == A
    assert _post(c, B, from_key=A).status_code == 409                          # one in flight per recipient
    assert _post(c, P, from_key=A).status_code == 200                          # another recipient is fine
    assert _post(c, B, body="mine").status_code == 200                          # and the operator isn't limited
    store.set_message_status(mid, "devbox", "sent")
    assert _post(c, B, from_key=A).status_code == 409                          # sent is still in flight
    store.cancel_message(_post(c, A, from_key=B).json()["id"])
    monkeypatch.setattr(store_mod, "AGENT_PER_HOUR", 2)
    store.db.execute("UPDATE messages SET status='replied' WHERE from_key=?", (A,))           # none in flight now
    r = _post(c, B, from_key=A)
    assert r.status_code == 429 and "has sent 2 messages" in r.json()["error"]  # A sent 2 this hour
    assert _post(c, P, from_key=B).status_code == 200                           # B's cancelled one doesn't count
    assert _post(c, A, from_key=B).status_code == 200
    assert _post(c, B, from_key=P).status_code == 200 and _post(c, A, from_key=B).status_code in (409, 429)


def test_outbox_gives_agent_messages_only_to_collectors_that_know_the_header(tmp_path):
    store = _store(tmp_path)
    mine = store.add_message(A, "from you", "laptop")
    theirs = store.add_message(A, "from B", "devbox", from_key=B)
    assert [m["id"] for m in store.outbox("laptop")] == [mine]                 # an older collector: yours only
    got = {m["id"]: m for m in store.outbox("laptop", agents=True)}
    assert (got[theirs]["from_key"], got[theirs]["from_title"]) == (B, "api work") and got[mine]["from_key"] is None
    c = TestClient(Hub(store, token="t").app())
    assert len(c.get("/v1/outbox", params={"host": "laptop"}, headers=H).json()["messages"]) == 1
    assert len(c.get("/v1/outbox", params={"host": "laptop", "agents": "1"}, headers=H).json()["messages"]) == 2


def test_the_delivered_turn_is_the_agents_not_yours(tmp_path):
    store = _store(tmp_path)
    q = {"ref": "0:plain", "kind": "plain", "text": "Docs too?", "answered": False}
    store.upsert_turns(A, [_turn(0, "go", "Docs too?", origin="human", asks=[q], complete=True)], None)
    store.apply_extraction(A, _turn(0, "go"), [], [], [{"text": "Want a PR?"}], None)
    mid = store.add_message(A, "yes, docs   too", "devbox", from_key=B)
    store.set_message_status(mid, "laptop", "sent")
    wrapped = deliver.agent_header(B, "api work") + "yes, docs too"
    store.upsert_turns(A, [_turn(0, "go", "Docs too?", origin="human", complete=True,
                                 asks=[q | {"answered": True, "answer": wrapped, "answered_at": "2026-10-29T10:05:00Z"}]),
                           _turn(1, wrapped, "ok", origin="human", complete=True, t="2026-10-29T10:05:00Z")], None)  # pi says human
    s = store.session(A)
    t1 = s["turns"][-1]
    assert (t1["user_origin"], t1["from_key"], t1["user_text"]) == ("peer", B, "yes, docs   too")
    a = {x["ref"]: x for x in s["asks"]}
    assert a["0:plain"]["answer"] == f"from {B}: yes, docs too"               # who answered, in the decision
    assert a["llm:0:0"]["answered"] == 0                                        # an agent's message isn't your prompt
    assert s["messages"][0]["status"] == "replied"
    assert [r["text"] for r in store.search("docs", kinds=("prompt",))] == []  # nor one of your prompts


def test_header_names_the_sender_for_both_harnesses():
    h = deliver.agent_header(B, "api   work\nnow")
    assert h.startswith("Message from another agent session, not from the operator") and deliver.HEADER not in h
    assert f'From: session {B} "api work now" on host devbox' in h and f'session "{B}"' in h
    for harness in ("claude", "pi"):
        c = json.loads(deliver.line(harness, "s", "hi", 3, from_key=B, from_title="api work"))["message"]["content"]
        assert c == deliver.agent_header(B, "api work") + "hi"
    assert "From: session devbox/pi/x on host devbox\n" in deliver.agent_header("devbox/pi/x", "")


async def test_collector_delivers_an_agent_message_with_its_header(tmp_path, monkeypatch):
    col = _collector(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Hub2().handler)) as client, FakeSocket(tmp_path / "p.sock", {"ok": True}) as ps:
        col.live = {P: Liveness(P, "idle", socket=ps.path)}
        assert await col.deliver_one(client, {"id": 4, "key": P, "body": "hello", "from_key": B, "from_title": "api work"})
        (line,) = await ps.got(1)
        assert line["message"]["content"] == deliver.agent_header(B, "api work") + "hello"


def _proc(root: Path, pid: int, ppid: int, start: str = "100"):
    rest = ["S", str(ppid)] + ["0"] * 17 + [start, "0"]                # rest[19] = start time
    (root / str(pid)).mkdir(parents=True)
    (root / str(pid) / "stat").write_text(f"{pid} (some (odd) name) {' '.join(rest)}\n")


def test_own_session_is_the_nearest_registered_ancestor(tmp_path, monkeypatch):
    proc, reg, rt = tmp_path / "proc", tmp_path / "sessions", tmp_path / "rt"
    reg.mkdir(); (rt / "ctxswitch").mkdir(parents=True)
    monkeypatch.setenv("CTXSWITCH_RUNTIME", str(rt))
    cfg = Config(); cfg.collector.host_id = "laptop"; cfg.collector.claude_sessions_dir = str(reg)
    _proc(proc, 10, 1); _proc(proc, 20, 10); _proc(proc, 30, 20); _proc(proc, 40, 30)      # claude 10 > sh 20 > ... > us 40
    (reg / "10.json").write_text(json.dumps({"pid": 10, "sessionId": "s1", "procStart": "100", "status": "busy"}))
    assert own_session(cfg, str(proc), pid=40) == A
    (reg / "10.json").write_text(json.dumps({"pid": 10, "sessionId": "s1", "procStart": "999"}))   # a reused pid
    assert own_session(cfg, str(proc), pid=40) is None
    (rt / "ctxswitch" / "pi-20.json").write_text(json.dumps({"pid": 20, "procStart": "100", "sessionId": "p1"}))
    assert own_session(cfg, str(proc), pid=40) == P                           # pi, nearer
    (reg / "30.json").write_text(json.dumps({"pid": 30, "sessionId": "s9", "procStart": "100"}))
    assert own_session(cfg, str(proc), pid=40) == "laptop/claude/s9"          # the nearest wins
    assert own_session(cfg, str(proc), pid=77) is None                         # no such process
    _proc(proc, 50, 51); _proc(proc, 51, 50)
    assert own_session(cfg, str(proc), pid=50) is None                         # a cycle ends


def _server(tmp_path, me):
    store = _store(tmp_path)
    cfg = Config(); cfg.hub.token = "t"
    client = HubClient(cfg)
    client._c = TestClient(Hub(store, token="t").app(), headers=H)
    return Server(client, lambda: me), store


def _call(server, name, **args):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})["result"]
    return r["isError"], r["content"][0]["text"]


def test_send_message_tool_sends_as_this_session_only(tmp_path):
    s, store = _server(tmp_path, A)
    err, text = _call(s, "send_message", session="api work", text="please rebase")
    assert not err and text.startswith(f"message 1 from {A} queued for {B}: delivering now")
    (m,) = store.messages(B)
    assert (m["from_key"], m["from_host"], m["body"]) == (A, "laptop", "please rebase")
    assert _call(s, "send_message", session=B, text="again") == (True, "your last message to that session hasn't been delivered yet")
    assert _call(s, "send_message", session="docs work", text="x") == (True, "that's this session")
    assert _call(s, "send_message", session=B, text="  ")[0]
    (tmp_path / "2").mkdir()
    nobody, store2 = _server(tmp_path / "2", None)
    err, text = _call(nobody, "send_message", session=B, text="x")
    assert err and text.startswith("can't tell which session this is") and store2.messages(B) == []


async def test_collector_asks_the_hub_for_agent_messages(tmp_path, monkeypatch):
    import asyncio

    import pytest
    col = _collector(tmp_path, monkeypatch)
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        raise asyncio.CancelledError                                           # one poll is enough
    real = httpx.AsyncClient
    monkeypatch.setattr("ctxswitch.collector.main.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(asyncio.CancelledError):
        await col.inbox_loop()
    assert seen == [{"host": "laptop", "wait": "25", "agents": "1"}]
