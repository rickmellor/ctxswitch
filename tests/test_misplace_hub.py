"""The misplaced-prompt detector: the hub's fast deterministic matcher (what a hook asks before the agent sees the
prompt), the ``misplaced`` record and its reads, and the tracker's LLM check that runs after an exchange lands.

Host names are the neutral ones the repo uses (gpu-host / devbox / laptop); the matcher only ever looks at the words
a session's work is named by, so these fixtures name work, not people.
"""
import json

import httpx
from starlette.testclient import TestClient

from ctxswitch.common.config import SeatConfig
from ctxswitch.hub import misplace
from ctxswitch.hub.api import Hub
from ctxswitch.hub.store import Store
from ctxswitch.hub.tracker import extract as X
from ctxswitch.hub.tracker.extract import Extractor

H = {"Authorization": "Bearer t"}
NOW = "2026-10-28T10:00:00Z"
LATER = "2026-10-28T12:00:00Z"
ZEPHYR = "zephyr dmp-sdk west build for the a53 target"


def _sess(key, *, title="", repo="", remote="", branch="main", cwd=None, purpose="", recap="", host=None,
          last=NOW, live="idle", harness="claude", job="", ended=False):
    """A session as the hub holds it: live unless told otherwise, since a candidate is one you could have meant."""
    h, harness, sid = key.split("/")
    return {"key": key, "host_id": host or h, "harness": harness, "session_id": sid, "title": title,
            "cwd": cwd if cwd is not None else f"/home/dev/repos/{repo}",
            "repo_root": f"/home/dev/repos/{repo}" if repo else "", "repo_remote": remote, "git_branch": branch,
            "started_at": last, "last_activity": last, "job": job, "ended": ended,
            "live": "ended" if ended else live, "purpose": purpose, "recap": recap}


def _load(store, *sessions):
    """Upsert them, give the tracker's purpose and the recap to whoever has one, then say who is live — one
    liveness snapshot per host, since each one is authoritative for that whole host."""
    hosts: dict[str, list[tuple[str, str]]] = {}
    for s in sessions:
        purpose, recap, status = s.pop("purpose", ""), s.pop("recap", ""), s.pop("live", "idle")
        store.upsert_session(s)
        if purpose:
            store.db.execute("INSERT OR IGNORE INTO tracker(key,through_idx,purpose) VALUES(?,-1,?)", (s["key"], purpose))
        if recap:
            store.set_recap(s["key"], recap, NOW)
        hosts.setdefault(s["host_id"], []).append((s["key"], status))
    for host, items in hosts.items():
        store.set_liveness(host, [{"key": k, "status": st} for k, st in items if st != "ended"])


# -- the matcher itself --------------------------------------------------------------------------------------------

def test_a_fingerprint_is_the_words_a_sessions_work_is_named_by():
    fp = misplace.fingerprint(_sess("devbox/claude/z1", title="Zephyr DMP SDK plan", repo="zephyr-dmp-sdk",
                                    purpose="plan the west build"))
    assert {"zephyr", "dmp", "sdk", "dmp-sdk", "zephyr-dmp-sdk", "plan"} <= fp     # whole name, pieces, glued back
    assert {"the", "for", "build", "sessions", "test", "commit", "please"} <= misplace.STOPWORDS
    assert len(misplace.STOPWORDS) >= 120                                          # filler names no work at all
    assert "build" in misplace.STOPWORDS and "plan" in fp                           # the verb names no work, the title does


def test_a_fingerprint_reads_instructions_and_ignores_where_the_home_lives():
    s = _sess("devbox/claude/z1", repo="widgetry", cwd="/home/dev/worktrees/widgetry/.claude/worktrees/kconfig-br")
    fp = misplace.fingerprint(s)
    assert "widgetry" in fp and "kconfig-br" in fp
    assert not {"home", "dev", "worktrees", "repos"} & fp                # where it lives is not what it is about
    assert "devicetree" not in fp                                          # a path part says nothing about a rule
    assert "devicetree" in misplace.fingerprint(dict(s, instructions=["keep the devicetree overlays under boards/"]))


