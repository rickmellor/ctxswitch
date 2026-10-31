"""The Bedrock monthly budget on the board: the collector reads it from `bedrock-spend`, the hub keeps the latest per
account, and the client says what's left of the month in the header and on the $ screen."""
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from ctxswitch.client import fmt
from ctxswitch.client.tui import app as tui
from ctxswitch.collector import main as C
from ctxswitch.hub.api import Hub
from ctxswitch.hub.store import Store

H = {"Authorization": "Bearer t"}
NOW = datetime(2026, 10, 31, 0, 0, 0, tzinfo=timezone.utc)
QUOTA = {"monthly_cost_limit_usd": 2500.0, "enforcement_mode": "block", "expires_at": "2026-11-01T00:00:00+00:00",
         "currency": "USD", "daily_cost_limit_usd": None}
BILL = {"month": "2026-10", "cost_usd": 2255.5756, "currency": "USD"}


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch):
    """"once per distinct error" is per process: a test must not inherit the last one's memory."""
    monkeypatch.setattr(C, "_budget_warned", set())


class _P:
    def __init__(self, out, rc=0):
        self.stdout, self.returncode = out, rc


def _run(quota=QUOTA, bill=BILL, rc=0, boom=None):
    """A fake subprocess.run answering the two argv shapes, recording what it was asked for."""
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        if boom:
            raise boom
        doc = quota if "/quota/current-month" in argv else bill
        return _P(json.dumps(doc) if not isinstance(doc, str) else doc, rc)
    run.calls = calls
    return run


