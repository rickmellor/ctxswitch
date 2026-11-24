"""The board's local-pool panel (`P`): what this machine's own `saint-pool` and `pool-stats` say, and the three
edits that go through `saint-pool config set`.

Nothing here runs a command: every test hands the panel a fake runner that answers canned output and records the
argv it was given, so the exact `saint-pool config set KEY VALUE` an edit issues is part of what's checked, and so is
that the panel never writes a config file itself. The cases a real host can be in are all here — ON and reachable, ON
and unreachable, OFF, a saint-pool with no `config` subcommand yet, no `pool-stats`, no `saint-pool` at all."""
from __future__ import annotations

import json

from rich.text import Text

from ctxswitch.client.tui import app as tui
from ctxswitch.client.tui import pool_panel as pp
from ctxswitch.client.tui.pool_panel import Cmd, PoolTools
from ctxswitch.common.config import Config

TODAY = "2026-11-24"
STATUS_ON = "saint-pool: ON (http://gpu-host:4000, reachable)\n"
STATUS_ON_DOWN = "saint-pool: ON (http://gpu-host:4000, UNREACHABLE)\n"
STATUS_OFF = "saint-pool: OFF\n"
CONFIG_ON = json.dumps({"enabled": True, "reviewer": "qwen-max", "rounds": 2,
                        "applies_to": "construction", "skip_read_mostly": True})
CONFIG_OFF = json.dumps({"enabled": False, "reviewer": "qwen-plus", "rounds": 1,
                         "applies_to": "all", "skip_read_mostly": False})
REVIEWERS = json.dumps([{"name": "qwen-max", "cloud": "none", "verified": True},
                        {"name": "qwen-32b", "cloud": "ollama", "verified": False},
                        {"name": "qwen-plus", "cloud": "none", "verified": True}])
STATS = json.dumps({"window": {"since": TODAY, "until": "2026-11-25",
                               "pool": {"jobs_finished": 19, "ok": 17, "ok_rate": 89.5, "job_hours": 1.5,
                                        "verify_pass": 8, "verify_fail": 1, "asks": 2,
                                        "review": {"ok": 15, "needs-work": 4}}}})
NO_CONFIG = Cmd(2, "", "saint-pool: unknown command 'config' (on|off|status|--hook)")
GOOD = {"saint-pool status": STATUS_ON, "saint-pool config get --json": CONFIG_ON,
        "saint-pool config reviewers --json": REVIEWERS, "pool-stats --json": STATS}


class FakeTools:
    """The pool's commands as a host answers them: canned output per command, every argv recorded.

    ``answers`` maps what was asked to a :class:`Cmd`, or to a string that is stdout with rc 0. The key is the whole
    argv or any prefix of it ("pool-stats --json" answers every pool-stats call); anything not matched answers rc 0
    with nothing. A tool named in ``missing`` is reported as not on PATH.

    The settings are state, as they are on a real host: they start as whatever ``answers`` gave for
    ``saint-pool config get --json``, a successful ``config set KEY VALUE`` changes that one key, and the next read
    says what it now holds — so the panel can only be right by reading back. A string in ``sets`` is what every set
    refuses with, a dict refuses only the keys it names; either way nothing changes."""

    def __init__(self, answers=None, missing=(), sets=None):
        self.answers = dict(answers or {})
        self.missing = set(missing)
        self.sets = sets
        self.calls: list[list[str]] = []
        got = self.answers.get("saint-pool config get --json")
        raw = got if isinstance(got, str) else (got.out if isinstance(got, Cmd) and got.ok else "")
        try:
            self.settings = json.loads(raw) if raw.strip()[:1] == "{" else None
        except ValueError:
            self.settings = None

    def __call__(self, argv: list[str]) -> Cmd:
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["saint-pool", "config", "set"]:
            bad = self.sets.get(argv[3]) if isinstance(self.sets, dict) else self.sets
            if bad:
                return Cmd(2, "", str(bad))
            if self.settings is not None:
                self.settings[argv[3]] = _as_field(argv[4])
            return Cmd(0, "")
        if argv == ["saint-pool", "config", "get", "--json"] and self.settings is not None:
            return Cmd(0, json.dumps(self.settings))
        for n in range(len(argv), 0, -1):
            if " ".join(argv[:n]) in self.answers:
                got = self.answers[" ".join(argv[:n])]
                return Cmd(0, got) if isinstance(got, str) else got
        return Cmd(0, "")

    def have(self, tool: str) -> bool:
        return tool not in self.missing

    def sets_of(self, key: str) -> list[list[str]]:
        """Every `saint-pool config set <key> …` argv it was asked for, oldest first."""
        return [c for c in self.calls if c[:4] == ["saint-pool", "config", "set", key]]

    def last_set(self, key: str) -> list[str] | None:
        out = self.sets_of(key)
        return out[-1] if out else None

    def stats_since(self) -> str | None:
        """The day pool-stats was asked for, out of its --since."""
        for c in self.calls:
            if c and c[0] == pp.STATS_TOOL and "--since" in c:
                return c[c.index("--since") + 1]
        return None


