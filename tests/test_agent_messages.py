"""Agents messaging other sessions as themselves: who the MCP server says is sending, the hub's checks, the header
the collector delivers it with, and the turn recorded as a peer's."""
import json
from pathlib import Path

import httpx
from starlette.testclient import TestClient
from test_messages import FakeSocket, Hub2, _collector, needs_unix

from ctxswitch.client import fmt
from ctxswitch.client.hubclient import HubClient
from ctxswitch.client.mcp_server import Server, own_session
from ctxswitch.collector import deliver
from ctxswitch.common.config import Config
from ctxswitch.common.schema import Liveness
from ctxswitch.hub import store as store_mod
from ctxswitch.hub.api import Hub
from ctxswitch.hub.store import Store
from ctxswitch.hub.tracker.recap import build_prompt

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
    _post(c, B, from_key=A)                                              # accepted: it queues behind the first
    assert (store.messages(B)[0]["from_key"], store.messages(B)[1]["waiting_behind"]) == (A, mid)
    assert _post(c, P, from_key=A).status_code == 200                    # another recipient is its own queue
    mine = _post(c, B, body="mine").json()["id"]                         # and the operator isn't limited
    store.set_message_status(mid, "devbox", "sent")
    assert [m["id"] for m in store.outbox("devbox", agents=True)] == [mine]   # yours goes; A's waits its turn
    store.cancel_message(_post(c, A, from_key=B).json()["id"])
    monkeypatch.setattr(store_mod, "AGENT_PER_HOUR", 2)
    store.db.execute("UPDATE messages SET status='replied' WHERE from_key=?", (A,))           # none in flight now
    r = _post(c, B, from_key=A)
    assert r.status_code == 429 and "has sent 3 messages" in r.json()["error"]  # A sent 3 this hour, 2 of them here
    assert _post(c, P, from_key=B).status_code == 200                           # B's cancelled one doesn't count
    assert _post(c, A, from_key=B).status_code == 200
    assert _post(c, B, from_key=P).status_code == 200
    assert _post(c, A, from_key=B).status_code == 429        # B's 1 open message is no bar; its 2 this hour are


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


def test_one_sender_s_messages_to_a_session_are_delivered_in_order(tmp_path):
    """A session sitting in one long tool call takes what's waiting when it returns, so the sender isn't refused for
    the whole call — but a later message waits while an earlier one hasn't been delivered."""
    store = _store(tmp_path)
    one = store.add_message(B, "first", "laptop", from_key=A)
    two = store.add_message(B, "second", "laptop", from_key=A)
    mine = store.add_message(B, "yours", "laptop")                       # the operator's is never held behind A's
    assert [m["id"] for m in store.outbox("devbox", agents=True)] == [one, mine]   # A's oldest goes, its next waits
    store.set_message_status(one, "devbox", "sent")                      # written to the socket, not taken yet
    assert [m["id"] for m in store.outbox("devbox", agents=True)] == [mine]
    store.db.execute("UPDATE messages SET status='delivered' WHERE id=?", (one,))
    assert [m["id"] for m in store.outbox("devbox", agents=True)] == [two, mine]   # A's queue opened: the next in order


def test_a_message_waiting_behind_another_says_so(tmp_path):
    store = _store(tmp_path)
    one = store.add_message(B, "first", "laptop", from_key=A)
    store.add_message(B, "second", "laptop", from_key=A)
    s = store.session(B, turns=0)
    first, second = s["messages"]
    assert (first["waiting_behind"], second["waiting_behind"]) == (None, one)
    assert fmt.message_note(s, second) == f"waiting behind #{one}"       # not "delivering…": nothing goes yet
    assert "waiting" not in fmt.message_note(s, first)
    store.db.execute("UPDATE messages SET status='delivered' WHERE id=?", (one,))
    assert store.session(B, turns=0)["messages"][1]["waiting_behind"] is None
    assert fmt.message_note(s, store.session(B, turns=0)["messages"][1]) == "delivering…"


def test_a_backlog_of_five_is_taken_and_the_sixth_is_refused(tmp_path):
    store = _store(tmp_path)
    c = TestClient(Hub(store, token="t").app())
    ids = [_post(c, B, from_key=A, body=f"note {i}").json()["id"] for i in range(store_mod.AGENT_BACKLOG)]
    assert len(set(ids)) == store_mod.AGENT_BACKLOG                       # none refused: a session can be busy for minutes
    r = _post(c, B, from_key=A, body="and one more")
    assert r.status_code == 409 and r.json()["error"] == (f"your last {store_mod.AGENT_BACKLOG} messages to that session "
                                                          f"are not delivered yet (the backlog is {store_mod.AGENT_BACKLOG})")
    assert [m["id"] for m in store.outbox("devbox", agents=True)] == [ids[0]]      # only the oldest goes
    store.db.execute("UPDATE messages SET status='delivered' WHERE id=?", (ids[0],))
    assert [m["id"] for m in store.outbox("devbox", agents=True)] == [ids[1]]
    assert _post(c, B, from_key=A, body="now the sixth").status_code == 200        # the backlog has room again


