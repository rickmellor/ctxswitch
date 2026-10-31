"""Pool jobs nested under the session that launched them, and what everything cost: the header's second line, the
detail pane's SPEND section, `ctx show`, the $ screen and `ctx spend`.

The hub side of the contract is on another branch, so every test here feeds the client the contract's JSON directly
(a fake client, or the app's client replaced by one): nothing here touches a real Store. Everything must also
survive a hub that returns none of it — no spend line, no children, no crash."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from ctxswitch.client import fmt
from ctxswitch.client.hubclient import HubClient
from ctxswitch.client.tui import app as tui
from ctxswitch.common.config import Config

P1, C1, C2, ORPHAN = "laptop/claude/p1", "laptop/claude/c1", "laptop/claude/c2", "devbox/claude/orph"
TOK = {"input": 100, "output": 40, "cache_read": 900, "cache_creation": 60}     # 1100 all told


def _sess(key, *, parent="", status="idle", started="2026-10-30T09:00:00Z", spend=None, host="laptop", **kw):
    s = {"key": key, "host_id": host, "harness": "claude", "session_id": key.rsplit("/", 1)[-1],
         "title": "webapp@main work", "live_status": status, "status_since": started, "last_activity": started,
         "started_at": started, "repo_root": "/r/webapp", "git_branch": "main", "turn_count": 3, "meta": {},
         "open_asks": [], "parent_key": parent, "job": "", "job_status": "", "job_brief": ""}
    if spend is not None:
        s["spend"] = spend
    return {**s, **kw}


def _job(key, parent, brief, status="ok", turns=72, started="2026-10-30T09:01:00Z", usd=0.1, **kw):
    return _sess(key, parent=parent, started=started, title=f"pool job {key}", job=f"sa-{key.rsplit('/', 1)[-1]}",
                 job_status=status, job_brief=brief, job_turns=turns, spend={"usd": usd, "tokens": 1100, "model": ""},
                 **kw)


class FakeClient:
    """The hub as the contract describes it: the rows and reports a test hands it, and nothing else. Pass an
    Exception as ``reports`` and every spend call fails, as it does on a hub that has no /v1/spend."""

    def __init__(self, rows, reports=None, detail=None):
        self.rows, self.reports, self.detail = rows, reports if reports is not None else {}, detail or {}
        self.spend_calls: list[tuple] = []

    def sessions(self, host=None, live_only=False):
        return {"sessions": [dict(x) for x in self.rows], "change": 1}

    def session(self, key, turns=50):
        return self.detail.get(key)

    def attention(self, host=None):
        return {"items": []}

    def hosts(self):
        return [{"host_id": "laptop"}, {"host_id": "devbox"}]

    def spend(self, frm, to, host=None):
        self.spend_calls.append((frm, to, host))
        if isinstance(self.reports, Exception):
            raise self.reports
        return self.reports[min(len(self.spend_calls) - 1, len(self.reports) - 1)] if isinstance(self.reports, list) \
            else self.reports

    def viewed(self, key, at):
        pass

    def ws_url(self):
        return "ws://x/v1"


def _board(monkeypatch, rows, reports=None) -> tui.CtxApp:
    cfg = Config(); cfg.hub.token = "t"; cfg.collector.host_id = "laptop"
    monkeypatch.setattr(tui.tmux, "in_tmux", lambda: False)
    monkeypatch.setattr(tui.CtxApp, "listen_ws", lambda self: None)
    monkeypatch.setattr(tui.CtxApp, "join_tmux", lambda self: None)
    app = tui.CtxApp(cfg, None)
    app.client = FakeClient(rows, reports)
    return app


def _rows_of(t) -> list[tuple[str, str, str]]:
    return [(t.get_row_at(i)[0].plain, t.get_row_at(i)[3].plain, t.coordinate_to_cell_key((i, 0)).row_key.value)
            for i in range(t.row_count)]


# -- the board's rows -------------------------------------------------------------------------------------------
async def test_a_pool_job_nests_under_the_session_that_launched_it(monkeypatch):
    parent = _sess(P1, status="waiting", open_asks=[{"kind": "plain", "text": "Ship it?", "asked_at": "2026-10-30T09:05:00Z",
                                                     "answered": False}], spend={"usd": 1.23, "tokens": 2_400_000,
                                                                                  "model": "claude-opus-5-5"})
    late = _job(C1, P1, "job1-runtime.md", started="2026-10-30T09:03:00Z")
    early = _job(C2, P1, "brief-two.md", status="error:timeout", turns=4, started="2026-10-30T09:02:00Z", usd=None)
    app = _board(monkeypatch, [_sess(ORPHAN, parent="laptop/claude/gone"), parent, late, early])
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(1)
        rows = _rows_of(app.query_one("#table"))
        assert [k for _, _, k in rows] == [ORPHAN, P1, C2, C1]          # children after their parent, by started_at
        assert [n for n, _, _ in rows] == ["1", "2", "", ""]            # a pool job never gets a number
        assert app.slots == [ORPHAN, P1]                                # Alt+N skips them entirely
        _, early_label, _ = rows[2]
        assert early_label.startswith("  ↳ ") and " cc·laptop " in early_label   # indented, then reach, then host
        assert early_label.endswith("brief-two.md · error:timeout · 4 turns 1.1k tok")  # no estimate: tokens say it
        assert rows[3][1].endswith("~$0.10")                             # the cost mark ends the row
        assert "job1-runtime.md" in rows[3][1] and "pool job" not in rows[3][1]   # the brief, not the title
        assert rows[1][1].endswith("\n└ Ship it?")                       # the waiting line reads └, not ↳


async def test_a_child_whose_parent_isnt_on_the_list_is_top_level(monkeypatch):
    """Filtered out, ended, or unknown: it gets its own number rather than vanishing under nothing."""
    app = _board(monkeypatch, [_job(ORPHAN, "laptop/claude/filtered-out", "x.md")])
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        rows = _rows_of(app.query_one("#table"))
        assert [n for n, _, _ in rows] == ["1"] and app.slots == [ORPHAN]
        assert "↳" not in rows[0][1] and " cc·laptop " in rows[0][1] and rows[0][1].endswith("x.md · ok · 72 turns ~$0.10")


async def test_an_old_hub_with_none_of_the_new_fields_still_boards(monkeypatch):
    """No parent_key / job / spend anywhere: no spend line, no nesting, nothing crashes."""
    plain = {"key": P1, "host_id": "laptop", "harness": "claude", "session_id": "p1", "title": "webapp@main work",
             "live_status": "idle", "status_since": "2026-10-30T09:00:00Z", "last_activity": "2026-10-30T09:00:00Z",
             "started_at": "2026-10-30T09:00:00Z", "repo_root": "/r/webapp", "git_branch": "main", "meta": {},
             "open_asks": [{"kind": "plain", "text": "Ship it?", "asked_at": "2026-10-30T09:05:00Z", "answered": False}]}
    app = _board(monkeypatch, [plain], reports=ValueError("HTTP 404"))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        head = app.query_one("#summary").render().plain.splitlines()
        assert len(head) == 2 and head[1] == "today —"                  # two lines, the cost unknown
        rows = _rows_of(app.query_one("#table"))
        assert [n for n, _, _ in rows] == ["1"] and rows[0][1].endswith("\n└ Ship it?")
        assert "SPEND" not in app.query_one("#detail_text").render().plain


async def test_a_refresh_that_cant_reach_spend_still_refreshes(monkeypatch):
    """The spend call rides in the same thread as the rest: an exception there costs the header line, nothing else."""
    app = _board(monkeypatch, [_sess(P1)], reports=ValueError("HTTP 404"))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert app.query_one("#table").row_count == 1 and app.spend is None
        assert app.client.spend_calls and len(app.client.spend_calls) == 1     # one window per refresh, no retries


def test_the_header_line_two_sums_up_today():
    one = {"total_usd": 4.12, "cloud": {"usd": 3.64, "by_account": [{"account": "bedrock", "usd": 3.64}]},
           "local": {"energy_usd": 0.48, "tokens": 17_300_000, "requests": 342}}
    two = {"total_usd": 12.4, "cloud": {"usd": 12.4, "by_account": [{"account": "bedrock", "usd": 11.1},
                                                                    {"account": "max", "usd": 1.3}]},
           "local": {"energy_usd": None, "error": "ConnectionError: refused"}}
    assert fmt.header_spend(one) == "today $4.12 · cloud 3.64 · local 0.48"
    assert fmt.header_spend(two) == "today $12.40 · cloud 12.40 (bedrock 11.10 · max 1.30) · local —"
    assert fmt.header_spend({"total_usd": 0.0, "cloud": {"usd": 0.0, "by_account": []},
                             "local": {"energy_usd": None, "source": None}}) == "today $0.00 · cloud 0.00 · local —"
    assert fmt.header_spend(None) == "today —"                          # not fetched yet, or no such route
    rows = [{"key": "a", "live_status": "idle"}, {"key": "b", "live_status": "waiting",
                                                 "open_asks": [{"kind": "plain", "text": "?"}]}]
    assert tui.summary_text(rows, "laptop", one).plain.splitlines()[1] == "today $4.12 · cloud 3.64 · local 0.48"
    assert tui.summary_text(rows, None).plain.splitlines() == ["ctx · all · 2 live · 1 waiting on you", "today —"]


async def test_the_header_fetches_today_once_per_refresh(monkeypatch):
    rep = {"total_usd": 2.0, "cloud": {"usd": 2.0, "by_account": []}, "local": {"energy_usd": 0.5}}
    app = _board(monkeypatch, [_sess(P1)], reports=rep)
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert app.query_one("#summary").render().plain.splitlines()[1] == "today $2.00 · cloud 2.00 · local 0.50"
        assert app.spend is rep
        frm, to, host = app.client.spend_calls[0]
        midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        assert (host, frm[:10], frm < to) == (None, midnight.isoformat()[:10], True)   # from = local midnight


# -- fmt's money words -------------------------------------------------------------------------------------------
def test_money_and_tokens_say_the_same_thing_everywhere():
    assert fmt.money(1.234) == "$1.23" and fmt.money(0) == "$0.00" and fmt.money(None) == "—"
    assert fmt.tokens_short(17_300_000) == "17.3M" and fmt.tokens_short(262_000) == "262k"
    assert fmt.tokens_short(41) == "41" and fmt.tokens_short(1_234) == "1.2k" and fmt.tokens_short(None) == "0"
    assert fmt.tok_total(TOK) == 1100
    assert fmt.job_label({"job": "sa-1", "job_brief": "b.md", "job_status": "ok", "job_turns": 72}) == "b.md · ok · 72 turns"
    assert fmt.job_label({"job": "sa-1", "job_status": "running"}) == "sa-1 · running"        # no brief, no turns yet
    assert fmt.job_label({"job": "sa-1", "job_brief": "b.md", "job_status": "ok"}) == "b.md · ok"
    assert fmt.job_label({"title": "not a job"}) == "not a job"
    assert fmt.link({"parent_key": "a"}, "parent_key") == "a"           # the hub's column …
    assert fmt.link({"meta": {"job": "sa-1"}}, "job") == "sa-1"        # … or, on an older hub, its session meta
    assert fmt.cost_mark({"spend": {"usd": 0.03}}) == " ~$0.03"
    assert fmt.cost_mark({"spend": {"tokens": 17_300_000}}) == " 17.3M tok"
    assert fmt.cost_mark({"job_tokens": TOK}) == " 1.1k tok" and fmt.cost_mark({}) == ""


def test_spend_line_covers_cloud_local_and_pool_jobs():
    s = {"spend": {"usd": 1.23, "tokens": 2_400_000, "model": "claude-opus-5-5", "account": "bedrock",
                   "local": {"energy_usd": 0.48, "tokens": 17_300_000, "requests": 342}}}
    assert fmt.spend_line(s) == ("cloud $1.23 · 2.4M tokens · claude-opus-5-5 · bedrock · "
                                 "local $0.48 energy · 17.3M tokens · 342 requests")
    assert fmt.spend_line({"spend": {"usd": None, "tokens": 2_400_000, "model": "saint-auto", "unpriced": True}}) \
        == "unpriced model saint-auto: 2.4M tokens"
    parent = {"spend": {"usd": 1.0, "tokens": 1000, "model": "claude-opus-5-5", "account": ""}}
    kids = [{"job": "sa-1", "job_brief": "b.md", "job_status": "ok", "job_turns": 3, "spend": {"usd": 0.02}},
            {"job": "sa-2", "job_brief": "c.md", "job_status": "ok", "spend": {"usd": 0.03}}]
    assert fmt.spend_line(parent, kids) == "cloud $1.00 · 1.0k tokens · claude-opus-5-5 · 2 pool jobs: $0.05 local est."
    assert fmt.spend_line({"spend": {"usd": None, "tokens": 0, "model": ""}}) == ""           # an old hub: nothing said
    assert fmt.spend_line({}, [{"job": "sa-1"}]) == "1 pool job"                              # the number, no estimates
    assert fmt.pool_job_lines(kids) == ["    ↳ b.md · ok · 3 turns ~$0.02", "    ↳ c.md · ok ~$0.03"]
    assert fmt.pool_job_lines([{"job": f"j{i}"} for i in range(9)], 6)[-1] == "    … 3 more"


def test_show_lines_adds_spend_and_the_jobs_it_launched():
    s = {"key": P1, "host_id": "laptop", "harness": "claude", "session_id": "p1", "meta": {}, "turns": [],
         "spend": {"usd": 1.23, "tokens": 2_400_000, "model": "claude-opus-5-5", "account": "bedrock"}}
    kids = [{"job": "sa-1", "job_brief": "b.md", "job_status": "ok", "job_turns": 72, "spend": {"usd": 0.03}}]
    lines = fmt.show_lines(s, 0, children=kids)
    assert "  spend : cloud $1.23 · 2.4M tokens · claude-opus-5-5 · bedrock" in lines
    i = lines.index("  pool jobs (1):")
    assert lines[i + 1] == "    ↳ b.md · ok · 72 turns ~$0.03"
    assert "spend" not in "\n".join(fmt.show_lines({**s, "spend": None}, 0))     # a hub that can't say: no line


def test_spend_lines_reads_a_report_as_the_screen_shows_it():
    def rep(total, accts, jobs, sessions, local=None, models=None):
        return {"window": {"from": "x", "to": "y"},
                "cloud": {"usd": total, "tokens": TOK, "by_account": accts, "by_model": models or []},
                "local": local or {"energy_usd": 0.48, "if_cloud_usd": 3.1, "tokens": 17_300_000, "requests": 342,
                                   "price_as": "claude-opus-5-5", "source": "saint", "error": None},
                "jobs": jobs, "sessions": sessions, "total_usd": total + 0.48, "unpriced_models": [], "rates": "config"}
    jobs = [{"job": "sa-20261030-1412-j1", "key": C1, "parent_key": P1, "parent_title": "OAuth login", "status": "ok",
             "brief": "oauth-tests.md", "started_at": "2026-10-30T14:12:00Z", "dur_s": 420, "turns": 72,
             "tokens": 900_000, "local_usd": 0.03, "if_cloud_usd": 1.4}]
    sess = [{"key": P1, "title": "OAuth login", "host_id": "laptop", "harness": "claude", "account": "bedrock",
             "usd": 1.23, "tokens": 2_400_000, "model": "claude-opus-5-5"}]
    text = "\n".join(fmt.spend_lines({"Today": rep(1.2, [{"account": "bedrock", "usd": 1.1, "tokens": 1, "sessions": 3},
                                                         {"account": "max", "usd": 0.1, "tokens": 2, "sessions": 1}],
                                                      jobs, sess),
                                      "7 days": rep(9.0, [{"account": "bedrock", "usd": 9.0, "tokens": 1, "sessions": 12}],
                                                    [], sess),
                                      "30 days": None}))
    assert "Today     $1.68   cloud 1.20 (bedrock 1.10 · max 0.10)" in text
    assert "7 days    $9.48   cloud 9.00" in text and "30 days   —" in text
    assert "local 0.48 energy · 17.3M tok · 342 requests" in text
    assert "POOL JOBS (1)" in text and "sa-20261030-1412-j1" in text and "OAuth login" in text
    assert "72 turns" in text and "7m" in text and "900k tok" in text and "~0.03" in text and "if-cloud 1.40" in text
    assert "SESSIONS (1)" in text and "$1.23" in text and P1 in text
    assert "rates: config" in text and "bedrock-spend" in text and "energy estimate from SAINT" in text
    down = rep(0.0, [], [], [], local={"energy_usd": None, "if_cloud_usd": None, "tokens": 0, "requests": 0,
                                       "price_as": "", "source": None, "error": "ConnectionError: refused"},
               models=[{"model": "saint-auto", "usd": 0.0, "tokens": 40_000, "priced": False}])
    t2 = "\n".join(fmt.spend_lines({"Today": down}))
    assert "local — energy" in t2 and "(ConnectionError: refused)" in t2
    assert "unpriced: saint-auto 40.0k tok" in t2 and "unpriced models (tokens only, never $0): saint-auto" in t2


# -- the $ screen -------------------------------------------------------------------------------------------------
def test_dollar_is_bound_to_the_spend_screen_and_documented():
    bound = {k.strip(): b.action for b in tui.CtxApp.BINDINGS for k in b.key.split(",")}
    assert bound["dollar_sign"] == "spend"
    assert ("dollar_sign",) in {keys for keys, _, _ in tui.HELP_KEYS}
    assert any("spend" in words for _, shown, words in tui.HELP_KEYS if shown == "$")
    assert "$" in tui.key_bar(tui.CtxApp.BINDINGS, 200).plain                    # and it shows in the key bar


async def test_the_spend_screen_loads_three_windows_and_closes(monkeypatch):
    def rep(n):
        return {"cloud": {"usd": float(n), "by_account": [{"account": "bedrock", "usd": float(n)}], "by_model": []},
                "local": {"energy_usd": None, "tokens": 0, "requests": 0, "error": None},
                "jobs": [] if n else [{"job": "sa-1", "parent_title": "t", "status": "ok", "turns": 1, "dur_s": 60,
                                       "tokens": 10, "local_usd": 0.01, "if_cloud_usd": 0.02}],
                "sessions": [], "total_usd": float(n), "unpriced_models": [], "rates": "default"}
    app = _board(monkeypatch, [_sess(P1)], reports=[rep(1), rep(0), rep(2), rep(3)])   # the first is the header's
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("dollar_sign"); await pilot.pause(0.8)
        assert isinstance(app.screen, tui.SpendScreen)
        text = app.screen.query_one("#spend_text").render().plain
        for title in ("Today", "7 days", "30 days"):
            assert title in text, text
        assert "sa-1" in text and len(app.client.spend_calls) >= 4               # the header's window, then three
        for close in (["escape"], ["q"], ["dollar_sign"]):
            await pilot.press(*close); await pilot.pause(0.3)
            assert not isinstance(app.screen, tui.SpendScreen), close
            if close != ["dollar_sign"]:
                await pilot.press("dollar_sign"); await pilot.pause(0.5)


async def test_the_spend_screen_on_a_hub_with_no_spend_route(monkeypatch):
    app = _board(monkeypatch, [_sess(P1)], reports=ValueError("HTTP 404"))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("dollar_sign"); await pilot.pause(0.8)
        text = app.screen.query_one("#spend_text").render().plain
        assert "Today" in text and "—" in text and "Traceback" not in text


def test_spend_windows_names_three_and_survives_one_failing():
    now, calls = datetime.now(timezone.utc), []

    class C:
        def spend(self, frm, to, host=None):
            calls.append((frm, to, host))
            if len(calls) == 2:
                raise ValueError("HTTP 404")
            return {"total_usd": 0.0}

    reports = tui.spend_windows(C(), "devbox")
    assert list(reports) == ["Today", "7 days", "30 days"] and reports["7 days"] is None
    assert [c[2] for c in calls] == ["devbox"] * 3
    assert calls[1][0] > (now - timedelta(days=8)).isoformat() and calls[2][0] < calls[1][0]   # wider, then wider


# -- the detail pane ----------------------------------------------------------------------------------------------
async def test_the_detail_shows_spend_and_the_jobs_under_the_selected_row(monkeypatch):
    detail = {**_sess(P1, spend={"usd": 1.23, "tokens": 2_400_000, "model": "claude-opus-5-5", "account": "bedrock",
                                 "local": {"energy_usd": 0.48, "tokens": 17_300_000, "requests": 342}}),
              "children_keys": [C1, C2], "turns": []}
    app = _board(monkeypatch, [{**detail, "children_keys": []}, _job(C1, P1, "b.md"), _job(C2, P1, "c.md", usd=None)],
                 reports={})
    app.client.detail = {P1: detail}
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert app.selected == P1                                    # the parent's row is the one selected
        t = app.query_one("#detail_text").render().plain
        assert "SPEND" in t and "cloud $1.23 · 2.4M tokens · claude-opus-5-5 · bedrock" in t
        assert "local $0.48 energy · 17.3M tokens · 342 requests" in t and "2 pool jobs" in t
        assert "POOL JOBS" in t and "↳ b.md · ok · 72 turns" in t and "↳ c.md · ok · 72 turns 1.1k tok" in t


async def test_the_detail_of_a_session_whose_children_only_the_hub_knows(monkeypatch):
    app = _board(monkeypatch, [{**_sess(P1), "children_keys": [C1]}], reports={})
    app.client.detail = {P1: {**_sess(P1), "children_keys": [C1], "turns": []},
                         C1: {**_job(C1, P1, "b.md"), "started_at": "2026-10-30T08:00:00Z"}}
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert app.selected == P1
        t = app.query_one("#detail_text").render().plain
        assert "1 pool job: $0.10 local est." in t and "↳ b.md · ok · 72 turns ~$0.10" in t


# -- ctx spend ----------------------------------------------------------------------------------------------------
def test_ctx_spend_prints_the_three_windows_or_json(monkeypatch, capsys):
    from ctxswitch.client import cli

    class C:
        def __init__(self):
            self.calls = []

        def spend(self, frm, to, host=None):
            self.calls.append((frm, to, host))
            return {"cloud": {"usd": float(len(self.calls)), "by_account": [], "by_model": []},
                    "local": {"energy_usd": None, "tokens": 0, "requests": 0, "error": None},
                    "jobs": [], "sessions": [], "total_usd": float(len(self.calls)), "unpriced_models": [],
                    "rates": "default"}

    c = C()
    monkeypatch.setattr(cli, "_client", lambda: c)
    assert cli.cmd_spend(SimpleNamespace(days=None, host=None, json=False)) == 0
    out = capsys.readouterr().out
    assert all(w in out for w in ("Today", "7 days", "30 days")) and len(c.calls) == 3
    assert cli.cmd_spend(SimpleNamespace(days=3, host="devbox", json=True)) == 0
    assert json.loads(capsys.readouterr().out)["cloud"]["usd"] == 4.0 and c.calls[-1][2] == "devbox"
    assert cli.cmd_spend(SimpleNamespace(days=14, host=None, json=False)) == 0 and "14 days" in capsys.readouterr().out
    gone = SimpleNamespace(spend=lambda f, t, h=None: (_ for _ in ()).throw(ValueError("HTTP 404")))
    monkeypatch.setattr(cli, "_client", lambda: gone)
    assert cli.cmd_spend(SimpleNamespace(days=None, host=None, json=True)) == 2
    assert "no spend report: HTTP 404" in capsys.readouterr().out


def test_the_client_asks_the_hub_for_one_window(monkeypatch):
    got = []

    class R:
        status_code = 200

        def json(self):
            return {"total_usd": 1.0}

    c = HubClient(Config())
    c._c = SimpleNamespace(get=lambda path, params=None: (got.append((path, params)), R())[1])
    assert c.spend("2026-10-30T00:00:00", "2026-10-30T12:00:00", "devbox") == {"total_usd": 1.0}
    assert got == [("/v1/spend", {"from": "2026-10-30T00:00:00", "to": "2026-10-30T12:00:00", "host": "devbox"})]
    c._c = SimpleNamespace(get=lambda path, params=None: SimpleNamespace(status_code=404, json=lambda: {}))
    try:
        c.spend("a", "b")
        raise AssertionError("a hub without the route must raise, like search() does")
    except ValueError as e:
        assert "HTTP 404" in str(e)


def test_a_jobs_cost_mark_prefers_its_local_estimate_and_shows_zero_as_zero():
    from ctxswitch.client import fmt
    assert fmt.cost_mark({"spend": {"local_usd": 0.0312, "usd": None, "tokens": 1_975_400}}) == " ~$0.03"
    assert fmt.cost_mark({"spend": {"local_usd": 0.0, "usd": None, "tokens": 5}}) == " ~$0.00"      # priced at nothing is still priced
    assert fmt.cost_mark({"spend": {"local_usd": None, "usd": 1.5, "tokens": 9}}) == " ~$1.50"
    assert fmt.cost_mark({"spend": {"local_usd": None, "usd": None, "tokens": 1_975_400}}) == " 2.0M tok"
    assert fmt.cost_mark({"job_tokens": {"input": 400, "output": 100, "cache_read": 0, "cache_creation": 0}}) == " 500 tok"
    assert fmt.cost_mark({}) == ""


async def test_a_nested_job_row_has_no_waiting_line_or_marks(tmp_path, monkeypatch):
    """A pool job under its parent is one line: its asks and unseen count belong to the job, not to you."""
    from ctxswitch.client.tui import app as tui
    from ctxswitch.common.config import Config
    parent = {"key": "laptop/claude/p", "host_id": "laptop", "harness": "claude", "live_status": "busy", "status_since": "2026-10-30T10:00:00Z",
              "started_at": "2026-10-30T09:00:00Z", "title": "parent", "cwd": "/r", "meta": {}, "open_asks": [], "parent_key": "", "job": ""}
    child = dict(parent, key="laptop/claude/c", title="Your working directory is …", parent_key="laptop/claude/p", job="sa-1-j1",
                 job_status="running", job_brief="b.md", started_at="2026-10-30T09:30:00Z", unseen=3,
                 open_asks=[{"kind": "plain", "text": "Which one?", "asked_at": "2026-10-30T10:00:00Z"}], spend={"local_usd": 0.02, "tokens": 10})
    cfg = Config(); cfg.hub.token = "t"
    monkeypatch.setattr(tui.tmux, "in_tmux", lambda: False)
    monkeypatch.setattr(tui.CtxApp, "listen_ws", lambda self: None)
    app = tui.CtxApp(cfg, None)
    monkeypatch.setattr(app.client, "sessions", lambda host=None, live_only=False: {"change": 1, "sessions": [parent, child]})
    monkeypatch.setattr(app.client, "attention", lambda host=None: {"items": []})
    monkeypatch.setattr(app.client, "hosts", lambda: [])
    monkeypatch.setattr(app.client, "spend", lambda *a, **k: None)
    monkeypatch.setattr(app.client, "session", lambda key, turns=50: dict(parent if key == parent["key"] else child, turns=[], asks=[], ledger=[], messages=[]))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        t = app.query_one("#table")
        rows = [t.get_row_at(i)[3].plain for i in range(t.row_count)]
        assert rows[0].startswith("▶") or "parent" in rows[0]
        kid = rows[1]
        assert "↳" in kid and "b.md · running" in kid and kid.endswith("~$0.02")
        assert "\n" not in kid and "◐" not in kid and "+3" not in kid                   # one line, no marks, no └
        assert t.rows[t.coordinate_to_cell_key((1, 0)).row_key].height == 1
        assert app.slots == ["laptop/claude/p"]                                         # Alt+1 is the parent only


def test_the_header_drops_the_account_brackets_before_it_drops_a_figure():
    from ctxswitch.client import fmt
    two = {"total_usd": 12.4, "cloud": {"usd": 12.4, "by_account": [{"account": "bedrock", "usd": 11.1}, {"account": "max", "usd": 1.3}]},
           "local": {"energy_usd": 0.48}}
    full = "today $12.40 · cloud 12.40 (bedrock 11.10 · max 1.30) · local 0.48"
    assert fmt.header_spend(two) == full and fmt.header_spend(two, len(full)) == full
    assert fmt.header_spend(two, len(full) - 1) == "today $12.40 · cloud 12.40 · local 0.48"   # too narrow: the brackets go, the figures stay
    assert fmt.header_spend(two, 20) == "today $12.40 · cloud 12.40 · local 0.48"
