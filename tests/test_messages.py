"""Messages: hub lifecycle (queued → sent → delivered → replied), the outbox long-poll, and collector delivery into
fake session sockets."""
import asyncio
import json
import time

import httpx
import pytest
from starlette.testclient import TestClient

from conftest import unix_sockets_ok
from ctxswitch.client import fmt
from ctxswitch.collector import deliver
from ctxswitch.collector.main import Collector
from ctxswitch.common.config import Config
from ctxswitch.common.schema import Liveness
from ctxswitch.hub.api import Hub
from ctxswitch.hub.store import Store

NOW = "2026-10-29T10:00:00Z"
KEY = "laptop/claude/s1"
H = {"Authorization": "Bearer t"}


def _session(store, key=KEY, live=True):
    h, harness, sid = key.split("/")
    store.upsert_session({"key": key, "host_id": h, "harness": harness, "session_id": sid, "title": "T", "cwd": "/r",
                          "last_activity": NOW})
    if live:
        store.set_liveness(h, [{"key": key, "status": "idle", "status_since": NOW, "pid": 1, "socket": "/x"}])


def _turn(idx, user, agent="", complete=False, origin="peer", mid=()):
    return {"idx": idx, "started_at": NOW, "ended_at": NOW, "user_text": user, "user_origin": origin, "assistant_text": agent,
            "tally": {}, "asks": [], "events": [], "complete": complete, "offset_end": idx * 100 + 10, "mid_texts": list(mid)}


def test_lifecycle_rewrites_the_delivering_turn_as_yours(tmp_path):
    store = Store(tmp_path / "hub.db")
    _session(store)
    mid = store.add_message(KEY, "Please   run the tests\nand report.", "laptop")
    store.upsert_turns(KEY, [_turn(0, deliver.HEADER + "Please run the tests and report.")], None)
    assert store.messages(KEY)[0]["status"] == "queued"                 # not sent yet: no match, even if the text is there
    assert store.outbox("laptop") == [{"id": mid, "key": KEY, "body": "Please   run the tests\nand report.", "harness": "claude",
                                     "from_key": None, "from_title": None}]
    assert not store.set_message_status(mid, "devbox", "sent")          # another host can't report it
    assert not store.set_message_status(mid, "laptop", "delivered")    # only sent / failed come from a collector
    assert store.set_message_status(mid, "laptop", "sent") and store.outbox("laptop") == []
    assert not store.set_message_status(mid, "laptop", "sent")         # already reported

    store.upsert_turns(KEY, [_turn(1, deliver.HEADER + "Please run the tests and report.", "running")], None)
    (m,) = store.messages(KEY)
    assert (m["status"], m["turn_idx"]) == ("delivered", 1)
    t = store.session(KEY, turns=1)["turns"][-1]
    assert (t["user_text"], t["user_origin"], t["via"], t["message_id"]) == (m["body"], "human", "ctx", mid)

    store.upsert_turns(KEY, [_turn(1, deliver.HEADER + "Please run the tests and report.", "12 passed", complete=True)], None)
    (m,) = store.session(KEY)["messages"]
    assert (m["status"], m["reply"]) == ("replied", "12 passed")
    assert "#1: 12 passed" in fmt.message_note(store.session(KEY), m)


def test_outbox_only_lists_sessions_live_on_that_host_and_cancel_is_queued_only(tmp_path):
    store = Store(tmp_path / "hub.db")
    _session(store)
    _session(store, "laptop/pi/p1", live=False)
    _session(store, "devbox/claude/s2")
    a, b, c = (store.add_message(k, "hi") for k in (KEY, "laptop/pi/p1", "devbox/claude/s2"))
    assert [m["id"] for m in store.outbox("laptop")] == [a]            # pi session ended: waits for it
    assert [m["id"] for m in store.outbox("devbox")] == [c]
    store.set_liveness("laptop", [{"key": "laptop/pi/p1", "status": "idle", "status_since": NOW, "pid": 2, "socket": "/y"}])
    assert [m["id"] for m in store.outbox("laptop")] == [b]            # ... and goes once it runs again (s1 now ended)
    assert store.cancel_message(b) and not store.cancel_message(b)
    store.set_message_status(c, "devbox", "sent")
    assert not store.cancel_message(c)                                 # already in the session
    assert store.outbox("laptop") == [] and [m["status"] for m in store.messages("laptop/pi/p1")] == ["cancelled"]


