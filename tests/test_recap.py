"""Recap tracker against a fake seat (httpx.MockTransport): what is sent, when, and what gets stored."""
import json
import time
from datetime import datetime, timezone

import httpx

from ctxswitch.common.config import SeatConfig
from ctxswitch.hub.store import Store
from ctxswitch.hub.tracker.recap import Recapper, anchor_of, build_prompt

NOW = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _session(store, key="laptop/claude/s1", last_activity=NOW):
    h, harness, sid = key.split("/")
    store.upsert_session({"key": key, "host_id": h, "harness": harness, "session_id": sid, "title": "Recap work",
                          "cwd": "/home/rick/repos/ctxswitch", "last_activity": last_activity,
                          "first_prompt": "add a recap line to the TUI"})


def _turn(idx, user, agent, complete=True, asks=()):
    return {"idx": idx, "started_at": NOW, "ended_at": NOW if complete else None, "user_text": user, "user_origin": "human",
            "assistant_text": agent, "tally": {}, "asks": list(asks), "events": [], "complete": complete, "offset_end": idx * 100 + 10}


class Seat:
    def __init__(self, reply="You approved the plan; it is wiring the tracker.", status=200):
        self.reply, self.status, self.bodies = reply, status, []

    def handler(self, req):
        self.bodies.append(json.loads(req.content))
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "down"})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"recap": self.reply})}}]})


def _recapper(store, seat, **kw):
    return Recapper(store, SeatConfig(), transport=httpx.MockTransport(seat.handler), debounce=0, **kw)


async def test_recap_made_once_per_anchor_and_stored(tmp_path):
    store = Store(tmp_path / "hub.db")
    _session(store)
    store.upsert_turns("laptop/claude/s1", [_turn(0, "add a recap line", "Shall I put it above LAST EXCHANGE?")], None)
    seat = Seat()
    r = _recapper(store, seat)
    assert await r.step() == 1
    s = store.session("laptop/claude/s1")
    assert s["recap"] == "You approved the plan; it is wiring the tracker." and s["recap_anchor"] == anchor_of(s)
    assert any(x["recap"] for x in store.sessions())                     # listed sessions carry it too
    body = seat.bodies[0]
    assert body["model"] == "saint-local-text" and body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["response_format"]["json_schema"]["strict"] is True
    user_msg = body["messages"][1]["content"]
    assert "add a recap line to the TUI" in user_msg and "Shall I put it above LAST EXCHANGE?" in user_msg
    # nothing changed: no second call
    assert await r.step() == 0 and len(seat.bodies) == 1
    # a new exchange moves the anchor: recapped again
    store.upsert_turns("laptop/claude/s1", [_turn(1, "yes", "Done.")], None)
    assert await r.step() == 1 and len(seat.bodies) == 2


async def test_open_ask_lands_in_the_prompt_and_moves_the_anchor(tmp_path):
    store = Store(tmp_path / "hub.db")
    _session(store)
    store.upsert_turns("laptop/claude/s1", [_turn(0, "go", "Plan ready.")], None)
    s0 = store.session("laptop/claude/s1")
    ask = {"ref": "t1", "kind": "plan", "text": "Approve the recap plan?", "answered": False}
    store.upsert_turns("laptop/claude/s1", [_turn(0, "go", "Plan ready.", asks=[ask])], None)
    s1 = store.session("laptop/claude/s1")
    assert anchor_of(s0) != anchor_of(s1)
    assert "WAITING ON YOU:\n- [plan] Approve the recap plan?" in build_prompt(s1)


async def test_debounce_old_sessions_and_seat_failure(tmp_path):
    store = Store(tmp_path / "hub.db")
    _session(store)
    _session(store, "laptop/pi/old", last_activity="2020-01-01T00:00:00Z")
    store.upsert_turns("laptop/claude/s1", [_turn(0, "go", "ok")], None)
    store.upsert_turns("laptop/pi/old", [_turn(0, "go", "ok")], None)
    store.db.execute("UPDATE sessions SET last_activity='2020-01-01T00:00:00Z' WHERE key='laptop/pi/old'")
    seat = Seat()
    r = Recapper(store, SeatConfig(), transport=httpx.MockTransport(seat.handler), debounce=4)
    t = time.time()
    assert r.due(t) == []                                   # first sight of the anchor: wait for it to settle
    assert r.due(t + 1) == []
    assert r.due(t + 5) == ["laptop/claude/s1"]             # settled; the 2020 session is never considered
    # seat down: no recap stored, and the tracker backs off instead of hammering it
    down = Seat(status=503)
    r2 = _recapper(store, down)
    assert await r2.step() == 0 and store.session("laptop/claude/s1")["recap"] is None
    assert await r2.step() == 0 and len(down.bodies) == 1


def test_store_migrates_a_pre_recap_database(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "old.db")
    db.execute("CREATE TABLE sessions (key TEXT PRIMARY KEY, host_id TEXT NOT NULL, harness TEXT NOT NULL, session_id TEXT NOT NULL, "
               "meta TEXT NOT NULL, title TEXT, cwd TEXT, repo_root TEXT, repo_remote TEXT, git_branch TEXT, started_at TEXT, "
               "last_activity TEXT, ended INTEGER NOT NULL DEFAULT 0, live_status TEXT NOT NULL DEFAULT 'unknown', status_since TEXT, "
               "pid INTEGER, socket TEXT, last_viewed_at TEXT, updated REAL NOT NULL)")
    db.commit(); db.close()
    store = Store(tmp_path / "old.db")
    _session(store)
    store.set_recap("laptop/claude/s1", "hi", "a")
    assert store.session("laptop/claude/s1")["recap"] == "hi"
