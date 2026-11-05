"""The hub's board view model: GET /v1/board and GET /v1/board/{key}.

The proof these tests make is PARITY, not appearance. Every value the hub decides is asserted equal to what the
client's own rule returns for the same session dict — client/fmt.py's session_state, state_glyph, harness_label,
repo_label, cost_mark, pool_mark, pool_stale, can_message, reply_target, job_label, pool_line, since_viewed, and
client/tui/app.py's rules hub/board.py ports: waiting_on, _visible, _nest, summary_line, render_detail's data
choices. A row that drifts from the board's own logic fails here whatever it looks like.

Hosts are named laptop / gpu-host / devbox, as everywhere in the tests and docs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from starlette.testclient import TestClient

from ctxswitch.client import fmt
from ctxswitch.client.tui import app as tui
from ctxswitch.common.config import ClaudeProfile
from ctxswitch.common.schema import LIVE
from ctxswitch.hub import board as B
from ctxswitch.hub.api import Hub
from ctxswitch.hub.store import Store
from ctxswitch.hub.tracker.recap import anchor_of

NOW = datetime.now(timezone.utc).replace(microsecond=0)
ISO = lambda mins: (NOW - timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ")     # noqa: E731
REMOTE = "git@github.com:acme/webapp.git"
WS = "acme/webapp@main"
H = {"Authorization": "Bearer t"}
BUSY, WAIT, IDLE, PROF = "laptop/claude/h1", "laptop/claude/h2", "gpu-host/claude/h3", "gpu-host/claude/h4"
ENDED, JOB, PIROW, SH = "devbox/claude/h5", "laptop/claude/h6", "devbox/pi/h7", "laptop/shell/h8"


class _Clock(datetime):
    """hub/board.py's clock, frozen: every age and window in an answer is then exactly what the test computed."""

    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(B, "datetime", _Clock)


def _meta(key, host, mins, *, harness="claude", title="webapp work", profile=None, pool=None, ended=False,
          parent="", job="", brief="", status="", **extra):
    m = {"key": key, "host_id": host, "harness": harness, "session_id": key.split("/")[-1], "title": title,
         "cwd": f"/home/me/{key.split('/')[-1]}", "repo_root": "/home/me/webapp", "repo_remote": REMOTE,
         "git_branch": "main", "started_at": ISO(mins + 40), "last_activity": ISO(mins)}
    if ended:
        m["ended"] = True
    if profile:
        m["profile"] = profile
    if pool:
        m["pool"], m["pool_at"], m["pool_how"] = pool[0], pool[1], "resume"
    if parent:
        m.update(parent_key=parent, job=job, job_brief=brief, job_status=status)
    return m | extra


def _turn(idx, mins, user="add the rate limiter", agent="Middleware done; tests pass.", origin="human",
          complete=True, asks=None, usage=None, model="claude-opus-5-5", events=None, tally=None):
    return {"idx": idx, "started_at": ISO(mins + 5), "ended_at": ISO(mins), "user_text": user, "user_origin": origin,
            "assistant_text": agent,
            "tally": tally or {"edits": 2, "bash": 1, "reads": 0, "agents": 0, "other": 0, "errors": 0},
            "asks": asks or [], "events": events or [], "complete": complete, "offset_end": idx * 100 + 10,
            "usage": {"input": 1000, "output": 400, "cache_read": 9000,
                      "cache_creation": 200} if usage is None else usage,
            "model": model}