def test_api_send_refuses_what_it_cant_deliver(tmp_path):
    store = Store(tmp_path / "hub.db")
    _session(store)
    _session(store, "laptop/claude-win/w1", live=False)
    c = TestClient(Hub(store, token="t").app())
    url = f"/v1/sessions/{KEY}/messages"
    assert c.post(url, json={"body": "x"}).status_code == 401
    assert c.post(url, headers=H, json={"body": "  "}).status_code == 400
    assert c.post(url, headers=H, json={"body": "x" * 20001}).status_code == 400
    assert c.post("/v1/sessions/laptop/claude-win/w1/messages", headers=H, json={"body": "x"}).status_code == 400
    assert c.post("/v1/sessions/laptop/claude/nope/messages", headers=H, json={"body": "x"}).status_code == 404
    mid = c.post(url, headers=H, json={"body": "go", "from_host": "devbox"}).json()["id"]
    assert c.get(f"/v1/sessions/{KEY}", headers=H).json()["messages"][0]["from_host"] == "devbox"
    assert c.get("/v1/outbox", headers=H).status_code == 400
    assert [m["id"] for m in c.get("/v1/outbox?host=laptop", headers=H).json()["messages"]] == [mid]
    st = f"/v1/messages/{mid}/status"
    assert c.post(st, headers=H, json={"host_id": "devbox", "status": "sent"}).status_code == 409
    assert c.post(st, headers=H, json={"host_id": "laptop", "status": "sent"}).json() == {"ok": True}
    assert c.post(f"/v1/messages/{mid}/cancel", headers=H).status_code == 409


async def test_outbox_long_poll_wakes_when_a_message_is_queued(tmp_path):
    store = Store(tmp_path / "hub.db")
    _session(store)
    hub = Hub(store, token="t")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hub.app()), base_url="http://hub") as c:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        poll = asyncio.create_task(c.get("/v1/outbox", params={"host": "laptop", "wait": 5}, headers=H))
        await asyncio.sleep(0.3)
        assert not poll.done()                                         # nothing waiting: it holds
        await c.post(f"/v1/sessions/{KEY}/messages", headers=H, json={"body": "wake"})
        r = await asyncio.wait_for(poll, 3)
        assert [m["body"] for m in r.json()["messages"]] == ["wake"] and loop.time() - t0 < 2
        r = await c.get("/v1/outbox", params={"host": "devbox", "wait": 0.2}, headers=H)
        assert r.json() == {"messages": []}                           # times out empty


needs_unix = pytest.mark.skipif(not unix_sockets_ok(),
                                reason="this sandbox forbids the unix sockets collector delivery binds")


class FakeSocket:
    """A session socket: records each line; pi answers like the extension, Claude answers nothing."""
    def __init__(self, path, reply=None):
        self.path, self.reply, self.lines = str(path), reply, []

    async def __aenter__(self):
        async def handle(r, w):
            self.lines.append(json.loads(await r.readline()))
            if self.reply is not None:
                w.write((json.dumps(self.reply) + "\n").encode()); await w.drain()
            w.close()
        self.server = await asyncio.start_unix_server(handle, self.path)
        return self

    async def got(self, n):
        for _ in range(100):                           # the server reads after the client has already closed
            if len(self.lines) >= n:
                return self.lines
            await asyncio.sleep(0.01)
        return self.lines

    async def __aexit__(self, *a):
        self.server.close(); await self.server.wait_closed()


def test_line_format():
    c = json.loads(deliver.line("claude", "s1", "hi", 7))
    assert c == {"type": "user", "message": {"content": deliver.HEADER + "hi"}, "msg_id": "ctx-7", "session_id": "s1"}
    assert json.loads(deliver.line("pi", "p1", "hi", 7))["message"]["content"] == "hi"


class Hub2:
    """Records the collector's status reports."""
    def __init__(self, code=200):
        self.code, self.reports = code, []

    def handler(self, req):
        self.reports.append((req.url.path, json.loads(req.content)))
        return httpx.Response(self.code, json={"ok": self.code == 200})


def _collector(tmp_path, monkeypatch):
    cfg = Config()
    cfg.collector.queue_path = str(tmp_path / "q.db"); cfg.collector.host_id = "laptop"
    monkeypatch.setattr("ctxswitch.collector.main.runtime_dir", lambda: tmp_path / "rt")
    return Collector(cfg)