def test_a_prompt_about_another_sessions_repo_is_flagged():
    ctx = _sess("gpu-host/claude/c1", title="ctxswitch hub board", repo="ctxswitch", last=LATER)
    zap = _sess("devbox/claude/z9", title="Zephyr DMP SDK plan", repo="zephyr-dmp-sdk", last=NOW)
    out = misplace.check(ZEPHYR, ctx, [zap])
    assert out["verdict"] == "other" and out["target_key"] == "devbox/claude/z9"
    assert out["target_title"] == "Zephyr DMP SDK plan" and out["why"].startswith("mentions zephyr") and "dmp-sdk" in out["why"]
    assert misplace.where(zap) == "devbox · zephyr-dmp-sdk@main"
    assert misplace.check(ZEPHYR, zap, [ctx])["verdict"] == "ok"               # typed into the right window


def test_generic_and_short_prompts_are_never_flagged():
    ctx, zap = _sess("gpu-host/claude/c1", repo="ctxswitch"), _sess("devbox/claude/z9", repo="zephyr-dmp-sdk")
    assert misplace.check("please run the tests and commit it", ctx, [zap])["verdict"] == "ok"
    assert misplace.check("zephyr dmp west", ctx, [zap])["verdict"] == "ok"    # three words name no work
    assert misplace.skip("zephyr dmp west") and not misplace.skip(ZEPHYR)


def test_slash_commands_and_ctxswitch_headers_are_skipped_everywhere():
    from ctxswitch.collector import deliver                      # the two headers ctx delivers with
    assert deliver.HEADER.startswith("Message from the operator (sent with ctxswitch):")
    assert deliver.agent_header("devbox/claude/z9", "Zephyr plan").startswith("Message from another agent session")
    for p in ("/model sonnet", "/model sonnet and zephyr dmp west build", "", "   ",
              "Message from the operator (sent with ctxswitch):\n\nzephyr dmp west build now",
              "Message from another agent session, not from the operator (sent with ctxswitch).\nFrom: x",
              "zephyr dmp west"):
        assert misplace.skip(p), p
    assert not misplace.skip(ZEPHYR)


def test_two_candidates_that_match_equally_go_to_the_more_recent_one():
    ctx = _sess("gpu-host/claude/c1", repo="ctxswitch")
    older = dict(_sess("devbox/claude/o1", title="Alpha beta notes", repo="alpha-beta"), last_activity=NOW)
    newer = dict(_sess("laptop/claude/n2", title="Gamma delta notes", repo="gamma-delta"), last_activity=LATER)
    assert misplace.check("alpha beta gamma delta", ctx, [older, newer])["target_key"] == "laptop/claude/n2"
    assert misplace.check("alpha beta gamma delta", ctx, [newer, older])["target_key"] == "laptop/claude/n2"
    bigger = dict(newer, title="Gamma delta epsilon notes", purpose="gamma delta epsilon work")
    assert misplace.check("alpha beta gamma delta epsilon", ctx, [older, bigger])["target_key"] == "laptop/claude/n2"


def test_terms_two_sessions_share_are_not_evidence_against_anyone():
    ctx = _sess("gpu-host/claude/c1", repo="ctxswitch")
    a = dict(_sess("devbox/claude/a", title="Alpha beta notes", repo="alpha-beta"), purpose="alpha beta notes")
    b = dict(_sess("laptop/claude/b", title="Gamma delta notes", repo="gamma-delta"), purpose="gamma delta notes")
    both = dict(_sess("devbox/claude/both", title="Every one", repo="all-things"), purpose="alpha beta gamma delta")
    assert misplace.check("alpha beta gamma delta", ctx, [a, b, both])["verdict"] == "ok"


def test_a_term_this_session_shares_cannot_carry_the_call():
    ctx = dict(_sess("gpu-host/claude/c1", repo="ctxswitch"), purpose="the ctxswitch hub board")
    other = _sess("devbox/claude/z9", repo="zephyr-dmp-sdk", title="Zephyr DMP SDK plan")
    assert misplace.check("ctxswitch zephyr dmp west", ctx, [other])["verdict"] == "ok"


# -- the store's candidates ----------------------------------------------------------------------------------------

