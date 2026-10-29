"""Ledger tracker against a fake seat: quote checks, retirement, plain-text asks, purpose history, scheduling."""
import json
from datetime import datetime, timedelta, timezone

import httpx
from starlette.testclient import TestClient

from ctxswitch.client import fmt
from ctxswitch.common.config import SeatConfig
from ctxswitch.hub.api import Hub
from ctxswitch.hub.store import Store
from ctxswitch.hub.tracker.extract import MAX_BAD, Extractor, quoted, validate
from ctxswitch.hub.tracker.recap import Recapper, build_prompt as recap_prompt

NOW = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
KEY = "laptop/claude/s1"
EMPTY = {"instructions": [], "retire": [], "decisions": [], "claims": [], "asks": [], "goal_changed": False, "purpose": ""}


def _session(store, key=KEY, last_activity=NOW):
    h, harness, sid = key.split("/")
    store.upsert_session({"key": key, "host_id": h, "harness": harness, "session_id": sid, "title": "T", "cwd": "/r",
                          "last_activity": last_activity})


def _turn(idx, user, agent, origin="human", complete=True, asks=()):
    return {"idx": idx, "started_at": NOW, "ended_at": NOW, "user_text": user, "user_origin": origin, "assistant_text": agent,
            "tally": {}, "asks": list(asks), "events": [], "complete": complete, "offset_end": idx * 100 + 10}


class Seat:
    """Replies with the queued outputs in order; records each prompt."""
    def __init__(self, *outs, status=200):
        self.outs, self.status, self.prompts = list(outs), status, []

    def handler(self, req):
        body = json.loads(req.content)
        self.prompts.append(body["messages"][1]["content"])
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "down"})
        out = self.outs.pop(0)
        return httpx.Response(200, json={"choices": [{"message": {"content": out if isinstance(out, str) else json.dumps({**EMPTY, **out})}}]})


def _ex(store, seat, **kw):
    return Extractor(store, SeatConfig(), transport=httpx.MockTransport(seat.handler), **kw)


def test_quote_must_come_from_its_own_side_of_the_exchange():
    turn = _turn(3, "Ship it, but don't push to main without asking me.", "Done: **Tests:** 72 pass and `q` detaches now.")
    assert quoted("Tests: 72 pass", turn["assistant_text"]) and quoted("q detaches now", turn["assistant_text"])   # markdown aside
    assert not quoted("pass", turn["assistant_text"])                                                          # too short to mean anything
    out = {**EMPTY,
           "instructions": [{"text": "ask before pushing to main", "quote": "don't push to main without asking me"},
                            {"text": "invented", "quote": "always use the local pool for builds"},
                            {"text": "ask before pushing to main", "quote": "push to main without asking"}],      # repeat
           "decisions": [{"text": "ship", "quote": "72 pass and q detaches", "question": "?"}],                    # agent text, not yours
           "claims": [{"text": "tests pass", "quote": "Tests: 72 pass", "kind": "tests_pass"},
                      {"text": "done", "quote": "Done: Tests: 72 pass", "kind": "bogus"},
                      {"text": "third", "quote": "and q detaches now", "kind": "done"}],                         # over the cap of 2
           "retire": [7, 99, "x"]}
    rows, retire, asks, dropped = validate(out, turn, None, [{"id": 7, "text": "use the pool"}])
    assert [(r["kind"], r["text"]) for r in rows] == [("instruction", "ask before pushing to main"), ("claim", "tests pass"), ("claim", "done")]
    assert rows[2]["extra"] == {"claim": "other"}                       # unknown claim kind kept as "other"
    assert retire == [7] and dropped == 2 and asks == []