@needs_unix
async def test_collector_delivers_once_and_only_to_its_own_live_sockets(tmp_path, monkeypatch):
    col = _collector(tmp_path, monkeypatch)
    hub = Hub2()
    async with httpx.AsyncClient(transport=httpx.MockTransport(hub.handler)) as client, \
            FakeSocket(tmp_path / "c.sock") as cs, FakeSocket(tmp_path / "p.sock", {"ok": True}) as ps:
        m1 = {"id": 1, "key": KEY, "body": "hello"}
        assert not await col.deliver_one(client, m1)                  # not in our registry: nothing written
        assert cs.lines == [] and hub.reports == []
        col.live = {KEY: Liveness(KEY, "idle", socket=cs.path), "laptop/pi/p1": Liveness("laptop/pi/p1", "busy", socket=ps.path),
                    "laptop/claude-win/w1": Liveness("laptop/claude-win/w1", "idle", socket=cs.path)}
        assert not await col.deliver_one(client, {"id": 9, "key": "laptop/claude-win/w1", "body": "x"})
        assert await col.deliver_one(client, m1)
        assert await cs.got(1) == [json.loads(deliver.line("claude", "s1", "hello", 1))]
        assert hub.reports == [("/v1/messages/1/status", {"host_id": "laptop", "status": "sent", "error": None})]
        hub.code = 503
        assert not await col.deliver_one(client, m1)                  # report lost: retried ...
        hub.code = 409
        assert await col.deliver_one(client, m1)
        assert len(await cs.got(2)) == 1                                  # ... but the session got it once
        assert await col.deliver_one(client, {"id": 2, "key": "laptop/pi/p1", "body": "do x"})
        assert ps.lines[0]["message"]["content"] == "do x" and hub.reports[-1][1]["status"] == "sent"


@needs_unix
async def test_collector_reports_a_refusal_or_a_dead_socket_as_failed(tmp_path, monkeypatch):
    col = _collector(tmp_path, monkeypatch)
    hub = Hub2()
    async with httpx.AsyncClient(transport=httpx.MockTransport(hub.handler)) as client, \
            FakeSocket(tmp_path / "p.sock", {"ok": False, "error": "no session"}) as ps:
        col.live = {"laptop/pi/p1": Liveness("laptop/pi/p1", "idle", socket=ps.path),
                    KEY: Liveness(KEY, "idle", socket=str(tmp_path / "gone.sock"))}
        assert await col.deliver_one(client, {"id": 3, "key": "laptop/pi/p1", "body": "x"})
        assert await col.deliver_one(client, {"id": 4, "key": KEY, "body": "x"})
    (_, r3), (_, r4) = hub.reports
    assert (r3["status"], r3["error"]) == ("failed", "no session")
    assert r4["status"] == "failed" and r4["error"].startswith("can't connect")


def test_notes_say_where_a_message_is():
    s = {"harness": "claude", "live_status": "idle", "meta": {"permission_mode": "plan"}}
    assert fmt.message_note(s, {"status": "queued"}) == "delivering…"
    assert "queued" in fmt.message_note(s | {"live_status": "ended"}, {"status": "queued"})
    assert "approval in plan mode" in fmt.message_note(s, {"status": "sent"})
    assert fmt.can_message({"harness": "claude-win"}) and not fmt.can_message(s)


def test_liveness_keeps_and_clears_the_process_that_shows_a_session(tmp_path):
    from ctxswitch.hub.store import Store
    st = Store(tmp_path / "h.db")
    st.set_liveness("laptop", [{"key": "laptop/claude/job", "status": "busy", "pid": 200, "via_pid": 100}])
    assert st.session("laptop/claude/job", turns=0)["via_pid"] == 100
    st.set_liveness("laptop", [])                                      # it ended: nothing shows it any more
    assert st.session("laptop/claude/job", turns=0)["via_pid"] is None


def by_id(mid, store):
    return {x["id"]: x for x in store.messages(KEY)}[mid]


def _resend(store, from_key, body):
    """The hub's answer to this sender's next message: its status, or "ok" when it took it."""
    c = TestClient(Hub(store, token="t").app())
    r = c.post(f"/v1/sessions/{KEY}/messages", headers=H, json={"body": body, "from_key": from_key})
    return r.status_code if r.status_code != 200 else "ok"