def _as_field(value: str):
    """A `config set` value back into what the tool would store: on/off as a bool, digits as a number, the rest as
    words — which is how the same setting comes out of the JSON read."""
    if value in ("on", "off"):
        return value == "on"
    return int(value) if value.lstrip("-").isdigit() else value


def _tools(fake: FakeTools) -> PoolTools:
    return PoolTools(run=fake, which=fake.have)


def _fake(**kw) -> FakeTools:
    """A host with every tool installed answering ``GOOD``, with what the test writes over it. pool-stats is
    answered under today's date as well, so a board that asked for some other day would come up with no numbers."""
    answers = {**GOOD, "pool-stats --json --since " + pp.today(): STATS, **kw.get("answers", {})}
    return FakeTools(answers=answers, missing=kw.get("missing", ()), sets=kw.get("sets"))


def _snap(tools, since: str = TODAY) -> dict:
    return pp.read_pool(tools if isinstance(tools, PoolTools) else _tools(tools), since)


def _panel(app) -> list[str]:
    return app.query_one("#pool").render().plain.splitlines()


def _board(monkeypatch, tmp_path, tools: FakeTools, shown: str | None = None) -> tui.CtxApp:
    """A board whose pool commands are the ones the test wrote, keeping its own panel state in a file of its own."""
    cfg = Config(); cfg.hub.token = "t"; cfg.collector.host_id = "laptop"
    monkeypatch.setattr(tui, "POOL_PANEL_FILE", tmp_path / "pool-panel")
    monkeypatch.setattr(tui, "LIST_FILE", tmp_path / "list-rows")
    if shown is not None:
        (tmp_path / "pool-panel").write_text(shown + "\n")
    monkeypatch.setattr(tui.tmux, "in_tmux", lambda: False)
    monkeypatch.setattr(tui.CtxApp, "listen_ws", lambda self: None)
    monkeypatch.setattr(tui.CtxApp, "join_tmux", lambda self: None)

    class Client:
        def sessions(self, host=None, live_only=False):
            return {"sessions": [], "change": 1}

        def attention(self, host=None):
            return {"items": []}

        def hosts(self):
            return [{"host_id": "laptop"}]

        def spend(self, frm, to, host=None):
            return None

        def metrics(self, host=None):
            return {"hosts": {}}

        def viewed(self, key, at):
            pass

        def ws_url(self):
            return "ws://x/v1"

    app = tui.CtxApp(cfg, None)
    app.client = Client()
    app.pool_tools = _tools(tools)
    return app


def _style_of(t: Text, line: int, needle: str) -> str:
    """The style the panel put on ``needle`` in ``line``: the tightest span over it."""
    lines = t.plain.splitlines()
    at = sum(len(x) + 1 for x in lines[:line]) + lines[line].index(needle)
    inside = [s for s in t.spans if s.start <= at < s.end]
    return min(inside, key=lambda s: s.end - s.start).style if inside else ""


# -- reading a host's commands --------------------------------------------------------------------------------------
def test_status_lines_become_fields():
    assert pp.parse_status(STATUS_ON) == {"state": "on", "url": "http://gpu-host:4000", "reachable": True,
                                          "review_line": ""}
    assert pp.parse_status(STATUS_ON_DOWN)["reachable"] is False
    assert pp.parse_status(STATUS_OFF) == {"state": "off", "url": "", "reachable": None, "review_line": ""}
    down = pp.parse_status("saint-pool: ON (http://gpu-host:4000) — WARNING: SAINT not reachable right now")
    assert down["state"] == "on" and down["reachable"] is False
    assert pp.parse_status(STATUS_ON + "review: on (qwen-max, 2 rounds)\n")["review_line"] == "on (qwen-max, 2 rounds)"
    assert pp.parse_status("")["state"] == "" and pp.parse_status("usage: saint-pool on|off|status")["state"] == ""