def test_candidates_are_live_sessions_on_any_host_without_workers_shells_or_this_one(tmp_path):
    store = Store(tmp_path / "h.db")
    _load(store, _sess("gpu-host/claude/me", repo="me"), _sess("devbox/claude/live", repo="a"),
          _sess("laptop/pi/gone", repo="b", ended=True), _sess("devbox/shell/s1", repo="c"),
          _sess("devbox/claude/worker", repo="d", job="sa-1"), _sess("laptop/claude/busy", repo="e", live="busy"))
    assert {c["key"] for c in store.misplace_candidates("gpu-host/claude/me")} == {"devbox/claude/live", "laptop/claude/busy"}
    assert "devbox/claude/live" not in {c["key"] for c in store.misplace_candidates("devbox/claude/live")}
    assert store.misplace_session("gpu-host/claude/me")["repo_root"].endswith("me")


def test_candidates_are_capped_at_the_most_recent_forty(tmp_path):
    store = Store(tmp_path / "h.db")
    _load(store, _sess("gpu-host/claude/me", repo="me"),
          *[_sess(f"devbox/claude/s{i}", repo=f"r{i}", last=f"2026-10-28T{i // 60:02d}:{i % 60:02d}:00Z")
            for i in range(45)])
    got = store.misplace_candidates("gpu-host/claude/me")
    assert len(got) == misplace.CANDIDATES and got[0]["key"] == "devbox/claude/s44"


# -- the API -------------------------------------------------------------------------------------------------------

def _hub(tmp_path):
    store = Store(tmp_path / "hub.db")
    return store, TestClient(Hub(store, token="t").app())


def _pair(store):
    _load(store, _sess("gpu-host/claude/c1", title="ctxswitch hub board", repo="ctxswitch"),
          _sess("devbox/claude/z9", title="Zephyr DMP SDK plan", repo="zephyr-dmp-sdk"))


def _check(c, key, prompt, pid="p-1"):
    return c.post("/v1/prompt-check", json={"key": key, "prompt": prompt, "prompt_id": pid, "cwd": "/r"}, headers=H)


def test_prompt_check_flags_the_prompt_once_and_says_where_the_work_lives(tmp_path):
    store, c = _hub(tmp_path)
    _pair(store)
    r = _check(c, "gpu-host/claude/c1", ZEPHYR).json()
    assert r["verdict"] == "other" and r["target_key"] == "devbox/claude/z9"
    assert r["target_title"] == "Zephyr DMP SDK plan" and r["target_where"] == "devbox · zephyr-dmp-sdk@main"
    assert r["why"].startswith("mentions zephyr") and "dmp-sdk" in r["why"]
    rows = store.misplaced_for("gpu-host/claude/c1")
    assert [(x["source"], x["verdict"], x["state"], x["target_key"]) for x in rows] == [("pre", "other", "open", "devbox/claude/z9")]
    assert rows[0]["prompt"] == ZEPHYR and rows[0]["at"].endswith("Z") and rows[0]["resolved_at"] is None
    assert _check(c, "gpu-host/claude/c1", ZEPHYR).json()["id"] == r["id"]      # the hook re-asks the same prompt_id
    assert len(store.misplaced_for("gpu-host/claude/c1")) == 1


def test_prompt_check_records_nothing_when_the_prompt_fits_or_the_key_is_unknown(tmp_path):
    store, c = _hub(tmp_path)
    _pair(store)
    for key, prompt in (("gpu-host/claude/c1", "rename the recap pane in the board"),
                        ("devbox/claude/z9", ZEPHYR),                          # its own work
                        ("gpu-host/claude/c1", "/model sonnet zephyr dmp west build"),
                        ("nope/claude/none", ZEPHYR)):                         # a key the hub has never heard of
        assert _check(c, key, prompt).json() == {"verdict": "ok"}, prompt
    assert store.misplaced_for("gpu-host/claude/c1") == [] and store.misplaced_open_counts() == {}