def _budget(**over) -> dict:
    d = {"account": "bedrock", "month": "2026-10", "limit_usd": 2500.0, "spent_usd": 2255.5756,
         "remaining_usd": 244.4244, "pct_used": 90.2, "enforcement": "block",
         "expires_at": "2026-11-01T00:00:00+00:00", "currency": "USD",
         "as_of": (NOW - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ"), "source": "bedrock-spend"}
    return {**d, **over}


# -- the collector's side ------------------------------------------------------------------------------------------

def test_read_budget_answers_the_bill_and_the_limit():
    d = C.read_budget("bedrock-spend", "bedrock", run=_run())
    assert d["account"] == "bedrock" and d["month"] == "2026-10"
    assert (d["limit_usd"], d["spent_usd"]) == (2500.0, 2255.5756)
    assert d["remaining_usd"] == pytest.approx(244.4244) and d["pct_used"] == 90.2
    assert d["enforcement"] == "block" and d["expires_at"] == "2026-11-01T00:00:00+00:00"
    assert d["currency"] == "USD" and d["source"] == "bedrock-spend"
    assert fmt.parse_iso(d["as_of"]) is not None            # an ISO instant of now, whenever now is


def test_read_budget_makes_no_more_than_two_calls_and_never_a_browser():
    run = _run()
    C.read_budget("bedrock-spend", "bedrock", run=run)
    assert [argv for argv, _ in run.calls] == [["bedrock-spend", "/quota/current-month", "--json"],
                                               ["bedrock-spend", "--json"]]
    for _, kw in run.calls:
        assert kw["env"]["BEDROCK_SPEND_BROWSER"] == "none" and kw["capture_output"] and kw["timeout"] == 60


def test_a_month_without_a_limit_shows_the_bill_against_nothing():
    d = C.read_budget("bedrock-spend", "bedrock", run=_run(quota={**QUOTA, "monthly_cost_limit_usd": None}))
    assert d["limit_usd"] is None and d["pct_used"] is None and d["remaining_usd"] is None
    assert d["spent_usd"] == 2255.5756                       # the bill is still the news


def test_a_budget_that_cannot_be_read_is_None_and_warns_once(caplog):
    for run in (_run(rc=1), _run(quota="not json"), _run(bill="{oops"),
                _run(boom=subprocess.TimeoutExpired("bedrock-spend", 60)),
                _run(bill={**BILL, "cost_usd": None}), _run(quota={"enforcement_mode": "block"})):
        assert C.read_budget("bedrock-spend", "bedrock", run=run) is None
    assert len(caplog.records) == 5                          # one per distinct way it can fail
    caplog.clear()
    for _ in range(3):                                       # the same failure on a hundred polls: no line after the first
        assert C.read_budget("bedrock-spend", "bedrock", run=_run(rc=1)) is None
    assert caplog.records == []


def test_read_budget_never_goes_below_zero_left():
    d = C.read_budget("bedrock-spend", "bedrock", run=_run(bill={**BILL, "cost_usd": 2600.0}))
    assert d["remaining_usd"] == 0.0 and d["pct_used"] == 104.0


async def test_the_budget_loop_polls_at_start_and_only_where_the_tool_exists(tmp_path, monkeypatch):
    import asyncio
    cfg = C.Config()
    cfg.collector.budget_cmd, cfg.collector.budget_every, cfg.collector.host_id = "bedrock-spend", 900, "laptop"
    cfg.collector.queue_path = str(tmp_path / "q.db")
    col = C.Collector(cfg)
    read = lambda cmd, account: (read.got.append((cmd, account)), {"account": account})[1]
    read.got = []
    monkeypatch.setattr(C, "read_budget", read)
    monkeypatch.setattr(C.shutil, "which", lambda c, path=None: "/usr/bin/" + c)
    slept = []

    async def nap(s):
        slept.append(s)
        raise asyncio.CancelledError
    monkeypatch.setattr(asyncio, "sleep", nap)
    with pytest.raises(asyncio.CancelledError):
        await col.budget_loop()
    assert read.got == [("/usr/bin/bedrock-spend", "bedrock")] and slept == [900]   # the first poll, right away, by its full path
    assert [kind for _, kind, _ in col.state.pending()] == ["budget"]

    read.got.clear(); monkeypatch.setattr(C.shutil, "which", lambda c, path=None: None)    # a host without the tool: no loop at all
    await col.budget_loop()
    assert read.got == [] and slept == [900]


# -- the hub's side ------------------------------------------------------------------------------------------------

def _hub(tmp_path):
    store = Store(tmp_path / "hub.db")
    return store, TestClient(Hub(store, token="t").app())


def _push(c, d, seq=1, host="laptop"):
    return c.post("/v1/ingest", json={"host_id": host, "events": [{"seq": seq, "kind": "budget", "payload": d}]},
                  headers=H)


def test_a_pushed_budget_rides_the_spend_report_and_the_latest_one_wins(tmp_path):
    store, c = _hub(tmp_path)
    assert _push(c, _budget()).status_code == 200
    b = c.get("/v1/spend", headers=H).json()["budget"]["bedrock"]
    assert (b["limit_usd"], b["spent_usd"], b["pct_used"], b["month"]) == (2500.0, 2255.5756, 90.2, "2026-10")
    assert store.budgets()["bedrock"]["remaining_usd"] == pytest.approx(244.4244)
    _push(c, _budget(spent_usd=100.0, remaining_usd=2400.0, pct_used=4.0), seq=2)   # a later poll replaces it
    assert [d["spent_usd"] for d in store.budgets().values()] == [100.0]
    _push(c, _budget(account="max", remaining_usd=8.0), seq=3)                      # per account, one each
    assert sorted(store.budgets()) == ["bedrock", "max"]


def test_health_shows_the_budget_only_to_whoever_has_the_token(tmp_path):
    store, c = _hub(tmp_path)
    _push(c, _budget())
    assert c.get("/v1/health").json()["budget"] is None
    h = c.get("/v1/health", headers=H).json()["budget"]["bedrock"]
    assert sorted(h) == ["as_of", "pct_used", "remaining_usd"] and h["pct_used"] == 90.2
    assert store.budgets()["bedrock"]["spent_usd"] == 2255.5756     # the full figure stays off the health route


def test_a_store_that_has_never_seen_a_budget_says_so_quietly(tmp_path):
    store, c = _hub(tmp_path)
    assert store.budgets() == {} and c.get("/v1/spend", headers=H).json()["budget"] == {}
    assert c.get("/v1/health", headers=H).json()["budget"] == {}


# -- the client's side ---------------------------------------------------------------------------------------------

def test_budget_line_reads_like_a_person_saying_it():
    assert fmt.budget_line(_budget(), NOW) == ("bedrock: $2255.58 of $2500.00 this month (90.2% used) · $244.42 left"
                                              " · 1 day left · blocks at the limit · as of 3m ago")
    assert fmt.budget_line(_budget(enforcement="alert"), NOW).endswith("alerts at the limit · as of 3m ago")
    assert fmt.budget_line(_budget(limit_usd=None, remaining_usd=None, pct_used=None), NOW).startswith(
        "bedrock: $2255.58 of — this month · no limit set · 1 day left")
    assert "2 days left" in fmt.budget_line(_budget(), NOW - timedelta(days=1))


def test_budget_line_says_ends_today_inside_a_day_and_stale_after_two_hours():
    assert "ends today" in fmt.budget_line(_budget(), NOW + timedelta(hours=12))    # half of that last day gone
    assert "ends today" in fmt.budget_line(_budget(), NOW + timedelta(days=3))      # the month's over: nothing to count
    assert "1 day left" in fmt.budget_line(_budget(), NOW - timedelta(hours=12))
    old = fmt.budget_line(_budget(as_of=(NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")), NOW)
    assert old.endswith("(stale)") and "as of 3h00 ago" in old
    assert "(stale)" not in fmt.budget_line(_budget(as_of=(NOW - timedelta(minutes=50)).isoformat()), NOW)


def test_header_budget_shows_the_account_with_the_least_left_in_whole_dollars():
    assert fmt.header_budget({}) == "" and fmt.header_budget(None) == ""
    assert fmt.header_budget({"bedrock": _budget(), "max": _budget(remaining_usd=1900.0)}) == " · $244 left"
    assert fmt.header_budget({"bedrock": _budget(), "max": _budget(remaining_usd=12.9)}) == " · $12 left"
    assert fmt.header_budget({"max": _budget(remaining_usd=None)}) == ""      # no limit, so nothing to say


def test_the_header_drops_the_brackets_first_and_the_budget_only_when_still_too_wide():
    rep = {"total_usd": 12.4, "cloud": {"usd": 12.4, "by_account": [{"account": "bedrock", "usd": 11.1},
                                                                    {"account": "max", "usd": 1.3}]},
           "local": {"energy_usd": 0.48}, "budget": {"bedrock": _budget()}}
    full = "today $12.40 · cloud 12.40 (bedrock 11.10 · max 1.30) · local 0.48 · $244 left"
    assert fmt.header_spend(rep) == full and fmt.header_spend(rep, len(full)) == full
    no_brackets = "today $12.40 · cloud 12.40 · local 0.48 · $244 left"
    assert fmt.header_spend(rep, len(full) - 1) == no_brackets          # the brackets first, the month stays
    assert fmt.header_spend(rep, len(no_brackets)) == no_brackets
    assert fmt.header_spend(rep, len(no_brackets) - 16) == "today $12.40 · cloud 12.40 · local 0.48"
    assert fmt.header_spend({**rep, "budget": {}}, 20) == "today $12.40 · cloud 12.40 · local 0.48"


def test_the_header_line_is_painted_by_the_worst_account():
    assert fmt.budget_style({}) == "dim" and fmt.budget_style(None) == "dim"
    assert fmt.budget_style({"a": _budget(pct_used=74.9)}) == "dim"
    assert fmt.budget_style({"a": _budget(pct_used=75)}) == "yellow"
    assert fmt.budget_style({"a": _budget(pct_used=89.9)}) == "yellow"
    assert fmt.budget_style({"a": _budget(pct_used=90)}) == "bold red"
    assert fmt.budget_style({"a": _budget(pct_used=10), "b": _budget(pct_used=91)}) == "bold red"
    rows = [{"key": "a", "live_status": "idle"}]
    assert tui.summary_text(rows, "laptop").plain.splitlines()[1].startswith("today —")
    t = tui.summary_text(rows, "laptop", {"total_usd": 1.0, "cloud": {}, "local": {}})
    assert t.spans[-1].style == "dim" and t.plain.splitlines()[1] == "today $1.00 · cloud — · local —"
    assert tui.summary_text(rows, "laptop", {"total_usd": 1.0, "cloud": {}, "local": {},
                                             "budget": {"bedrock": _budget()}}).spans[-1].style == "bold red"


def test_the_spend_screen_has_a_budget_section_under_the_windows():
    rep = {"window": {"from": "2026-10-24T00:00:00Z", "to": "2026-10-31T00:00:00Z"},
           "cloud": {"usd": 3.0, "tokens": 100, "by_account": [{"account": "bedrock", "usd": 3.0}], "by_model": []},
           "local": {"energy_usd": 0.4, "tokens": 1000, "requests": 3}, "jobs": [], "sessions": [],
           "total_usd": 3.4, "unpriced_models": [], "rates": "default", "budget": {"bedrock": _budget()}}
    lines = fmt.spend_lines({"7 days": rep})
    i = lines.index("BUDGET")
    assert lines[i - 1] == "" and lines[i + 1].startswith("  bedrock: $2255.58 of $2500.00 this month (90.2% used)")
    assert "POOL JOBS" in lines and lines.index("POOL JOBS") > i        # the windows, then the bill, then the detail
    assert "BUDGET" not in fmt.spend_lines({"7 days": {**rep, "budget": {}}})


def test_the_budget_command_is_found_in_home_bin_when_the_service_path_leaves_it_out(tmp_path, monkeypatch):
    """systemd --user's PATH has no ~/bin; the laptop's bedrock-spend is there, so the poll must still find it."""
    (tmp_path / "bin").mkdir()
    tool = tmp_path / "bin" / "fake-spend"
    tool.write_text("#!/bin/sh\n"); tool.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert C.find_budget_cmd("fake-spend") == str(tool)
    assert C.find_budget_cmd("no-such-spend-tool") is None
    assert C.find_budget_cmd("") is None