def test_config_and_reviewers_and_stats_come_back_as_fields():
    assert pp.parse_settings(Cmd(0, CONFIG_ON)) == {"enabled": True, "reviewer": "qwen-max", "rounds": 2,
                                                    "applies_to": "construction", "skip_read_mostly": True}
    assert pp.parse_settings(NO_CONFIG) is None                       # a saint-pool with no config subcommand
    assert pp.parse_settings(Cmd(0, "not json")) is None
    assert pp.parse_settings(Cmd(0, '{"review": {"enabled": false, "rounds": 3}}')) == {"enabled": False, "rounds": 3}
    assert pp.parse_reviewers(Cmd(0, REVIEWERS))[0] == {"name": "qwen-max", "cloud": "none", "verified": True}
    assert pp.parse_reviewers(NO_CONFIG) is None and pp.parse_reviewers(Cmd(0, "[]")) == []
    st = pp.parse_stats(Cmd(0, STATS))
    assert st["jobs_finished"] == 19 and st["ok_rate"] == 89.5 and st["verify_fail"] == 1 and st["asks"] == 2
    assert st["review"] == {"ok": 15, "needs-work": 4}
    assert pp.parse_stats(Cmd(0, '{"window": {"pool": {}}}')) is None     # a window with nothing in it says nothing


def test_a_missing_tool_is_a_dim_line_not_an_error():
    """The whole snapshot, from hosts with the tools in various states of being installed."""
    snap = _snap(_fake())
    assert snap["readonly"] is False and snap["missing"] == [] and snap["stats"]["asks"] == 2
    assert snap["pool"]["state"] == "on" and snap["settings"]["reviewer"] == "qwen-max"
    no_stats = _snap(_fake(missing=[pp.STATS_TOOL]))
    assert no_stats["missing"] == [pp.STATS_TOOL] and no_stats["stats"] is None and no_stats["readonly"] is False
    no_tool = _snap(_fake(missing=[pp.POOL_TOOL]))
    assert no_tool["readonly"] is True and no_tool["missing"] == [pp.POOL_TOOL]
    assert no_tool["settings"] is None and no_tool["reviewers"] is None and no_tool["pool"]["state"] == ""
    assert no_tool["stats"]["jobs_finished"] == 19                      # pool-stats still says its part


def test_the_day_pool_stats_is_asked_for_is_today():
    tools = _fake()
    pp.read_pool(_tools(tools), pp.today())
    assert tools.stats_since() == pp.today() and len(pp.today()) == 10


def test_reading_the_pool_never_leaves_the_house_by_running_anything():
    """The one runner is the whole way out: nothing else can start a process, and it doesn't raise on a tool
    that isn't there — it answers rc 127, which is what the panel calls missing."""
    def never(argv):
        raise AssertionError(f"ran {argv}")
    assert pp.default_run(["/bin/this-tool-is-not-here-ctx-test"]).rc == 127
    snap = pp.read_pool(PoolTools(run=never, which=lambda t: None), TODAY)
    assert snap["readonly"] is True and snap["missing"] == [pp.POOL_TOOL, pp.STATS_TOOL]


# -- rendering ------------------------------------------------------------------------------------------------------
def _lines(snap, width: int = 60) -> list[str]:
    return pp.panel_text(snap, width).plain.splitlines()


def test_a_host_with_the_pool_on_says_the_switch_the_settings_the_reviewers_and_today():
    lines = _lines(_snap(_fake()), 100)
    assert len(lines) == 5
    assert lines[0].startswith("pool · ON · http://gpu-host:4000 · reachable")
    assert lines[1] == "review · on · qwen-max · 2 rounds · construction · skip read-mostly on"
    assert lines[2] == "reviewers qwen-max ✓ · qwen-32b (ollama) ✗ · qwen-plus ✓"   # a cloud reviewer says so
    assert lines[3].startswith("today · 19 jobs · ok 90% · 1.5 job-h · verify 8/1 · 2 asks")
    assert "review 15 ok" in lines[3] and "review 4 needs-work" in lines[3]      # verdicts, when reported
    assert lines[4] == pp.EDIT_KEYS