def test_prompt_check_uses_the_cwd_the_hook_is_sitting_in(tmp_path):
    """A session live but not indexed yet is a placeholder row with no repo and no cwd of its own; the hook knows
    where it runs, and that is what makes a prompt about this repo read as belonging here."""
    store, c = _hub(tmp_path)
    _load(store, _sess("devbox/claude/z9", title="Zephyr DMP SDK plan", repo="zephyr-dmp-sdk"))
    store.set_liveness("gpu-host", [{"key": "gpu-host/claude/c1", "status": "idle"}])   # the hub knows nothing of it
    prompt = "zephyr dmp west ctxswitch"
    here = c.post("/v1/prompt-check", json={"key": "gpu-host/claude/c1", "prompt": prompt, "prompt_id": "p-cwd",
                                           "cwd": "/home/dev/repos/ctxswitch-hub"}, headers=H).json()
    assert here == {"verdict": "ok"}
    lost = c.post("/v1/prompt-check", json={"key": "gpu-host/claude/c1", "prompt": prompt, "prompt_id": "p-lost",
                                           "cwd": "/home/dev/repos/other-thing"}, headers=H).json()
    assert lost["verdict"] == "other" and lost["target_key"] == "devbox/claude/z9"


def test_both_routes_need_the_token(tmp_path):
    _, c = _hub(tmp_path)
    assert c.post("/v1/prompt-check", json={"key": "x", "prompt": ZEPHYR}).status_code == 401
    assert c.post("/v1/misplaced/1/state", json={"state": "kept"}).status_code == 401


def test_the_operator_keeps_moves_or_resents_a_flagged_prompt(tmp_path):
    store, c = _hub(tmp_path)
    _pair(store)
    mid = _check(c, "gpu-host/claude/c1", ZEPHYR).json()["id"]
    assert c.post(f"/v1/misplaced/{mid}/state", json={"state": "kept"}, headers=H).json() == {"ok": True}
    assert store.misplaced_for("gpu-host/claude/c1") == []
    row = store.db.execute("SELECT state, resolved_at FROM misplaced WHERE id=?", (mid,)).fetchone()
    assert row["state"] == "kept" and row["resolved_at"].endswith("Z")
    assert c.post(f"/v1/misplaced/{mid}/state", json={"state": "moved"}, headers=H).json() == {"ok": False}
    assert c.post(f"/v1/misplaced/{mid}/state", json={"state": "sideways"}, headers=H).json() == {"ok": False}
    assert c.post("/v1/misplaced/999/state", json={"state": "resent"}, headers=H).status_code == 409
    assert store.set_misplaced_state(mid, "resent") is False                   # only an open row changes


def test_a_prompt_flagged_twice_by_its_text_is_one_row(tmp_path):
    store, _ = _hub(tmp_path)
    _load(store, _sess("gpu-host/claude/c1", repo="ctxswitch"))
    a = store.add_misplaced({"key": "gpu-host/claude/c1", "prompt": ZEPHYR, "source": "llm", "verdict": "drift"})
    assert a == store.add_misplaced({"key": "gpu-host/claude/c1", "prompt": ZEPHYR, "source": "llm", "verdict": "drift"})
    assert len(store.db.execute("SELECT id FROM misplaced").fetchall()) == 1
    assert store.misplaced_seen("gpu-host/claude/c1", ZEPHYR)
    assert not store.misplaced_seen("gpu-host/claude/c1", "rename the recap pane")