async def test_exchange_rows_ask_lifecycle_retire_and_purpose(tmp_path):
    store = Store(tmp_path / "hub.db")
    _session(store)
    store.upsert_turns(KEY, [_turn(0, "Build the tracker. Never push without asking.", "Plan ready. Shall I start with the store, then stop?"),
                             _turn(1, "<bash-input>ls</bash-input>", "a b"),
                             _turn(2, "x", "task done", origin="task")], None)
    seat = Seat({"instructions": [{"text": "ask before pushing", "quote": "Never push without asking."}],
                 "asks": [{"text": "start with the store?", "quote": "Shall I start with the store"}],
                 "goal_changed": True, "purpose": "Build the tracker"})
    ex = _ex(store, seat)
    assert await ex.step(calls=5) == 1 and len(seat.prompts) == 1      # wrapper + task turns read past for free
    assert store.tracker_state(KEY)["through_idx"] == 2
    led = store.ledger(KEY)
    assert [(r["kind"], r["text"], r["turn_idx"], r["active"]) for r in led] == [("instruction", "ask before pushing", 0, True)]
    att = [a for a in store.attention() if a["item"] == "ask"]
    assert [(a["kind"], a["text"]) for a in att] == [("text", "start with the store?")]
    assert store.session(KEY)["purpose"] == "Build the tracker"     # ... and the <bash-input> after it didn't answer it

    # your next prompt answers the ask at once (ingest, not the tracker); the model then retires the instruction
    store.upsert_turns(KEY, [_turn(3, "yes. you may push freely now; also add docs", "Pushed.")], None)
    ask = next(a for a in store.session(KEY)["asks"] if a["ref"].startswith("llm:"))
    assert ask["answered"] == 1 and ask["answer"] == "yes. you may push freely now; also add docs"
    rid = led[0]["id"]
    seat.outs.append({"retire": [rid], "goal_changed": True, "purpose": "Build the tracker and its docs"})
    assert await ex.step() == 1
    assert f"[{rid}] ask before pushing" in seat.prompts[1]                # the model sees the active instruction with its id
    s = store.session(KEY)
    assert [r["active"] for r in s["ledger"]] == [False]
    assert s["purpose"] == "Build the tracker and its docs" and s["purpose_history"] == [{"purpose": "Build the tracker", "turn": 0}]
    assert await ex.step() == 0 and store.tracker_due(ex.cutoff()) == []    # caught up
    assert not store.apply_extraction(KEY, _turn(3, "y", "z"), [{"kind": "claim", "text": "dup", "quote": "dup"}], [], [], None)

    # a reset (transcript rewritten) forgets the ledger with the turns
    store.upsert_session({"key": KEY, "host_id": "laptop", "harness": "claude", "session_id": "s1", "reset": True, "last_activity": NOW})
    assert store.ledger(KEY) == [] and store.tracker_state(KEY)["through_idx"] == -1


async def test_hard_ask_wins_and_catch_up_closes_answered_asks(tmp_path):
    store = Store(tmp_path / "hub.db")
    _session(store)
    hard = {"kind": "plain", "text": "Which one?", "ref": "0:plain", "answered": False, "answer": None, "answered_at": None}
    store.upsert_turns(KEY, [_turn(0, "pick a db", "Postgres or sqlite. Which one?", asks=[hard]),
                             _turn(1, "go on", "Shall I add an index too? It would help."),
                             _turn(2, "yes add it", "Added.")], None)
    q = {"asks": [{"text": "which db?", "quote": "Postgres or sqlite. Which one?"}]}
    seat = Seat(q, {"asks": [{"text": "add an index?", "quote": "Shall I add an index too?"}]}, {})
    await _ex(store, seat).step(calls=3)
    llm = [a for a in store.session(KEY)["asks"] if a["ref"].startswith("llm:")]
    assert [(a["text"], a["answered"], a["answer"]) for a in llm] == [("add an index?", 1, "yes add it")]   # already answered


async def test_seat_down_backs_off_and_bad_replies_skip_the_exchange(tmp_path):
    store = Store(tmp_path / "hub.db")
    _session(store)
    store.upsert_turns(KEY, [_turn(0, "do it", "ok")], None)
    down = Seat(status=503)
    ex = _ex(store, down)
    assert await ex.step() == 0 and store.tracker_state(KEY)["through_idx"] == -1 and ex.status["last_error"]
    assert await ex.step() == 0 and len(down.prompts) == 1                  # backing off: no second call
    bad = Seat(*["not json"] * MAX_BAD)
    ex = _ex(store, bad)
    for _ in range(MAX_BAD):
        ex._backoff_until = 0
        await ex.step()
    assert store.tracker_state(KEY)["through_idx"] == 0 and ex.status["skipped"] == 1 and len(bad.prompts) == MAX_BAD