def test_review_off_and_a_tool_that_does_not_answer_are_said_in_words():
    off = _lines(_snap(_fake(answers={"saint-pool status": STATUS_OFF, "saint-pool config get --json": CONFIG_OFF})), 100)
    assert off[0].startswith("pool · OFF") and "http://" not in off[0]
    assert off[1] == "review · off · qwen-plus · 1 round · all · skip read-mostly off"
    down = _lines(_snap(_fake(answers={"saint-pool status": STATUS_ON_DOWN})))
    assert down[0].startswith("pool · ON · http://gpu-host:4000 · UNREACHABLE")
    old = _lines(_snap(FakeTools(answers={"saint-pool status": STATUS_ON})))     # no config subcommand at all
    assert "config get: no answer" in old[1] and "config reviewers: no answer" in old[2]
    assert old[3] == f"{pp.STATS_TOOL}: nothing to report today"
    part = _lines(_snap(_fake(answers={"saint-pool config get --json": '{"enabled": true}'})))
    assert part[1] == "review · on · — · — rounds · — · skip read-mostly —"      # a field it lacks is a dash


def test_a_host_with_no_pool_stats_gets_one_dim_line_and_nothing_else_changes():
    lines = _lines(_snap(_fake(missing=[pp.STATS_TOOL])))
    assert len(lines) == 5 and lines[3] == f"{pp.STATS_TOOL} not on PATH — no job numbers here"
    assert lines[1].startswith("review · on")                                   # what saint-pool can say is there


def test_a_host_with_no_saint_pool_says_so_and_is_read_only():
    lines = _lines(_snap(_fake(missing=[pp.POOL_TOOL])))
    assert lines[0].startswith(f"pool · {pp.POOL_TOOL} not on PATH")
    assert lines[1] == f"{pp.POOL_TOOL} config: nothing to read, and nothing to set"
    assert lines[-1] == pp.READ_ONLY_MSG and lines[-2].startswith("today ·")   # pool-stats still said its part


def test_a_status_that_failed_says_what_the_tool_answered():
    tools = FakeTools(answers={"saint-pool status": Cmd(1, "", "curl: (7) failed to connect")})
    assert "curl: (7) failed to connect" in _lines(_snap(tools))[0]


def test_a_status_with_its_own_review_line_shows_it():
    tools = FakeTools(answers={**GOOD, "saint-pool status": STATUS_ON + "review: on (qwen-max, 2 rounds)\n"})
    lines = _lines(_snap(tools))
    assert lines[1] == "review: on (qwen-max, 2 rounds)" and len(lines) == 6


def test_an_edit_that_was_refused_shows_up_on_the_panel():
    lines = _lines(dict(_snap(_fake()), error="saint-pool: rounds must be 1..5"))
    assert "config set: saint-pool: rounds must be 1..5" in lines
    assert lines[-1] == pp.EDIT_KEYS                                            # and it still says its keys


def test_no_line_is_ever_wider_than_the_pane():
    for fake in (_fake(), _fake(missing=[pp.STATS_TOOL]), _fake(missing=[pp.POOL_TOOL]),
                 _fake(answers={"saint-pool status": STATUS_OFF, "saint-pool config get --json": CONFIG_OFF})):
        for width in (30, 44, 60, 100):
            lines = _lines(_snap(fake), width)
            assert all(len(ln) <= width for ln in lines), (width, lines)
    assert "more" in _lines(_snap(_fake()), 30)[2]                # too narrow for the names: it drops whole ones


def test_the_colours_come_from_the_answers():
    on = pp.panel_text(_snap(_fake()), 100)
    assert _style_of(on, 0, "ON") == "bold green" and _style_of(on, 0, "reachable") == "green"
    assert _style_of(on, 1, "on") == "green"                                # review is on
    assert _style_of(on, 2, "✓") == "green" and _style_of(on, 2, "✗") == "red"
    assert _style_of(pp.panel_text(_snap(_fake(answers={"saint-pool status": STATUS_ON_DOWN})), 100),
                     0, "UNREACHABLE") == "red"
    assert _style_of(pp.panel_text(dict(_snap(_fake()), error="no such key"), 100), 4, "no such key") == "red"