def _seed(store: Store) -> None:
    """One of every kind of row the board has to make a decision about, on laptop / gpu-host / devbox."""
    live: list[tuple[str, str, str, int]] = []

    def add(key, host, mins, *, live_status="idle", turns=None, **kw):
        store.upsert_session(_meta(key, host, mins, **kw))
        store.upsert_turns(key, turns or [_turn(0, mins)], None, ended=bool(kw.get("ended")))
        if not kw.get("ended"):                     # an ended session is not in its host's live snapshot
            live.append((key, host, live_status, mins))

    add(BUSY, "laptop", 4, live_status="busy", title="OAuth login", pool=("off", ISO(30)),
        turns=[_turn(0, 6), _turn(1, 4, "use the session store", "Wiring the callback…", complete=False)])
    add(WAIT, "laptop", 8, title="release notes",
        turns=[_turn(0, 9, "draft the notes", "Approved?", asks=[{"ref": "llm:0:0", "kind": "text", "text": "Approved?",
                                                                  "answered": False}])])
    add(IDLE, "gpu-host", 20, title="GPU node upgrade")
    add(PROF, "gpu-host", 30, title="shader pass", profile="max", pool=("on", ISO(30)))
    add(ENDED, "devbox", 400, title="CI cache", ended=True)
    add(JOB, "laptop", 12, title="oauth-tests.md", parent=BUSY, job="sa-1", brief="oauth-tests.md", status="ok",
        job_tokens={"input": 21_000, "output": 6_400, "cache_read": 1_900_000, "cache_creation": 48_000},
        turns=[_turn(0, 12, "brief: oauth-tests.md", "Both flows pass.",
                     usage={"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0})])
    add(PIROW, "devbox", 55, harness="pi", title="load test", pool=("on", ISO(90)))
    add(SH, "laptop", 3, harness="shell", title="~/scratch", status="shell",
        turns=[_turn(0, 4, "make test", "ok", origin="shell")])
    for host in ("laptop", "gpu-host", "devbox"):
        store.host_seen(host)
        store.set_liveness(host, [{"key": k, "status": st, "status_since": ISO(m), "pid": 4242, "socket": "/x",
                                   "tmux_at": "ctx:1.2" if k == BUSY else None}
                                  for k, h, st, m in live if h == host])
    for host, state in (("laptop", "on"), ("gpu-host", "on"), ("devbox", "off")):
        store.set_host_pool({"host_id": host, "state": state, "url": "http://saint:4000", "checked_at": ISO(1)})
    store.set_budget({"account": "bedrock", "month": "2026-10", "limit_usd": 2500.0, "spent_usd": 2255.5,
                      "remaining_usd": 244.4, "pct_used": 90.2, "enforcement": "block", "currency": "USD",
                      "expires_at": ISO(-40000), "as_of": ISO(3), "source": "bedrock-spend"})


def _hub(tmp_path) -> SimpleNamespace:
    store = Store(tmp_path / "hub.db")
    _seed(store)
    h = Hub(store, token="t")
    return SimpleNamespace(store=store, hub=h, client=TestClient(h.app(), headers=H))


def _display(top: list[dict], kids: dict[str, list[dict]]) -> list[dict]:
    """Board order: each top-level row followed by the children under it, newest launch last."""
    return [s for r in top for s in [r, *sorted(kids.get(r["key"], []), key=lambda c: c.get("started_at") or "")]]


def _rows(body) -> dict:
    return {r["key"]: r for r in _flat(body)}


def _flat(body) -> list[dict]:
    """Every session in the answer: the top-level rows and the pool jobs riding underneath them, in board order."""
    return [r for row in body["rows"] for r in [row, *row["children"]]]


# -- auth and parameters -------------------------------------------------------------------------------------------

def test_both_routes_need_the_bearer_token():
    anon = TestClient(Hub(Store(":memory:"), token="t").app())
    assert anon.get("/v1/board").status_code == 401
    assert anon.get("/v1/board/laptop/claude/x").status_code == 401


def test_a_bad_tz_is_a_400_on_both_routes_and_an_unknown_key_is_a_404(tmp_path):
    h = _hub(tmp_path)
    assert h.client.get("/v1/board", params={"tz": "Mars/Olympus"}).json() == {"error": "bad tz"}
    assert h.client.get("/v1/board/laptop/claude/h1", params={"tz": "Mars/Olympus"}).json() == {"error": "bad tz"}
    assert h.client.get("/v1/board/laptop/claude/h1", params={"tz": "+99:00"}).json() == {"error": "bad tz"}
    assert h.client.get("/v1/board", params={"tz": "+05:30"}).status_code == 200
    assert h.client.get("/v1/board", params={"tz": "America/Chicago"}).status_code == 200
    assert h.client.get("/v1/board/laptop/claude/nope").status_code == 404
    assert h.client.get("/v1/board/laptop/claude/nope").json() == {"error": "not found"}


# -- the rows: every value equals the client's own rule for the same session ---------------------------------------

def test_a_row_says_what_the_board_would_have_decided_for_it(tmp_path):
    parity = Store(tmp_path / "parity.db")
    _seed(parity)
    rows = parity.sessions(None, include_ended=True)
    Hub(parity)._price_jobs(rows)                       # what the route does to its own rows before it decides
    body = _hub(tmp_path).client.get("/v1/board", params={"ended": "1"}).json()
    by_key = _rows(body)
    assert set(by_key) == {s["key"] for s in rows}
    host_pools = parity.host_pools()
    for s in rows:
        r = by_key[s["key"]]                            # nested or not, every session is in the answer exactly once
        assert r["state"] == fmt.session_state(s)
        assert r["glyph"] == fmt.state_glyph(s)
        assert r["title"] == fmt.display_title(s)
        assert r["repo"] == fmt.repo_label(s)
        assert r["waiting_on"] == tui.waiting_on(s)
        assert r["cost_mark"] == fmt.cost_mark(s).strip()
        assert r["can_message"] == fmt.can_message(s)
        assert r["unseen"] == s["unseen"]
        assert r["open_asks"] == len(s["open_asks"])
        assert r["age_at"] == (s["status_since"] if s["live_status"] in LIVE else s["last_activity"])
        assert r["ended"] == bool(s["ended"])
        assert r["spend"] == s["spend"]
        assert r["last_turn"] == s["last_turn"]
        assert r["pool"] == {"state": fmt._pool_state(s), "host_state": fmt._host_state(s, host_pools),
                             "stale": fmt.pool_stale(s, host_pools), "mark": fmt.pool_mark(s, host_pools)[0],
                             "style": fmt.pool_mark(s, host_pools)[1]}
        target, why = fmt.reply_target(s)
        assert r["reply_target"] == (None if target is None else
                                     {"ref": target["ref"], "kind": target["kind"], "text": target["text"]})
        assert r["reply_why"] == (why or fmt.can_message(s))


def test_the_row_values_the_board_shows_side_by_side(tmp_path):
    r = _rows(_hub(tmp_path).client.get("/v1/board", params={"ended": "1"}).json())
    assert r[BUSY]["state"] == "busy" and r[BUSY]["glyph"] == "●" and r[BUSY]["title"] == "OAuth login"
    assert r[BUSY]["waiting_on"] is None and r[BUSY]["tmux_at"] == "ctx:1.2" and r[BUSY]["pid"] == 4242
    assert r[WAIT]["state"] == "waiting" and r[WAIT]["glyph"] == "◐"
    assert r[WAIT]["waiting_on"] == "Approved?"         # a plain ask says the question itself
    assert r[WAIT]["reply_target"] == {"ref": "llm:0:0", "kind": "text", "text": "Approved?"}
    assert r[WAIT]["reply_why"] is None and r[WAIT]["can_message"] is None
    assert r[WAIT]["ask_age"] == "9m"                   # the row's ◐<age>: how long that ask has waited
    assert r[IDLE]["state"] == "idle" and r[IDLE]["glyph"] == "○" and r[IDLE]["waiting_on"] is None
    assert r[PROF]["label"] == "max"                    # its own host named the account it runs under
    assert r[ENDED]["state"] == "ended" and r[ENDED]["glyph"] == "–"
    assert r[PIROW]["label"] == "pi" and r[PIROW]["pool"]["mark"] == ""      # the ⌂ badge is Claude's only
    assert r[PIROW]["pool"]["stale"] is True            # told on, its host's switch says off
    assert r[SH]["label"] == "sh" and r[SH]["can_message"] == "shells don't take messages"
    assert r[SH]["reply_target"] is None and r[SH]["reply_why"] == "shells don't take messages"
    assert r[BUSY]["pool"]["mark"] == "⌂" and r[BUSY]["pool"]["style"] == "bold yellow"   # stale either way round
    assert r[PROF]["pool"]["mark"] == "⌂" and r[PROF]["pool"]["style"] == "bold green"
    assert r[IDLE]["pool"]["mark"] == "" and r[IDLE]["pool"]["stale"] is False             # never told: silent


def test_an_idle_row_on_a_host_that_names_its_accounts_uses_the_default_account(tmp_path):
    """"profiles matter" is what a board with the [[claude_profiles]] table decides (config.profiles_matter); the
    hub's stand-in is that some session on that host carries a meta.profile — the account its collector named."""
    h = _hub(tmp_path)
    plain = "gpu-host/claude/plainy"
    h.store.upsert_session(_meta(plain, "gpu-host", 7, title="no profile of its own"))
    h.store.upsert_turns(plain, [_turn(0, 7)], None)
    row = _rows(h.client.get("/v1/board", params={"ended": "1"}).json())[plain]
    assert row["label"] == "default"                    # that host names its accounts: the unnamed one is the default
    assert row["label"] == fmt.harness_label(h.store.session(plain, turns=0),
                                             [ClaudeProfile(), ClaudeProfile(name="max", config_dir="~/.claude-max")])
    other = _rows(h.client.get("/v1/board", params={"host": "laptop", "ended": "1"}).json())[BUSY]
    assert other["label"] == "cc" == fmt.harness_label(h.store.session(BUSY, turns=0), [])
    # the decision belongs to the host, not to the board: naming an account on laptop makes laptop's unnamed
    # sessions "default" everywhere — filtered or not, in a detail card too
    named = "laptop/claude/named"
    h.store.upsert_session(_meta(named, "laptop", 6, title="a named account", profile="bedrock"))
    h.store.upsert_turns(named, [_turn(0, 6)], None)
    for rows in (_rows(h.client.get("/v1/board", params={"host": "laptop", "ended": "1"}).json()),
                 _rows(h.client.get("/v1/board", params={"ended": "1"}).json())):
        assert rows[named]["label"] == "bedrock" and rows[BUSY]["label"] == "default"
    assert _rows(h.client.get("/v1/board", params={"ended": "1"}).json())[PROF]["label"] == "max"
    assert B.detail(h.store, BUSY, prices={})["info"]["label"] == "default"

def test_ended_rows_hide_unless_asked_and_the_host_filter_filters(tmp_path):
    h = _hub(tmp_path)
    assert ENDED not in _rows(h.client.get("/v1/board").json())
    assert {r["key"] for r in _flat(h.client.get("/v1/board", params={"ended": "1"}).json())} >= {ENDED, BUSY, JOB}
    assert {r["key"] for r in _flat(h.client.get("/v1/board", params={"host": "gpu-host"}).json())} == {IDLE, PROF}


def test_q_matches_title_key_repo_or_last_prompt_exactly_as_the_board_does(tmp_path):
    h = _hub(tmp_path)
    all_rows = h.store.sessions(None, include_ended=True)
    for q in ("oauth", "WEBAPP", "make test", "h7", "zzz"):      # casefolded, like app._visible's .lower()
        got = {r["key"] for r in _flat(h.client.get("/v1/board", params={"q": q, "ended": "1"}).json())}
        want = {s["key"] for s in all_rows
                if q.casefold() in " ".join([fmt.display_title(s), s["key"], fmt.repo_label(s),
                                             (s.get("last_turn") or {}).get("user_text") or ""]).casefold()}
        assert got == want, q


def test_pool_jobs_nest_one_level_under_whoever_launched_them(tmp_path):
    h = _hub(tmp_path)
    kid2, grand = "laptop/claude/jobtwo", "laptop/claude/grandkid"
    for key, parent, mins in ((kid2, BUSY, 11), (grand, kid2, 10)):
        h.store.upsert_session(_meta(key, "laptop", mins, title=key, parent=parent, job=f"sa-{mins}",
                                     brief=f"{key}.md", status="ok"))
        h.store.upsert_turns(key, [_turn(0, mins, f"brief: {key}.md", "done")], None)
    body = h.client.get("/v1/board", params={"ended": "1"}).json()
    rows = _rows(body)
    kids = [k["key"] for k in rows[BUSY]["children"]]
    assert set(kids) == {JOB, kid2}
    assert kids == sorted(kids, key=lambda k: h.store.session(k, turns=0)["started_at"])     # by started_at
    assert rows[JOB]["nested"] is True
    label = fmt.job_label(h.store.session(JOB, turns=0))
    assert rows[JOB]["job_label"] == label == "oauth-tests.md · ok"
    assert rows[JOB]["title"] == fmt.display_title(h.store.session(JOB, turns=0))
    assert rows[JOB]["waiting_on"] is None and rows[JOB]["ask_age"] is None   # a job inherits its parent's marks
    assert rows[JOB]["pool_mark"] == ""
    assert grand in rows and grand not in {k["key"] for r in body["rows"] for k in r["children"]}   # one level only
    # and the rows come in the order app._nest makes them, from the same visible list
    vis = B.visible(h.store.sessions(None, include_ended=True), True, "")
    assert [r["key"] for r in _flat(body)] == [s["key"] for s in tui.CtxApp._nest(None, vis)[0]]


def test_a_pool_job_row_costs_what_the_sessions_route_says_it_does(tmp_path):
    store = Store(tmp_path / "hub.db"); _seed(store)
    saint = type("S", (), {"url": "http://saint:4000/usage/v1",
                           "window": lambda self, f, t: {"totals": {
                               "energy_cost_usd": 2.0,
                               "tokens_by_type": {"input": 1000, "output": 1000, "cache_read": 0,
                                                  "cache_creation": 0}}}})()
    c = TestClient(Hub(store, token="t", saint=saint).app(), headers=H)
    job_row = _rows(c.get("/v1/board", params={"ended": "1"}).json())[BUSY]["children"][0]
    listed = [s for s in c.get("/v1/sessions").json()["sessions"] if s["key"] == JOB][0]
    assert job_row["spend"]["local_usd"] == listed["spend"]["local_usd"] is not None
    assert job_row["cost_mark"] == fmt.cost_mark(listed).strip()


def test_the_screen_host_reports_its_switch_in_the_header(tmp_path):
    h = _hub(tmp_path)
    head = lambda **kw: h.client.get("/v1/board", params=kw).json()["header"]      # noqa: E731
    assert head(screen_host="devbox")["pool"] == "off"
    assert head(screen_host="laptop")["pool"] == "on"
    assert head()["pool"] is None                       # no screen host named: no tag
    assert head(screen_host="nowhere")["pool"] is None


# -- the header -----------------------------------------------------------------------------------------------------

def test_header_counts_are_app_summary_line_s_counts(tmp_path):
    h = _hub(tmp_path)
    body = h.client.get("/v1/board", params={"ended": "1"}).json()
    top = [r for r in body["rows"]]
    head = body["header"]
    txt = tui.summary_line(top, None).plain                        # the board's own first line says the same
    assert f"{head['live']} live" in txt
    assert (f"{head['waiting']} waiting on you" in txt) if head["waiting"] else "nothing waiting" in txt
    assert head["live"] == len(top) - sum(1 for s in top if fmt.session_state(s) == "ended")
    assert head["hosts"] == ["devbox", "gpu-host", "laptop"]
    assert head["change"] == body["change"] == h.store.change


def test_header_budget_comes_from_the_store_budgets(tmp_path):
    h = _hub(tmp_path)
    today = h.client.get("/v1/board").json()["header"]["spend_today"]
    assert today["budget_left_usd"] == 244                             # whole dollars, like fmt.header_budget
    assert today["budget_pct"] == 90.2
    assert today["budget_style"] == fmt.budget_style(h.store.budgets()) == "bold red"
    h.store.db.execute("DELETE FROM kv WHERE k LIKE 'budget:%'")
    today = h.client.get("/v1/board").json()["header"]["spend_today"]
    assert today["budget_left_usd"] is None and today["budget_style"] == "dim"


def test_spend_today_starts_at_local_midnight_in_the_tz_asked_for(tmp_path, monkeypatch):
    h = _hub(tmp_path)
    seen: list[tuple[str, str]] = []

    def fake(store, frm, to, host, prices, saint, price_as):
        seen.append((frm, to))
        return {"total_usd": 1.5, "cloud": {"usd": 1.0, "by_account": [{"account": "bedrock", "usd": 1.0}]},
                "local": {"energy_usd": 0.5}}

    monkeypatch.setattr(B, "spend_report", fake)
    for tz in ("America/Chicago", "+05:30"):
        today = h.client.get("/v1/board", params={"tz": tz, "host": "laptop"}).json()["header"]["spend_today"]
        zone = ZoneInfo(tz) if "/" in tz else B.zone(tz)
        assert seen[-1] == (today["from"], today["to"])                # what the header asked spend_report for
        frm = datetime.strptime(today["from"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        assert frm.astimezone(zone).strftime("%H:%M:%S") == "00:00:00"  # midnight where the operator is
        assert frm.astimezone(zone).date() == NOW.astimezone(zone).date()
        assert today["total_usd"] == 1.5 and today["cloud_usd"] == 1.0 and today["local_usd"] == 0.5
        assert today["by_account"] == [{"account": "bedrock", "usd": 1.0}]
    assert h.client.get("/v1/board").json()["header"]["spend_today"]["from"] == B.window(None)[0]  # tz unset: UTC


def test_the_route_answers_what_hub_board_board_computes(tmp_path):
    h = _hub(tmp_path)
    via = h.client.get("/v1/board", params={"ended": "1", "screen_host": "laptop", "q": "e"}).json()
    direct = B.board(h.store, ended=True, q="e", tz=None, screen_host="laptop", prices={},
                     price_jobs=h.hub._price_jobs)
    assert via == direct


# -- the detail card ------------------------------------------------------------------------------------------------

@pytest.fixture()
def rich(tmp_path):
    """One session with everything a detail card reads: recap, ledger, asks, messages (one sent, one answering an
    ask), a sibling with its own instruction, six turns, and the pool job it launched."""
    store = Store(tmp_path / "hub.db")
    key = "laptop/claude/deep"
    store.upsert_session({"key": key, "host_id": "laptop", "harness": "claude", "session_id": "deep",
                          "title": "deep work", "cwd": "/home/me/webapp", "repo_root": "/home/me/webapp",
                          "repo_remote": REMOTE, "git_branch": "main", "started_at": ISO(300), "last_activity": ISO(2),
                          "pool": "on", "pool_at": ISO(200), "pool_how": "resume"})
    turns = [_turn(i, 300 - i * 40, user=f"prompt {i}", agent=f"answer {i}") for i in range(6)]
    turns[5] = _turn(5, 2, user="which one?", agent="Token bucket?",
                     asks=[{"ref": "llm:5:0", "kind": "text", "text": "Token bucket or sliding window?",
                            "answered": False}])
    store.upsert_turns(key, turns, None)
    store.set_liveness("laptop", [{"key": key, "status": "waiting", "status_since": ISO(2), "pid": 7,
                                   "socket": "/x", "name": "deep work"}])
    store.set_host_pool({"host_id": "laptop", "state": "off", "checked_at": ISO(1)})
    store.apply_extraction(key, turns[4], [
        {"kind": "instruction", "text": "don't touch the billing endpoints", "quote": "q", "extra": {}},
        {"kind": "claim", "text": "Middleware done, tests pass", "quote": "q", "extra": {"claim": "tests_pass"}}],
        [], [], "Rate limiting the public API")
    store.db.execute("INSERT INTO asks(key,ref,kind,text,turn_idx,asked_at) VALUES(?,?,?,?,?,?)",
                     (key, "hook:perm:Bash:1", "permission", "allow Bash: npm test", 5, ISO(3)))
    store.set_recap(key, "Middleware in; waiting on you: bucket or window.", anchor_of(store.session(key, turns=6)),
                    "Rate limiting the API.\nYou said: billing is off limits.")
    mid = store.add_message(key, "keep it per key", "laptop")
    assert store.set_message_status(mid, "laptop", "sent")
    store.add_message(key, "yes to Bash, on the dialog", "laptop", "hook:perm:Bash:1")
    store.add_workstream_message(WS, "anyone free to review?", "devbox")
    sib = "devbox/claude/sib"
    store.upsert_session({"key": sib, "host_id": "devbox", "harness": "claude", "session_id": "sib", "title": "sib",
                          "cwd": "/home/me/webapp", "repo_root": "/home/me/webapp", "repo_remote": REMOTE,
                          "git_branch": "main", "started_at": ISO(100), "last_activity": ISO(10)})
    store.upsert_turns(sib, [_turn(0, 10)], None)
    store.apply_extraction(sib, _turn(0, 10), [{"kind": "instruction", "text": "don't push", "quote": "q",
                                                "extra": {}}], [], [], None)
    job = "laptop/claude/jobof"
    store.upsert_session({"key": job, "host_id": "laptop", "harness": "claude", "session_id": "jobof", "title": "t.md",
                          "cwd": "/home/me/webapp", "repo_root": "/home/me/webapp", "repo_remote": REMOTE,
                          "git_branch": "main", "started_at": ISO(50), "last_activity": ISO(20), "parent_key": key,
                          "job": "sa-9", "job_brief": "t.md", "job_status": "ok",
                          "job_tokens": {"input": 20000, "output": 6000, "cache_read": 100000, "cache_creation": 4000}})
    store.upsert_turns(job, [_turn(0, 20, "brief: t.md", "pass")], None)
    return store, key


def test_detail_has_every_section_with_the_right_shape(rich):
    store, key = rich
    full = store.session(key, turns=8)
    d = B.detail(store, key, turns=8, prices={})
    assert set(d) >= {"key", "title", "state", "glyph", "recap", "waiting_on", "reply_target", "spend", "pool_jobs",
                      "info", "purpose", "last_exchange", "since_viewed", "since_line", "messages",
                      "workstream_messages", "instructions", "claims", "decisions", "siblings", "earlier",
                      "workstream", "change"}
    assert d["key"] == key and d["title"] == fmt.display_title(full) == "deep work"
    assert d["state"] == fmt.session_state(full) == "waiting" and d["glyph"] == fmt.state_glyph(full) == "◐"
    assert d["recap"] == {"text": full["recap"], "reentry_lines": full["reentry"].splitlines(), "stale": False}
    assert d["recap"]["reentry_lines"] == ["Rate limiting the API.", "You said: billing is off limits."]
    opens = [a for a in full["asks"] if not a["answered"]]
    assert d["waiting_on"] == [{"ref": a["ref"], "kind": a["kind"], "text": a["text"], "asked_at": a["asked_at"]}
                               for a in opens]
    assert {a["kind"] for a in d["waiting_on"]} == {"text", "permission"}   # a question and a dialog
    ask, why = fmt.reply_target(full)
    assert d["reply_target"] == {"ref": ask["ref"], "kind": ask["kind"], "text": ask["text"]}
    assert ask["ref"] == "llm:5:0"                      # the oldest ask a quick reply can answer, not the dialog
    assert d["reply_why"] is None == why
    kids = [store.session(k, turns=0) for k in store.children(key)]
    assert [k["key"] for k in kids] == ["laptop/claude/jobof"]
    assert d["spend"]["line"] == fmt.spend_line(full, kids) and "1 pool job" in d["spend"]["line"]
    for f in ("usd", "tokens", "model", "account"):
        assert d["spend"][f] == full["spend"][f]
    assert d["spend"]["local_usd"] is None              # no SAINT to ask: "we didn't ask", not "it cost nothing"
    assert d["pool_jobs"] == [{"key": j["key"], "label": fmt.job_label(j), "cost_mark": fmt.cost_mark(j).strip(),
                               "status": j["job_status"]} for j in kids]
    assert d["pool_jobs"][0]["label"] == "t.md · ok"
    assert d["purpose"] == full["purpose"] == "Rate limiting the public API"
    assert d["workstream"] == full["workstream"] == WS and d["workstream_label"] == "webapp@main"
    assert d["workstream_messages"] == [{"id": m["id"], "body": m["body"]} for m in full["workstream_messages"]]
    assert [i["text"] for i in d["instructions"]] == [r["text"] for r in fmt.instructions(full)]
    assert d["instructions"][0]["n"] == 1 and set(d["instructions"][0]) == {"n", "id", "text", "turn_idx"}
    assert [(c["text"], c["turn_idx"]) for c in d["claims"]] == [(r["text"], r["turn_idx"]) for r in fmt.claims(full)]
    assert d["claims"][0]["glyph"] == fmt.CLAIM_GLYPH["tests_pass"] and d["claims"][0]["kind"] == "tests_pass"
    assert d["decisions"] == [{"question": q_, "answer": a} for _, q_, a in fmt.decisions(full)] == []
    assert d["info"]["short_key"] == fmt.short_key(full) and d["info"]["repo"] == fmt.repo_label(full)
    assert d["info"]["pool_line"] == fmt.pool_line(full, store.host_pools(), NOW)
    assert d["info"]["pool_line"].startswith("pool: on (told at resume,")
    assert "host switch: off" in d["info"]["pool_line"] and "stale" in d["info"]["pool_line"]
    assert d["info"]["turn_count"] == full["turn_count"] == 6 and d["info"]["reach_hint"] is None
    assert d["info"]["age_at"] == full["status_since"]                # live: how long it has been in this state
    assert d["info"]["started_at"] == full["started_at"] and d["info"]["cwd"] == full["cwd"]
    assert d["info"]["label"] == "cc" and d["info"]["permission_mode"] is None


def test_detail_last_exchange_and_earlier_come_from_the_same_turns(rich):
    store, key = rich
    full, d = store.session(key, turns=8), B.detail(store, key, turns=8, prices={})
    trs = full["turns"]
    you = next(x for x in reversed(trs) if x["user_origin"] in ("human", "peer", "shell"))
    said = next(x for x in reversed(trs) if x["assistant_text"])
    assert d["last_exchange"]["you"] == {"who": fmt.who(you), "at": you["started_at"], "text": you["user_text"]}
    assert d["last_exchange"]["agent"] == {"who": fmt.replied(said), "at": said["ended_at"],
                                          "text": said["assistant_text"]}
    assert d["last_exchange"]["you"]["who"] == "you" and d["last_exchange"]["agent"]["who"] == "agent"
    assert d["last_exchange"]["tally"] == {k: v for k, v in trs[-1]["tally"].items() if v}
    assert d["last_exchange"]["in_progress"] is (not trs[-1]["complete"])
    assert d["last_exchange"]["events"] == trs[-1]["events"] and d["last_exchange"]["origin"] == trs[-1]["user_origin"]
    assert [e["idx"] for e in d["earlier"]] == [4, 3, 2, 1, 0]        # app's turns[-7:-1]: the six before the last
    assert d["earlier"][0]["user_text"] == "prompt 4" and d["earlier"][-1]["idx"] == 0


def test_detail_messages_note_sender_and_the_ask_they_answer(rich):
    store, key = rich
    full, d = store.session(key, turns=8), B.detail(store, key, turns=8, prices={})
    asked = {a["ref"]: a for a in full["asks"]}
    assert len(d["messages"]) == len(fmt.messages(full)) == 2
    for got, m in zip(d["messages"], fmt.messages(full)):
        assert got["note"] == fmt.message_note(full, m)
        assert got["glyph"] == fmt.MSG_GLYPH[m["status"]]
        assert got["sender"] == (fmt.sender(m) or None) is None       # both are the operator's own
        assert got["re"] == (fmt.one_line(asked[m["ask_ref"]]["text"], 60) if m.get("ask_ref") in asked else None)
    assert [m["re"] for m in d["messages"]] == [None, "allow Bash: npm test"]
    assert "not in the transcript yet" in d["messages"][0]["note"]


def test_detail_siblings_and_since_viewed_match_the_clients_own_rules(rich):
    store, key = rich
    full, d = store.session(key, turns=8), B.detail(store, key, turns=8, prices={})
    assert [x["key"] for x in d["siblings"]] == [x["key"] for x in full["siblings"]]
    assert "devbox/claude/sib" in [x["key"] for x in d["siblings"]]   # the pool job it launched is one too: same repo
    for got, x in zip(d["siblings"], full["siblings"]):
        st = "ended" if x["ended"] and x["live_status"] not in LIVE else x["live_status"] or "unknown"
        assert got["state"] == st and got["glyph"] == fmt.GLYPH[st]
        assert got["age_at"] == (x["status_since"] if st in LIVE else x["last_activity"])
        assert got["instructions"] == x["instructions"]
        assert got["label"] == "cc" and got["harness"] == x["harness"]
    assert [x["instructions"] for x in d["siblings"] if x["key"] == "devbox/claude/sib"] == [["don't push"]]
    assert d["since_viewed"] is None == fmt.since_viewed(full) and d["since_line"] == ""
    store.mark_viewed(key, ISO(120))
    full2, d2 = store.session(key, turns=8), B.detail(store, key, turns=8, prices={})
    assert d2["since_viewed"] == fmt.since_viewed(full2) is not None
    assert d2["since_line"] == fmt.since_line(fmt.since_viewed(full2))
    assert d2["since_viewed"]["exchanges"] == full2["unseen"] > 0


def test_recap_is_stale_when_the_anchor_moved(rich):
    store, key = rich
    assert B.detail(store, key, turns=8, prices={})["recap"]["stale"] is False
    store.upsert_turns(key, [_turn(6, 1, user="and the tests?", agent="", complete=False)], None)
    full = store.session(key, turns=8)
    assert full["recap_anchor"] != anchor_of(full)
    assert B.detail(store, key, turns=8, prices={})["recap"]["stale"] is True
    other = "laptop/claude/plain"                       # a session with no recap isn't an "updating" one
    store.upsert_session(_meta(other, "laptop", 5, title="no recap"))
    store.upsert_turns(other, [_turn(0, 5)], None)
    assert B.detail(store, other, turns=8, prices={})["recap"] == {"text": "", "reentry_lines": [], "stale": False}


def test_detail_turns_and_tz_params(rich):
    store, key = rich
    c = TestClient(Hub(store, token="t").app(), headers=H)
    d = c.get(f"/v1/board/{key}", params={"turns": "2"}).json()
    assert d["key"] == key and [e["idx"] for e in d["earlier"]] == [4]      # app's turns[-7:-1] of those two turns
    assert c.get(f"/v1/board/{key}", params={"tz": "+05:30"}).status_code == 200
    assert c.get(f"/v1/board/{key}", params={"tz": "nonsense"}).json() == {"error": "bad tz"}
    assert c.get("/v1/board/laptop/claude/nothing").status_code == 404


# -- the module's own helpers ---------------------------------------------------------------------------------------

def test_zone_accepts_an_iana_name_an_offset_and_nothing_else():
    assert B.zone("America/Chicago") == ZoneInfo("America/Chicago")
    assert B.zone(None) is timezone.utc and B.zone("") is timezone.utc
    assert B.zone("UTC") == ZoneInfo("UTC")
    assert B.zone("+05:30").utcoffset(None) == timedelta(hours=5, minutes=30)
    assert B.zone("-08:00").utcoffset(None) == timedelta(hours=-8)
    assert B.zone("Chicago") is None and B.zone("5") is None and B.zone("+5:30") is None
    assert B.zone("+99:00") is None                     # an offset no zone runs on
    frm, to = B.window("+05:30")
    assert frm == B._today(B.zone("+05:30"))[0] and to.endswith("Z") and frm < to
    assert B.window("Mars/Olympus") is None


def test_the_ported_rules_are_the_clients_own(tmp_path):
    """What hub/board.py ports from app.py, checked against app.py on the same rows: not a re-derivation, the answer."""
    store = Store(tmp_path / "hub.db"); _seed(store)
    rows = store.sessions(None, include_ended=True)
    top, kids = B.nest(rows)
    ordered, nested = tui.CtxApp._nest(None, rows)
    assert [s["key"] for s in _display(top, kids)] == [s["key"] for s in ordered]
    assert {c["key"] for v in kids.values() for c in v} == nested
    assert [s["key"] for s in B.visible(rows, False, "")] == [s["key"] for s in rows if fmt.session_state(s) != "ended"]
    for s in rows:
        assert B.age_at(s) == (s["status_since"] if s["live_status"] in LIVE else s["last_activity"])
        assert B.harness_label(s, False) == fmt.harness_label(s, [])
        assert B.harness_label(s, True) in (fmt.harness_label(s, [ClaudeProfile()]), "default")
