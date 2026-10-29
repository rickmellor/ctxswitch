"""Search across sessions (store + API) and the MCP server: the protocol, each tool through the real hub app, and
the errors an agent gets back instead of a dead server."""
import io
import json

from starlette.applications import Starlette
from starlette.testclient import TestClient

from ctxswitch.client.hubclient import HubClient
from ctxswitch.client.mcp_server import PROTOCOLS, Server, serve
from ctxswitch.common.config import Config
from ctxswitch.hub.api import Hub
from ctxswitch.hub.store import Store

H = {"Authorization": "Bearer t"}
A, B = "laptop/claude/s1", "devbox/pi/p1"


def _session(store, key, title, live=True):
    h, harness, sid = key.split("/")
    store.upsert_session({"key": key, "host_id": h, "harness": harness, "session_id": sid, "title": title, "cwd": "/r",
                          "last_activity": "2026-10-29T10:00:00Z"})
    if live:
        store.set_liveness(h, [{"key": key, "status": "idle", "status_since": "2026-10-29T10:00:00Z", "pid": 1, "socket": "/x"}])


def _turn(idx, user, agent="", t="2026-10-29T10:00:00Z", origin="human", asks=()):
    return {"idx": idx, "started_at": t, "ended_at": t, "user_text": user, "user_origin": origin, "assistant_text": agent,
            "tally": {}, "asks": list(asks), "events": [], "complete": True, "offset_end": idx * 100 + 10}


def _row(kind, text, extra=None):
    return {"kind": kind, "text": text, "quote": "q", "extra": extra or {}}


def _store(tmp_path):
    store = Store(tmp_path / "hub.db")
    _session(store, A, "tabs work")
    _session(store, B, "pi docs")
    q = {"ref": "toolu_1", "kind": "question", "text": "Tabs or spaces?", "answered": True, "answer": "Spaces, 4 wide",
         "answered_at": "2026-10-29T10:01:00Z"}
    store.upsert_turns(A, [_turn(0, "set up the repo", "Tabs or spaces?", asks=[q]),
                           _turn(1, "don't push to main, use 100% of the pool", t="2026-10-29T10:02:00Z"),
                           _turn(2, "a peer said push_it", t="2026-10-29T10:03:00Z", origin="peer"),
                           _turn(3, "tried pushXit", t="2026-10-29T10:04:00Z")], None)
    store.apply_extraction(A, _turn(1, "x"), [_row("instruction", "Don't push to main")], [], [], None)
    store.apply_extraction(A, _turn(2, "x"), [_row("claim", "Tests pass on main", {"claim": "done"})], [], [], None)
    store.upsert_turns(B, [_turn(0, "write the docs", t="2026-10-29T09:00:00Z")], None)
    store.apply_extraction(B, _turn(0, "x"), [_row("decision", "Docs go in docs/, not the wiki", {"question": "Where do docs go?"}),
                                              _row("instruction", "use 100x zoom")], [], [], None)
    return store


def test_search_finds_every_kind_newest_first(tmp_path):
    store = _store(tmp_path)
    got = [(r["key"], r["kind"], r["text"]) for r in store.search("main")]
    assert got == [(A, "claim", "Tests pass on main"), (A, "instruction", "Don't push to main"),     # turn 2, then turn 1
                   (A, "prompt", "don't push to main, use 100% of the pool")]
    (d,) = store.search("spaces")                                     # an answered question is a decision
    assert (d["kind"], d["question"], d["text"], d["title"], d["host_id"]) == ("decision", "Tabs or spaces?", "Spaces, 4 wide", "tabs work", "laptop")
    (d,) = store.search("where docs")                                 # the question counts as text
    assert (d["key"], d["question"]) == (B, "Where do docs go?")
    assert [r["kind"] for r in store.search("push main", kinds=("instruction",))] == ["instruction"]   # every word, any order
    assert store.search("push wiki") == [] and store.search("   ") == []
    assert [r["text"] for r in store.search("100%")] == ["don't push to main, use 100% of the pool"]   # % is a character
    assert store.search("push_it") == []                              # a peer's prompt isn't yours; _ is a character too
    assert {r["key"] for r in store.search("the")} == {A, B}
    assert {r["key"] for r in store.search("the", host="devbox")} == {B}
    assert len(store.search("o", limit=2)) == 2


def test_search_marks_retired_instructions(tmp_path):
    store = _store(tmp_path)
    (row,) = [r for r in store.ledger(A) if r["kind"] == "instruction"]
    store.retire_row(A, row["id"])
    (r,) = store.search("push", kinds=("instruction",))
    assert r["active"] is False


def test_search_api(tmp_path):
    c = TestClient(Hub(_store(tmp_path), token="t").app())
    assert c.get("/v1/search?q=main").status_code == 401
    r = c.get("/v1/search", params={"q": "main", "kind": "claim,instruction"}, headers=H).json()["results"]
    assert [x["kind"] for x in r] == ["claim", "instruction"]
    assert c.get("/v1/search", params={"q": "main", "kind": "bogus"}, headers=H).status_code == 400
    assert len(c.get("/v1/search", params={"q": "o", "limit": "1"}, headers=H).json()["results"]) == 1