# -- cycling, in words ----------------------------------------------------------------------------------------------
def test_only_verified_reviewers_are_offered():
    rows = pp.parse_reviewers(Cmd(0, REVIEWERS))
    assert pp.next_reviewer(rows, "qwen-max") == "qwen-plus"                   # the unverified one is stepped over
    assert pp.next_reviewer(rows, "qwen-plus") == "qwen-max"                   # and the verified list wraps
    assert pp.next_reviewer(rows, "qwen-32b") == "qwen-max"                    # one off the list starts at the top
    assert pp.next_reviewer(rows, "") == "qwen-max"
    assert pp.next_reviewer([r for r in rows if not r["verified"]], "qwen-32b") is None
    assert pp.next_reviewer(None, "qwen-max") is None and pp.next_reviewer([], "") is None


# -- the board: the key, the panel, the edits -----------------------------------------------------------------------
def test_p_is_bound_to_the_panel_and_documented():
    bound = {k.strip(): b.action for b in tui.CtxApp.BINDINGS for k in b.key.split(",")}
    assert bound["P"] == "pool_panel" and bound["T"] == "pool_review" and bound["R"] == "pool_reviewer"
    assert bound["plus"] == "pool_rounds_up" and bound["minus"] == "pool_rounds_down"
    keys = {keys for keys, _, _ in tui.HELP_KEYS}
    assert ("P",) in keys and ("T",) in keys and ("R",) in keys and ("plus", "minus") in keys
    assert any("local pool" in words for _, shown, words in tui.HELP_KEYS if shown == "P")
    assert "Local pool panel" in {title for title, _ in tui.HELP_TIPS}
    tip = dict(tui.HELP_TIPS)["Local pool panel"]
    assert "saint-pool config set" in tip and "pool-stats" in tip and "read-only" in tip
    assert "P pool" in tui.key_bar(tui.CtxApp.BINDINGS, 200).plain             # and it shows in the key bar


async def test_p_shows_the_panel_from_the_commands_and_remembers_it(monkeypatch, tmp_path):
    tools = _fake()
    app = _board(monkeypatch, tmp_path, tools)
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.5)
        assert not app.query_one("#pool").display and tools.calls == []        # hidden: not one command runs
        await pilot.press("p"); await pilot.pause(0.5)                         # p is pop-ups, not this panel
        assert not app.query_one("#pool").display
        await pilot.press("P"); await pilot.pause(1)
        assert app.query_one("#pool").display
        panel = _panel(app)
        assert panel[0].startswith("pool · ON · http://gpu-host:4000 · reachable")
        assert "review · on · qwen-max · 2 rounds" in panel[1]
        assert (tmp_path / "pool-panel").read_text().strip() == "on"
        assert ["saint-pool", "status"] in tools.calls
        assert ["saint-pool", "config", "get", "--json"] in tools.calls
        assert tools.stats_since() == pp.today()
        n = len(tools.calls)
        await pilot.pause(3)
        assert len(tools.calls) == n                                           # no timer: it reads once, on open
        await pilot.press("P"); await pilot.pause(0.5)
        assert not app.query_one("#pool").display
        assert (tmp_path / "pool-panel").read_text().strip() == "off"


async def test_a_board_that_left_the_panel_on_reads_the_tools_as_soon_as_it_mounts(monkeypatch, tmp_path):
    tools = _fake()
    app = _board(monkeypatch, tmp_path, tools, shown="on")
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert app.query_one("#pool").display and ["saint-pool", "status"] in tools.calls


async def test_r_refreshes_the_panel_too(monkeypatch, tmp_path):
    tools = _fake()
    app = _board(monkeypatch, tmp_path, tools, shown="on")
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        n = len(tools.calls)
        await pilot.press("r"); await pilot.pause(1)
        assert len(tools.calls) > n and ["saint-pool", "status"] in tools.calls[n:]


async def test_t_reviewer_and_rounds_each_run_the_exact_command_and_read_back(monkeypatch, tmp_path):
    tools = _fake()
    app = _board(monkeypatch, tmp_path, tools, shown="on")
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        await pilot.press("T"); await pilot.pause(1)                           # it read on, so: off
        assert tools.last_set(pp.KEY_REVIEW) == ["saint-pool", "config", "set", "enabled", "off"]
        await pilot.press("T"); await pilot.pause(1)                           # it read back off, so: on
        assert tools.last_set(pp.KEY_REVIEW) == ["saint-pool", "config", "set", "enabled", "on"]
        await pilot.press("R"); await pilot.pause(1)
        assert tools.last_set(pp.KEY_REVIEWER) == ["saint-pool", "config", "set", "reviewer", "qwen-plus"]
        await pilot.press("plus"); await pilot.pause(1)
        assert tools.last_set(pp.KEY_ROUNDS) == ["saint-pool", "config", "set", "rounds", "3"]
        assert "review · on · qwen-plus · 3 rounds" in _panel(app)[1]           # read back, not remembered