async def test_only_recent_or_live_sessions_are_tracked(tmp_path):
    store = Store(tmp_path / "hub.db")
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    _session(store, "laptop/claude/old", last_activity=old)
    _session(store, "laptop/claude/new")
    _session(store, "laptop/claude/live", last_activity=old)
    for k in ("laptop/claude/old", "laptop/claude/new", "laptop/claude/live"):
        store.upsert_turns(k, [_turn(0, "hi", "hello")], None)
        store.db.execute("UPDATE sessions SET last_activity=? WHERE key=?", (old if k != "laptop/claude/new" else NOW, k))
    store.set_liveness("laptop", [{"key": "laptop/claude/live", "status": "idle", "status_since": NOW, "pid": 1, "socket": None}])
    ex = _ex(store, Seat())
    assert store.tracker_due(ex.cutoff()) == ["laptop/claude/live", "laptop/claude/new"]   # live first; old one left alone


async def test_recap_sees_instructions_and_stores_reentry(tmp_path):
    store = Store(tmp_path / "hub.db")
    _session(store)
    store.upsert_turns(KEY, [_turn(0, "Never push without asking.", "ok")], None)
    store.apply_extraction(KEY, _turn(0, "", ""), [{"kind": "instruction", "text": "ask before pushing", "quote": "Never push"}],
                           [], [], "Build the tracker")
    prompt = recap_prompt(store.session(KEY))
    assert "Purpose: Build the tracker" in prompt and "- ask before pushing" in prompt

    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"recap": "It is building the tracker.", "reentry": "Building the tracker.\n\nYou said: ask before pushing.\n" + "x\n" * 5})}}]})
    r = Recapper(store, SeatConfig(), transport=httpx.MockTransport(handler), debounce=0)
    assert await r.step() == 1
    assert store.session(KEY)["reentry"].split("\n") == ["Building the tracker.", "You said: ask before pushing.", "x", "x"]


def test_api_retire_row(tmp_path):
    store = Store(tmp_path / "hub.db")
    _session(store)
    store.apply_extraction(KEY, _turn(0, "", ""), [{"kind": "instruction", "text": "use the pool", "quote": "use the pool"}], [], [], None)
    rid = store.ledger(KEY)[0]["id"]
    c = TestClient(Hub(store, token="t").app())
    H = {"Authorization": "Bearer t"}
    assert c.post(f"/v1/sessions/{KEY}/ledger/{rid}/retire").status_code == 401
    assert c.post(f"/v1/sessions/{KEY}/ledger/{rid}/retire", headers=H).json() == {"ok": True}
    assert c.post(f"/v1/sessions/{KEY}/ledger/{rid}/retire", headers=H).status_code == 404      # already retired
    row = c.get(f"/v1/sessions/{KEY}", headers=H).json()["ledger"][0]
    assert row["active"] is False and row["retired_by"] == "operator"


def test_decisions_merge_answered_asks_with_your_choices_in_turn_order():
    s = {"asks": [{"turn_idx": 4, "text": "push now?", "answered": 1, "answer": "yes"},
                  {"turn_idx": 1, "text": "which db?", "answered": 0, "answer": None}],
         "ledger": [{"kind": "decision", "turn_idx": 2, "text": "sqlite", "extra": {"question": "which db?"}, "active": True},
                    {"kind": "instruction", "turn_idx": 0, "text": "use the pool", "extra": {}, "active": True},
                    {"kind": "instruction", "turn_idx": 1, "text": "never push", "extra": {}, "active": False}]}
    assert fmt.decisions(s) == [(2, "which db?", "sqlite"), (4, "push now?", "yes")]
    assert fmt.decisions(s, 1) == [(4, "push now?", "yes")]
    assert [r["text"] for r in fmt.instructions(s)] == ["use the pool"]