def _server(tmp_path, store=None):
    cfg = Config()
    cfg.hub.token = "t"
    client = HubClient(cfg)
    client._c = TestClient(Hub(store or _store(tmp_path), token="t").app(), headers=H)
    return Server(client)


def _talk(server, *msgs):
    out = io.StringIO()
    serve(server, io.StringIO("".join((m if isinstance(m, str) else json.dumps(m)) + "\n" for m in msgs)), out)
    return [json.loads(x) for x in out.getvalue().splitlines()]


def _call(server, name, **args):
    (r,) = _talk(server, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    res = r["result"]
    return res["isError"], res["content"][0]["text"]


def test_handshake_lists_tools_and_answers_only_requests(tmp_path):
    s = _server(tmp_path)
    init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    r = _talk(s, init, {"jsonrpc": "2.0", "method": "notifications/initialized"}, "",
              {"jsonrpc": "2.0", "id": "x", "method": "ping"}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
              {"jsonrpc": "2.0", "id": 3, "method": "resources/list"}, "{not json", {"id": 4, "method": "ping"})
    assert [m.get("id") for m in r] == [0, "x", 2, 3, None, 4]       # the notification and blank line: no reply
    assert r[0]["result"]["protocolVersion"] == "2025-03-26" and r[0]["result"]["capabilities"] == {"tools": {}}
    assert "not the operator's" in r[0]["result"]["instructions"]
    assert r[1]["result"] == {}
    tools = {t["name"]: t for t in r[2]["result"]["tools"]}
    assert sorted(tools) == ["attention", "get_session", "list_sessions", "search", "send_message"]
    assert tools["get_session"]["inputSchema"]["required"] == ["session"]
    assert (r[3]["error"]["code"], r[4]["error"]["code"], r[5]["error"]["code"]) == (-32601, -32700, -32600)
    (old,) = _talk(s, init | {"params": {"protocolVersion": "1999-01-01"}})
    assert old["result"]["protocolVersion"] == PROTOCOLS[0]           # one we don't know: ours


def test_tools_read_the_hub(tmp_path):
    store = _store(tmp_path)
    store.upsert_turns(B, [_turn(1, "next", "Shall I publish?", t="2026-10-29T10:05:00Z",
                                 asks=[{"ref": "1:plain", "kind": "plain", "text": "Shall I publish?", "answered": False}])], None)
    s = _server(tmp_path, store)
    err, text = _call(s, "list_sessions")
    assert not err and sorted(ln.split()[0] for ln in text.splitlines()) == [B, A]
    assert "Shall I publish?" in text
    assert _call(s, "list_sessions", host="devbox")[1].split()[0] == B
    err, text = _call(s, "attention")
    assert not err and text.startswith(B) and "[plain] Shall I publish?" in text
    err, text = _call(s, "get_session", session="pi docs", turns=1)
    assert not err and text.startswith("◐ " + B) and "Shall I publish?" in text and "write the docs" not in text   # 1 exchange
    assert store.session(B)["last_viewed_at"] is None                 # an agent reading isn't the operator looking
    err, text = _call(s, "search", query="spaces")
    assert not err and text == f"{A} #0 (tabs work) [decision] Tabs or spaces?  →  Spaces, 4 wide"
    assert _call(s, "search", query="zebra") == (False, "nothing matches 'zebra'")


def test_bad_calls_come_back_as_tool_errors(tmp_path):
    s = _server(tmp_path)
    err, text = _call(s, "get_session", session="s")                  # matches both
    assert err and text.startswith("2 sessions match 's': ")
    assert _call(s, "get_session", session="zzz") == (True, "0 sessions match 'zzz'")
    assert _call(s, "get_session", session="a/b/c")[0]                # a full key that isn't there
    assert _call(s, "get_session") == (True, "missing argument(s): session")
    assert _call(s, "get_session", session="x", nope=1) == (True, "unknown argument(s): nope")
    assert _call(s, "get_session", session=3) == (True, "session must be a string")
    assert _call(s, "get_session", session="x", turns=True) == (True, "turns must be a integer")
    assert _call(s, "get_session", session="x", turns=99) == (True, "turns must be between 0 and 50")
    assert _call(s, "search", query="x", kind="bogus")[0]
    (r,) = _talk(s, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "post_message", "arguments": {}}})
    assert r["error"]["code"] == -32602
    (r,) = _talk(s, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search", "arguments": []}})
    assert r["result"]["isError"]


def test_hub_down_is_an_error_not_a_dead_server(tmp_path):
    cfg = Config()
    cfg.hub.url = "http://127.0.0.1:9"                                # nothing listens on discard
    s = Server(HubClient(cfg))
    err, text = _call(s, "attention")
    assert err and text.startswith("ConnectError")
    (r,) = _talk(s, {"jsonrpc": "2.0", "id": 5, "method": "ping"})
    assert r == {"jsonrpc": "2.0", "id": 5, "result": {}}


def test_an_older_hub_without_search_says_so(tmp_path):
    s = _server(tmp_path)
    s.c._c = TestClient(Starlette(routes=[]), headers=H)              # 404, plain text
    assert _call(s, "search", query="x") == (True, "HTTP 404")