def test_each_sender_waits_behind_its_own_messages_only(tmp_path):
    store = _store(tmp_path)
    a1 = store.add_message(B, "a first", "laptop", from_key=A)
    store.add_message(B, "a second", "laptop", from_key=A)
    p1 = store.add_message(B, "p first", "laptop", from_key=P)
    assert [m["id"] for m in store.outbox("devbox", agents=True)] == [a1, p1]      # each pair its own queue
    store.set_message_status(a1, "devbox", "sent")
    assert [m["id"] for m in store.outbox("devbox", agents=True)] == [p1]


def test_a_workstream_message_waits_behind_the_same_senders_earlier_one(tmp_path):
    """Binding a workstream message hands it to that session's collector, so it can't jump the queue that sender
    already has with it. Nothing sends a workstream message as an agent yet; the row would carry from_key if it did."""
    from test_phase45 import _session
    store = Store(tmp_path / "hub.db")
    for k in (A, B):
        _session(store, k, live=False)
    store.set_liveness("devbox", [{"key": B, "status": "idle", "status_since": T0, "pid": 1, "socket": "/x"}])
    c = TestClient(Hub(store, token="t").app())
    first = c.post(f"/v1/sessions/{B}/messages", headers=H, json={"body": "first", "from_key": A}).json()["id"]
    held = c.post("/v1/workstreams/messages", headers=H, json={"workstream": "me/proj@main", "body": "second"}).json()["id"]
    store.db.execute("UPDATE messages SET from_key=? WHERE id=?", (A, held))
    assert [m["id"] for m in store.outbox("devbox", agents=True)] == [first]       # it stays unbound: A's earlier
    assert [m["id"] for m in store.session(B)["workstream_messages"]] == [held]    # shown as unclaimed, not delivered
    store.db.execute("UPDATE messages SET status='delivered' WHERE id=?", (first,))
    assert [m["id"] for m in store.outbox("devbox", agents=True)] == [held]        # bound, and delivered in order


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
    lines = fmt.show_lines(s, 2)
    assert f"       from {B}: yes, docs too" in lines and "       you  : go" in lines    # labelled with who sent it
    assert f"- #1 another agent session ({B}), not you: yes, docs too" in build_prompt(s)   # the recap isn't told it's you
    assert fmt.who({"user_origin": "peer"}) == "peer" and fmt.who({"user_origin": "task"}) == "task"


def test_header_names_the_sender_for_both_harnesses():
    h = deliver.agent_header(B, "api   work\nnow")
    assert h.startswith("Message from another agent session, not from the operator") and deliver.HEADER not in h
    assert f'From: {deliver.SENDER_NAMES[B.split("/")[1]]} session {B} "api work now" on host devbox' in h
    assert f'session "{B}"' in h
    for harness, tool in (("claude", "use the mcp__ctxswitch__send_message tool"), ("pi", "use the ctxswitch MCP server's send_message tool")):
        c = json.loads(deliver.line(harness, "s", "hi", 3, from_key=B, from_title="api work"))["message"]["content"]
        assert c == deliver.agent_header(B, "api work", harness) + "hi" and tool in c
    assert "From: pi session devbox/pi/x on host devbox\n" in deliver.agent_header("devbox/pi/x", "")


def test_the_from_line_says_which_agent_sent_it_whatever_the_receiver_calls_it():
    """Claude Code heads every inbox message "Another Claude session sent a message"; the From line says who it was."""
    for key, kind in (("laptop/agy/c2d3", "Antigravity session"), ("laptop/claude/a1", "Claude Code session"),
                      ("devbox/pi/p1", "pi session"), ("laptop/claude-win/w1", "Claude Code (Windows) session"),
                      ("laptop/shell/s1", "session")):
        h = deliver.agent_header(key, "t")
        assert f"From: {kind} {key} " in h, h
        assert h.startswith("Message from another agent session, not from the operator (sent with ctxswitch).\n")


@needs_unix
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
    err, text = _call(s, "send_message", session=B, text="again")        # a busy session queues them in order
    assert not err and text.startswith(f"message 2 from {A} queued for {B}")
    assert [x["waiting_behind"] for x in store.messages(B)] == [None, 1]
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