def test_the_reads_carry_the_flags_to_the_board(tmp_path):
    store, c = _hub(tmp_path)
    _pair(store)
    first = _check(c, "gpu-host/claude/c1", ZEPHYR, pid="p-a").json()["id"]
    second = store.add_misplaced({"key": "gpu-host/claude/c1", "prompt_id": "p-b", "prompt": "and the same again",
                                  "source": "llm", "verdict": "other", "target_key": "devbox/claude/z9",
                                  "target_title": "Zephyr DMP SDK plan", "why": "reads like its work",
                                  "confidence": 0.82, "at": "2026-10-28T11:00:00Z"})
    s = c.get("/v1/sessions/gpu-host/claude/c1", headers=H).json()
    assert [r["id"] for r in s["misplaced"]] == [second, first] and s["misplaced_open"] == 2
    assert sorted(s["misplaced"][0]) == ["at", "confidence", "id", "key", "prompt", "prompt_id", "resolved_at",
                                        "source", "state", "target_key", "target_title", "verdict", "why"]
    assert s["misplaced"][0]["prompt"] == "and the same again" and s["misplaced"][0]["confidence"] == 0.82
    row = next(x for x in c.get("/v1/sessions", headers=H).json()["sessions"] if x["key"] == "gpu-host/claude/c1")
    assert row["misplaced_open"] == 2

    items = [i for i in c.get("/v1/attention", headers=H).json()["items"] if i.get("kind") == "misplaced"]
    assert {i["ref"] for i in items} == {f"misplaced:{first}", f"misplaced:{second}"}
    mine = next(i for i in items if i["ref"] == f"misplaced:{second}")
    assert mine["item"] == "misplaced" and mine["text"] == "looks like it's for Zephyr DMP SDK plan"
    assert mine["key"] == "gpu-host/claude/c1" and mine["host_id"] == "gpu-host" and mine["harness"] == "claude"
    assert mine["live_status"] == "idle" and mine["asked_at"] == "2026-10-28T11:00:00Z"

    drift = store.add_misplaced({"key": "gpu-host/claude/c1", "prompt_id": "p-c", "prompt": "book the flights",
                                 "source": "llm", "verdict": "drift", "why": "nothing here does travel",
                                 "confidence": 0.9, "at": "2026-10-28T12:00:00Z"})
    items = [i for i in c.get("/v1/attention", headers=H).json()["items"] if i.get("kind") == "misplaced"]
    assert next(i for i in items if i["ref"] == f"misplaced:{drift}")["text"] == "off this session's purpose"
    c.post(f"/v1/misplaced/{drift}/state", json={"state": "kept"}, headers=H)
    assert {i["ref"] for i in c.get("/v1/attention", headers=H).json()["items"] if i.get("kind") == "misplaced"} == \
           {f"misplaced:{first}", f"misplaced:{second}"}


def test_an_ended_session_stops_asking_the_operator(tmp_path):
    store, c = _hub(tmp_path)
    _pair(store)
    _check(c, "gpu-host/claude/c1", ZEPHYR)
    assert len([i for i in c.get("/v1/attention", headers=H).json()["items"] if i.get("kind") == "misplaced"]) == 1
    store.set_liveness("gpu-host", [])
    assert [i for i in c.get("/v1/attention", headers=H).json()["items"] if i.get("kind") == "misplaced"] == []


# -- the tracker's seat check ---------------------------------------------------------------------------------------

KEY = "gpu-host/claude/c1"
OTHER = "devbox/claude/z9"
MSG = "zephyr dmp west build the a53 target now"
LEDGER = {"instructions": [], "retire": [], "decisions": [], "claims": [], "asks": [], "goal_changed": False, "purpose": ""}
FIT = {"verdict": "fits", "target_key": "", "confidence": 0.9, "reason": "its own work"}


class Seat:
    """Answers whichever schema the request asked for, counting the calls by the name it was asked under."""

    def __init__(self, fit=FIT, boom=False):
        self.fit, self.boom, self.calls = fit, boom, []

    def handler(self, req):
        body = json.loads(req.content)
        fmt = body["response_format"]["json_schema"]
        self.calls.append((fmt["name"], body["messages"][1]["content"], body["messages"][0]["content"], fmt["schema"]))
        if self.boom and fmt["name"] == "misplace":
            raise httpx.ConnectError("seat down")
        out = LEDGER if fmt["name"] == "ledger" else {**FIT, **(self.fit or {})}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(out)}}]})

    def n(self, name="misplace"):
        return len([c for c in self.calls if c[0] == name])


def _turn(idx=0, user="rename the recap pane header in the board", agent="Done, dimmer now.", origin="human"):
    return {"idx": idx, "started_at": NOW, "ended_at": NOW, "user_text": user, "user_origin": origin,
            "assistant_text": agent, "tally": {}, "asks": [], "events": [], "complete": True, "offset_end": idx * 10 + 5}


def _tracker(tmp_path, seat, *turns, others=True, name="h.db"):
    store = Store(tmp_path / name)
    _load(store, _sess(KEY, title="ctxswitch hub board", repo="ctxswitch"),
          *([_sess(OTHER, title="Zephyr DMP SDK plan", repo="zephyr-dmp-sdk")] if others else []))
    store.upsert_turns(KEY, list(turns) or [_turn()], None)
    return store, Extractor(store, SeatConfig(), transport=httpx.MockTransport(seat.handler))