async def test_rounds_stop_at_one_and_five_and_nothing_runs_when_they_cant_move(monkeypatch, tmp_path):
    tools = _fake(answers={"saint-pool config get --json": json.dumps({"enabled": True, "reviewer": "qwen-max",
                                                                      "rounds": 5})})
    app = _board(monkeypatch, tmp_path, tools, shown="on")
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        await pilot.press("plus"); await pilot.pause(0.5)                      # already at the top: no command
        assert tools.sets_of(pp.KEY_ROUNDS) == []
        for _ in range(6):                                                     # down through the bottom, and past it
            await pilot.press("minus"); await pilot.pause(0.5)
        assert tools.sets_of(pp.KEY_ROUNDS) == [["saint-pool", "config", "set", "rounds", str(n)] for n in (4, 3, 2, 1)]
        n = len(tools.calls)
        await pilot.press("minus"); await pilot.pause(0.5)
        assert len(tools.calls) == n                                           # at 1: not one command


async def test_an_error_from_config_set_is_shown_on_the_panel(monkeypatch, tmp_path):
    tools = _fake(sets="saint-pool: rounds must be 1..5")
    app = _board(monkeypatch, tmp_path, tools, shown="on")
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert not [ln for ln in _panel(app) if ln.startswith("config set:")]
        await pilot.press("plus"); await pilot.pause(1)
        assert "config set: saint-pool: rounds must be 1..5" in _panel(app)
        await pilot.press("r"); await pilot.pause(1)                           # a refresh says what it says now
        assert not [ln for ln in _panel(app) if ln.startswith("config set:")]


async def test_one_key_refused_and_the_next_edit_clears_it(monkeypatch, tmp_path):
    tools = _fake(sets={pp.KEY_ROUNDS: "saint-pool: rounds must be 1..5"})
    app = _board(monkeypatch, tmp_path, tools, shown="on")
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        await pilot.press("plus"); await pilot.pause(1)
        assert "config set: saint-pool: rounds must be 1..5" in _panel(app)
        await pilot.press("T"); await pilot.pause(1)
        assert not [ln for ln in _panel(app) if ln.startswith("config set:")]


async def test_a_host_with_no_saint_pool_is_read_only_and_says_so(monkeypatch, tmp_path):
    tools = _fake(missing=[pp.POOL_TOOL])
    app = _board(monkeypatch, tmp_path, tools, shown="on")
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert pp.READ_ONLY_MSG in _panel(app)
        for key in ("T", "R", "plus", "minus"):
            await pilot.press(key); await pilot.pause(0.5)
        assert [c for c in tools.calls if c[:3] == ["saint-pool", "config", "set"]] == []


async def test_nothing_verified_to_cycle_edits_nothing(monkeypatch, tmp_path):
    tools = _fake(answers={"saint-pool config reviewers --json":
                           json.dumps([{"name": "qwen-32b", "cloud": "ollama", "verified": False}])})
    app = _board(monkeypatch, tmp_path, tools, shown="on")
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        await pilot.press("R"); await pilot.pause(0.5)
        assert [c for c in tools.calls if c[:3] == ["saint-pool", "config", "set"]] == []


async def test_the_edit_keys_change_nothing_with_the_panel_closed(monkeypatch, tmp_path):
    """T, R and + − mean something with the panel up: closed, nothing is set and no command runs at all."""
    tools = _fake()
    app = _board(monkeypatch, tmp_path, tools)
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.5)
        for key in ("T", "R", "plus", "minus"):
            await pilot.press(key); await pilot.pause(0.5)
        assert tools.calls == []


async def test_the_panel_writes_no_file_of_its_own(monkeypatch, tmp_path):
    """The only file the board keeps about this panel is whether it starts shown: saint-pool owns its config."""
    tools = _fake()
    app = _board(monkeypatch, tmp_path, tools, shown="on")
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        for key in ("T", "R", "plus"):
            await pilot.press(key); await pilot.pause(0.8)
        assert {p.name for p in tmp_path.iterdir()} <= {"pool-panel", "list-rows", "popup.log"}