def test_a_message_taken_mid_turn_is_delivered_without_rewriting_the_turn(tmp_path):
    """A session that never goes idle surfaces a message inside the turn already running, so waiting for a prompt of
    its own leaves the row `sent` forever — and then that sender's whole backlog would hold behind it."""
    from ctxswitch.hub import store as store_mod
    store = Store(tmp_path / "hub.db")
    _session(store)
    a, b = "devbox/claude/s9", "devbox/claude/s8"
    for k in (a, b):
        h, harness, sid = k.split("/")
        store.upsert_session({"key": k, "host_id": h, "harness": harness, "session_id": sid, "title": "T", "cwd": "/x",
                              "last_activity": NOW})
    store.upsert_turns(KEY, [_turn(0, "work on it", "working", origin="human")], None)
    m1 = store.add_message(KEY, "rebase main when the build finishes", "devbox", from_key=a)
    store.set_message_status(m1, "laptop", "sent")
    assert store.agent_send_refused(a, KEY) is None                      # one open message is a backlog of 1, not a refusal

    rendered = deliver.agent_header(a, "api work") + "rebase main when the build finishes"
    body = "rebase main when the build finishes"
    store.upsert_turns(KEY, [_turn(0, "work on it", "rebuilt, rebasing", origin="human", mid=[rendered, body])], None)
    (m,) = store.messages(KEY)
    assert (m["status"], m["turn_idx"], m["mid_turn"]) == ("delivered", 0, 1) and m["delivered_at"]
    t = store.session(KEY, turns=1)["turns"][-1]
    assert (t["user_text"], t["user_origin"], t.get("via")) == ("work on it", "human", None)   # still its own prompt
    assert t["message_id"] == m1 and "from_key" not in t
    assert store.agent_send_refused(a, KEY) is None                     # no longer in flight: the next one goes
    assert _resend(store, a, "and then run the suite") == "ok"          # the hub takes it at the API too

    m2 = store.add_message(KEY, "and the changelog too", "devbox", from_key=b)
    store.set_message_status(m2, "laptop", "sent")
    store.upsert_turns(KEY, [_turn(0, "work on it", "done", origin="human", complete=True,
                                   mid=[deliver.agent_header(b, "docs work") + "and the changelog too"])], None)
    by = {x["id"]: x for x in store.messages(KEY)}
    assert (by[m1]["status"], by[m2]["status"]) == ("replied", "replied")     # both taken by that one turn
    assert (by[m2]["mid_turn"], by[m2]["turn_idx"], by[m2]["reply"]) == (1, 0, "done")
    assert store.session(KEY, turns=1)["turns"][-1]["assistant_text"] == "done"     # the turn's own reply, not rewritten

    # a prompt turn wins over a mid-turn text naming the same message
    m3 = store.add_message(KEY, "open a ticket for it", "devbox", from_key=a)
    store.set_message_status(m3, "laptop", "sent")
    store.upsert_turns(KEY, [_turn(1, deliver.agent_header(a, "api work") + "open a ticket for it", "filed",
                                   origin="human", complete=True, mid=[deliver.agent_header(a, "api work") + "open a ticket for it"])],
                        None)
    t1 = [t for t in store.session(KEY, turns=2)["turns"] if t["idx"] == 1][0]
    assert (t1["user_origin"], t1["from_key"], t1.get("via")) == ("peer", a, "ctx")
    assert by_id(m3, store)["mid_turn"] == 0                        # the prompt turn, not a mid-turn flag

    # the 6-hour valve: a row still `sent` (the two sides disagreed about it) stops holding the sender's queue
    s7 = "devbox/claude/s7"
    store.upsert_session({"key": s7, "host_id": "devbox", "harness": "claude", "session_id": "s7", "title": "T",
                          "cwd": "/x", "last_activity": NOW})
    stuck = [store.add_message(KEY, f"stuck one {i}", "devbox", from_key=s7) for i in range(store_mod.AGENT_BACKLOG)]
    for c in stuck:
        store.set_message_status(c, "laptop", "sent")                   # none of them ever reached the session
    assert store.agent_send_refused(s7, KEY) is not None                # a full backlog of rows nobody took
    follow = store.add_message(KEY, "and its follow-up", "devbox", from_key=s7)
    mine = lambda: [x["id"] for x in store.outbox("laptop", agents=True) if x["from_key"] == s7]
    assert mine() == []                                                 # ... and the next one waits behind them
    store.db.execute("UPDATE messages SET created=? WHERE from_key=?", (time.time() - store_mod.SENT_STALE_S - 60, s7))
    assert store.agent_send_refused(s7, KEY) is None                    # past the valve they stop holding the queue
    assert mine() == [follow]                                           # the valve opened its queue again
    stale = {x["id"]: x for x in store.messages(KEY)}[stuck[0]]
    assert stale["status"] == "sent"                                    # a stale row still reads as undelivered
    assert "not in the transcript yet" in fmt.message_note(store.session(KEY), stale)


def test_a_mid_turn_delivery_reads_that_way():
    s = {"harness": "claude", "live_status": "busy"}
    assert fmt.message_note(s, {"status": "delivered", "turn_idx": 4, "mid_turn": 1}) == "delivered mid-turn in #4"
    assert fmt.message_note(s, {"status": "delivered", "turn_idx": 4}) == "delivered as #4, working on it"


def test_an_older_database_gains_the_mid_turn_column(tmp_path):
    path = tmp_path / "old.db"
    Store(path).db.execute("ALTER TABLE messages DROP COLUMN mid_turn")      # a db written before mid-turn tracking
    store = Store(path)
    assert "mid_turn" in {r[1] for r in store.db.execute("PRAGMA table_info(messages)")}
    _session(store)
    mid = store.add_message(KEY, "rebase main", "devbox")
    store.set_message_status(mid, "laptop", "sent")
    store.upsert_turns(KEY, [_turn(0, "work on it", origin="human", mid=["rebase main"])], None)
    assert store.messages(KEY)[0]["status"] == "delivered"