async def test_the_tracker_flags_a_confident_other_with_its_target(tmp_path):
    seat = Seat({"verdict": "other", "target_key": OTHER, "confidence": 0.9, "reason": "zephyr work, not this board"})
    store, ex = _tracker(tmp_path, seat)
    assert await ex.step(calls=3) == 1
    assert seat.n("ledger") == 1 and seat.n() == 1                 # one exchange: one extraction, one check
    rows = store.misplaced_for(KEY)
    assert [(r["source"], r["verdict"], r["target_key"], r["target_title"]) for r in rows] == \
           [("llm", "other", OTHER, "Zephyr DMP SDK plan")]
    assert rows[0]["confidence"] == 0.9 and rows[0]["why"] == "zephyr work, not this board"
    _, asked, system, schema = seat.calls[1]
    assert "Zephyr DMP SDK plan" in asked and "ctxswitch" in asked and "devbox · zephyr-dmp-sdk@main" in asked
    assert "other sessions" in system.lower() and "fits" in str(schema)


async def test_a_fit_or_a_weak_answer_records_nothing(tmp_path):
    for n, fit in enumerate((FIT, {"verdict": "other", "target_key": OTHER, "confidence": 0.5, "reason": "maybe"},
                             {"verdict": "drift", "target_key": "", "confidence": 0.69, "reason": "sort of"}, None)):
        seat = Seat(fit)
        store, ex = _tracker(tmp_path, seat, name=f"h{n}.db")
        assert await ex.step(calls=3) == 1
        assert store.misplaced_for(KEY) == [] and seat.n() == 1


async def test_a_drift_needs_no_target_and_an_unknown_key_is_dropped(tmp_path):
    seat = Seat({"verdict": "drift", "target_key": "somewhere/claude/x", "confidence": 0.95, "reason": "nothing here travels"})
    store, ex = _tracker(tmp_path, seat, name="a.db")
    await ex.step(calls=3)
    assert [(r["verdict"], r["target_key"]) for r in store.misplaced_for(KEY)] == [("drift", "")]
    seat = Seat({"verdict": "other", "target_key": "somewhere/claude/x", "confidence": 0.95, "reason": "elsewhere"})
    store, ex = _tracker(tmp_path, seat, name="b.db")
    await ex.step(calls=3)
    assert [(r["verdict"], r["target_key"], r["target_title"]) for r in store.misplaced_for(KEY)] == [("other", "", "")]


async def test_the_tracker_asks_about_nothing_the_hook_already_answered(tmp_path):
    for n, turn in enumerate((_turn(user="/model sonnet and zephyr dmp west build now"),
                              _turn(user="Message from the operator (sent with ctxswitch):\n\nzephyr dmp west build now"),
                              _turn(user="Message from another agent session, not from the operator (sent with ctxswitch).\nFrom: x"),
                              _turn(user="ship it"), _turn(user="carry on", origin="task"))):
        seat = Seat()
        store, ex = _tracker(tmp_path, seat, turn, name=f"s{n}.db")
        await ex.step(calls=3)
        assert seat.n() == 0 and store.misplaced_for(KEY) == []


async def test_a_message_ctx_delivered_is_not_a_prompt_the_operator_typed(tmp_path):
    """A turn the hub matched to a message is ctx's delivery, not the operator typing into this window: whoever
    sent it knows where it was meant for, so this check stays out of it."""
    seat = Seat({"verdict": "other", "target_key": OTHER, "confidence": 0.95, "reason": "zephyr's work"})
    store, ex = _tracker(tmp_path, seat, _turn(user="ship it"))
    mid = store.add_message(KEY, MSG)
    store.set_message_status(mid, "gpu-host", "sent")              # the collector put it on the socket
    store.upsert_turns(KEY, [_turn(1, MSG, "On it.")], None)       # ... and it arrives as this turn's prompt
    turn = store.session(KEY)["turns"][1]
    assert turn["via"] == "ctx" and turn["user_origin"] == "human"   # the hub rewrote it as yours, but ctx brought it
    assert await ex.step(calls=3) == 2 and seat.n() == 0 and store.misplaced_for(KEY) == []   # "ship it" is too short


async def test_a_pre_check_row_already_means_no_second_opinion(tmp_path):
    seat = Seat({"verdict": "other", "target_key": OTHER, "confidence": 0.9, "reason": "again"})
    store, ex = _tracker(tmp_path, seat)
    store.add_misplaced({"key": KEY, "prompt_id": "p-1", "prompt": "rename the recap pane header in the board",
                         "source": "pre", "verdict": "other", "target_key": OTHER, "target_title": "Zephyr plan",
                         "why": "mentions recap, board"})
    assert await ex.step(calls=3) == 1
    assert seat.n() == 0 and len(store.misplaced_for(KEY)) == 1


async def test_a_seat_that_raises_costs_no_row_and_no_crash(tmp_path):
    seat = Seat(boom=True)
    store, ex = _tracker(tmp_path, seat)
    assert await ex.step(calls=3) == 1                             # the exchange was still read and extracted
    assert store.tracker_state(KEY)["through_idx"] == 0 and store.misplaced_for(KEY) == []
    assert seat.n("ledger") == 1 and seat.n() == 1


async def test_a_check_with_nothing_else_live_has_nothing_to_say(tmp_path):
    seat = Seat()
    store, ex = _tracker(tmp_path, seat, others=False)
    assert await ex.step(calls=3) == 1 and seat.n() == 0 and store.misplaced_for(KEY) == []


async def test_the_llm_check_can_be_switched_off(tmp_path, monkeypatch):
    assert X.MISPLACE_CHECK is True
    monkeypatch.setattr(X, "MISPLACE_CHECK", False)
    seat = Seat({"verdict": "other", "target_key": OTHER, "confidence": 0.9, "reason": "off it goes"})
    store, ex = _tracker(tmp_path, seat)
    assert await ex.step(calls=3) == 1 and seat.n() == 0 and store.misplaced_for(KEY) == []


# -- tuned on real sessions (2026-11-11): two sessions run from the home folder, so no repo or branch to go by ----------
def _home(key, title, prompts, recap=""):
    return {"key": key, "host_id": "laptop", "harness": "claude", "cwd": "/home/me", "repo_root": "", "repo_remote": "",
            "git_branch": "", "title": title, "purpose": "", "recap": recap, "instructions": [], "prompts": prompts,
            "last_activity": "2026-11-11T10:00:00Z", "live_status": "idle"}


def test_a_slug_title_and_a_hyphenated_prompt_word_match_by_their_parts():
    this = _home("laptop/claude/a", "ctx switch", ["add the board's cpu panel"])
    other = _home("laptop/claude/b", "zephyr-dmp-sdk-plan", ["finish the esp32 staging run"])
    r = misplace.check("why does the zephyr-dmp build pick the wrong sdk overlay for staging", this, [other])
    assert r["verdict"] == "other" and r["target_key"] == "laptop/claude/b", r
    assert "zephyr" in r["why"]


def test_what_the_operator_typed_to_a_session_is_part_of_its_fingerprint():
    other = _home("laptop/claude/b", "plan", ["run the conformance sweep on native_sim", "push the jitp onboarding fix"])
    assert {"conformance", "native_sim", "jitp", "onboarding"} <= misplace.fingerprint(other)
    this = _home("laptop/claude/a", "ctx switch", ["align the gpu panel bars"])
    r = misplace.check("the conformance sweep on native_sim fails the jitp onboarding test", this, [other])
    assert r["verdict"] == "other" and r["target_key"] == "laptop/claude/b"


def test_command_output_and_wrappers_are_never_checked():
    for p in ("<bash-stdout>  seats 3 free  </bash-stdout>", "<command-name>/model</command-name> pick opus please",
              "<task-notification> job finished with status ok </task-notification>"):
        assert misplace.skip(p), p


def test_where_names_a_repo_and_branch_or_else_the_folder():
    assert misplace.where({"host_id": "devbox", "repo_root": "/home/me/repos/webapp", "git_branch": "main"}) == "devbox · webapp@main"
    assert misplace.where({"host_id": "devbox", "repo_root": "/home/me/repos/webapp", "git_branch": "HEAD"}) == "devbox · webapp"
    assert misplace.where({"host_id": "laptop", "repo_root": "", "repo_remote": "", "cwd": "/home/me"}) == "laptop · ~"
    assert misplace.where({"host_id": "laptop", "repo_root": "", "repo_remote": "", "cwd": "/home/me/notes"}) == "laptop · ~/notes"
