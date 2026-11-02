"""The Start screen (s, and on its own when the board starts with nothing live): one key per profile to start
something, and the ended sessions this board can bring back — several of them at once.

Driven like test_spend_board.py: a fake hub, tmux patched out, the board's own screens pushed over it. The tmux side
of resuming several at once (one window each, the last one into the viewer pane) is in tests/test_tmux.py.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from ctxswitch.client import fmt
from ctxswitch.client.tui import app as tui
from ctxswitch.common.config import Config

NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
HOME = "/home/me"
K1, K2, K3 = "laptop/claude/aaaa1111", "devbox/pi/bbbb2222", "laptop/claude/cccc3333"
OPTS = [("claude · bedrock", "claude", "bedrock"), ("claude · max", "claude", "max"), ("pi", "pi", None),
        ("antigravity", "agy", None), ("shell", "shell", None)]


@pytest.fixture(autouse=True)
def _home(monkeypatch):
    monkeypatch.setenv("HOME", HOME)


def _sess(key, *, status="ended", ended=True, ago=180, title="ctx switch", cwd=f"{HOME}/repos/ctxswitch", **kw):
    """An ended session unless told otherwise: the Start screen only lists what fmt.session_state calls ended."""
    at = (NOW - timedelta(minutes=ago)).isoformat().replace("+00:00", "Z")
    host, harness = key.split("/")[:2]
    s = {"key": key, "host_id": host, "harness": harness, "session_id": key.rsplit("/", 1)[-1], "title": title,
         "live_status": status, "ended": ended, "status_since": at, "last_activity": at, "started_at": at,
         "cwd": cwd, "repo_root": cwd, "git_branch": "main", "turn_count": 3, "meta": {}, "open_asks": []}
    return {**s, **kw}


class FakeClient:
    """The board's hub as these tests need it: the rows it hands out, and nothing else."""

    def __init__(self, rows):
        self.rows = rows

    def sessions(self, host=None, live_only=False):
        return {"sessions": [dict(x) for x in self.rows], "change": 1}

    def session(self, key, turns=50):
        return None

    def attention(self, host=None):
        return {"items": []}

    def hosts(self):
        return [{"host_id": "laptop"}, {"host_id": "devbox"}]

    def spend(self, frm, to, host=None):
        return None

    def viewed(self, key, at):
        pass

    def ws_url(self):
        return "ws://x/v1"


def _board(monkeypatch, rows, in_tmux=False, reach=None) -> tui.CtxApp:
    cfg = Config(); cfg.hub.token = "t"; cfg.collector.host_id = "laptop"
    monkeypatch.setattr(tui.tmux, "in_tmux", lambda: in_tmux)
    monkeypatch.setattr(tui.tmux, "reap_viewer", lambda *a, **k: None)
    monkeypatch.setattr(tui.tmux, "panes", lambda *a, **k: [])
    monkeypatch.setattr(tui.tmux, "visible_key", lambda ps=None, sessions=None: None)
    monkeypatch.setattr(tui.tmux, "on_screen_keys", lambda ps=None, sessions=None: set())
    monkeypatch.setattr(tui.tmux, "reach_all", lambda sessions, host=None, ssh=None: dict(reach or {}))
    monkeypatch.setattr(tui.CtxApp, "listen_ws", lambda self: None)
    monkeypatch.setattr(tui.CtxApp, "join_tmux", lambda self: None)
    app = tui.CtxApp(cfg, None)
    app.client = FakeClient(rows)
    return app


def _push(app, recents, reach=None, options=None):
    """Push a StartScreen over a running board; returns (the screen, what it dismissed with)."""
    seen: list = []
    reach = dict(reach if reach is not None else {s["key"]: "resume" for s in recents})
    screen = tui.StartScreen(options or OPTS, recents, reach, NOW)
    app.push_screen(screen, lambda r: seen.append(r))
    return screen, seen


# -- fmt.recent_sessions / fmt.recent_line ------------------------------------------------------------------------
def test_recent_sessions_keeps_ended_ones_this_board_can_resume():
    rows = [_sess(K1), _sess(K2, ago=2880), _sess(K3, status="busy", ended=False)]
    keep = fmt.recent_sessions(rows, {K1: "resume", K2: "ssh", K3: "resume"})
    assert [s["key"] for s in keep] == [K1, K2]              # ended on its own host, or ended on one it can ssh to


def test_recent_sessions_drops_what_enter_cannot_open_and_what_is_live():
    rows = [_sess(K1), _sess(K2, ago=2880), _sess(K3, status="idle", ended=False)]
    for reach in ("remote", "none", "pane", "foreign"):
        assert fmt.recent_sessions(rows, {K1: reach, K2: reach, K3: reach}) == [], reach
    assert fmt.recent_sessions(rows, {}) == []              # a board that can't reach anything


def test_recent_sessions_sorts_newest_first_and_caps_the_list():
    rows = [_sess(f"laptop/claude/i{i:04d}", ago=ago) for i, ago in enumerate((60, 3600, 120, 86_400))]
    reach = {s["key"]: "resume" for s in rows}
    assert [s["key"] for s in fmt.recent_sessions(rows, reach)] == [rows[0]["key"], rows[2]["key"],
                                                                    rows[1]["key"], rows[3]["key"]]
    assert [s["key"] for s in fmt.recent_sessions(rows, reach, n=2)] == [rows[0]["key"], rows[2]["key"]]


def test_recent_line_reads_like_a_board_row():
    assert fmt.recent_line(_sess(K1), {K1: "resume"}, NOW, 120) == \
        "↻ cc·laptop  3h00  ctx switch  ctxswitch@main  ~/repos/ctxswitch"
    assert fmt.recent_line(_sess(K2, ago=2880), {K2: "ssh"}, NOW, 120) == \
        "↗ pi·devbox  2d  ctx switch  ctxswitch@main  ~/repos/ctxswitch"
    assert fmt.recent_line(_sess(K3, title=""), {K3: "resume"}, NOW, 120) == \
        "↻ cc·laptop  3h00  (no prompt yet)  ctxswitch@main  ~/repos/ctxswitch"
    assert fmt.recent_line(_sess(K1, cwd=HOME), {K1: "resume"}, NOW, 120).endswith("~")


def test_recent_line_drops_the_directory_before_it_touches_the_title():
    s = _sess(K1, title="a title far too long for this pane", cwd=f"{HOME}/repos/ctxswitch")
    full = fmt.recent_line(s, {K1: "resume"}, NOW, 200)
    assert len(fmt.recent_line(s, {K1: "resume"}, NOW, len(full))) == len(full)      # room: nothing cut
    tight = fmt.recent_line(s, {K1: "resume"}, NOW, len(full) - 1)
    assert "~/repos" not in tight and "a title far too long for this pane" in tight  # the directory went, whole
    assert len(fmt.recent_line(s, {K1: "resume"}, NOW, 30)) <= 30                    # tighter: the title gives way


def test_recent_sessions_leaves_out_pool_job_workers():
    rows = [_sess(K1), {**_sess(K2), "job": "sa-20261101-1", "parent_key": K1}]
    assert [s["key"] for s in fmt.recent_sessions(rows, {K1: "resume", K2: "resume"})] == [K1]


# -- the screen's keys and its text ---------------------------------------------------------------------------------
async def test_a_digit_starts_that_profile_in_the_cursor_row_s_directory(monkeypatch):
    app = _board(monkeypatch, [_sess(K1, cwd="/x")])
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.4)
        screen, seen = _push(app, [_sess(K1, cwd="/x")])
        await pilot.pause(0.3)
        await pilot.press("5")                                # shell, the last of these options
        await pilot.pause(0.3)
        assert seen == [("new", "shell", None, "/x")] and not isinstance(app.screen, tui.StartScreen)
        _, seen = _push(app, [_sess(K1, cwd="/x")], options=[("pi", "pi", None)])
        await pilot.pause(0.3)
        await pilot.press("1")
        await pilot.pause(0.3)
        assert seen == [("new", "pi", None, "/x")]


async def test_a_digit_past_the_last_option_does_nothing(monkeypatch):
    app = _board(monkeypatch, [_sess(K1)])
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.4)
        screen, seen = _push(app, [_sess(K1)], options=[("pi", "pi", None)])
        await pilot.pause(0.3)
        await pilot.press("9")
        await pilot.pause(0.3)
        assert seen == [] and isinstance(app.screen, tui.StartScreen)


async def test_space_marks_several_and_enter_resumes_them_in_list_order(monkeypatch):
    recents = [_sess(K1), _sess(K2, ago=2880)]
    app = _board(monkeypatch, recents)
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.4)
        screen, seen = _push(app, recents)
        await pilot.pause(0.3)
        await pilot.press("down", "space")                    # mark the second one …
        await pilot.pause(0.2)
        await pilot.press("up", "space")                      # … then the first
        await pilot.pause(0.2)
        assert screen.marked == {K1, K2}
        assert "[x]" in screen.query_one("#start_text").render().plain
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert seen == [("resume", [K1, K2])]                 # the list's order, not the order they were marked
        assert not isinstance(app.screen, tui.StartScreen)


async def test_enter_with_nothing_marked_resumes_the_row_under_the_cursor(monkeypatch):
    recents = [_sess(K1), _sess(K2, ago=2880)]
    app = _board(monkeypatch, recents)
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.4)
        screen, seen = _push(app, recents)
        await pilot.pause(0.3)
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert seen == [("resume", [K1])]
        screen, seen = _push(app, recents)
        await pilot.pause(0.3)
        await pilot.press("down", "enter")
        await pilot.pause(0.3)
        assert seen == [("resume", [K2])]
        screen, seen = _push(app, recents)
        await pilot.pause(0.3)
        await pilot.press("up", "up", "k"); await pilot.pause(0.3)     # j/k move it, clamped at both ends
        assert screen.cursor == 0 and seen == []
        await pilot.press("down", "down", "down", "j"); await pilot.pause(0.3)
        assert screen.cursor == len(recents) - 1


async def test_escape_q_and_s_close_it_with_nothing(monkeypatch):
    app = _board(monkeypatch, [_sess(K1)])
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.4)
        for close in ("escape", "q", "s"):
            screen, seen = _push(app, [_sess(K1)])
            await pilot.pause(0.3)
            await pilot.press(close)
            await pilot.pause(0.3)
            assert seen == [None] and not isinstance(app.screen, tui.StartScreen), close


async def test_the_start_screen_shows_the_options_and_the_recents(monkeypatch):
    app = _board(monkeypatch, [])
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.4)
        screen, _ = _push(app, [_sess(K1), _sess(K2, ago=2880)], reach={K1: "resume", K2: "ssh"})
        await pilot.pause(0.3)
        t = screen.query_one("#start_text").render()
        for want in ("START", "RECENT (2)", "1 claude · bedrock", "2 claude · max", "3 pi", "4 antigravity",
                     "5 shell", "[ ]", "↻ cc·laptop", "↗ pi·devbox", "3h00", "2d", "Space mark"):
            assert want in t.plain, want
        assert any(getattr(sp.style, "reverse", False) for sp in t.spans)   # the cursor row is reverse-video
        assert t.plain.splitlines()[3].startswith("> ") and t.plain.splitlines()[4].startswith("  [")
        await pilot.press("space"); await pilot.pause(0.3)
        assert "[x]" in screen.query_one("#start_text").render().plain


async def test_a_board_with_nothing_ended_to_resume_says_so(monkeypatch):
    app = _board(monkeypatch, [_sess(K1, status="idle", ended=False)])
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.4)
        screen, seen = _push(app, [])
        await pilot.pause(0.3)
        assert "RECENT — nothing ended to resume" in screen.query_one("#start_text").render().plain
        await pilot.press("enter"); await pilot.pause(0.3)
        assert seen == [None]                                # nothing to resume: it says so and stays put


# -- the board opens it, once ---------------------------------------------------------------------------------------
async def test_a_cold_board_opens_the_start_screen_by_itself(monkeypatch):
    rows = [_sess(K1), _sess(K2, ago=2880)]
    app = _board(monkeypatch, rows, in_tmux=True, reach={K1: "resume", K2: "ssh"})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert isinstance(app.screen, tui.StartScreen) and app.start_shown
        assert "RECENT (2)" in app.screen.query_one("#start_text").render().plain


async def test_a_board_with_something_live_opens_the_board_not_the_start_screen(monkeypatch):
    rows = [_sess(K1), _sess(K2, status="busy", ended=False, ago=1)]
    app = _board(monkeypatch, rows, in_tmux=True, reach={K1: "resume", K2: "pane"})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert not isinstance(app.screen, tui.StartScreen) and app.start_shown
        assert app.slots == [K2]                                  # its row is on the board, ended ones hidden


async def test_a_cold_board_opens_it_once_and_s_opens_it_again(monkeypatch):
    rows = [_sess(K1), _sess(K2, ago=2880)]
    app = _board(monkeypatch, rows, in_tmux=True, reach={K1: "resume", K2: "ssh"})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert isinstance(app.screen, tui.StartScreen)
        app.pop_screen(); await pilot.pause(0.3)
        depth = len(app.screen_stack)
        app.apply_data(rows, [], ["laptop"], 2, None, first_load=True)
        await pilot.pause(0.3)
        assert len(app.screen_stack) == depth                 # a second load never opens it again
        await pilot.press("s"); await pilot.pause(0.3)
        assert isinstance(app.screen, tui.StartScreen)        # but the key does
        await pilot.press("escape"); await pilot.pause(0.3)
        app.refresh_data(); await pilot.pause(1)
        assert len(app.screen_stack) == depth                 # and a live refresh doesn't reopen it


async def test_it_never_opens_over_a_screen_already_up(monkeypatch):
    app = _board(monkeypatch, [_sess(K1)], in_tmux=True, reach={K1: "resume"})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert isinstance(app.screen, tui.StartScreen)
        app.pop_screen(); await pilot.pause(0.3)
        app.push_screen(tui.Help()); await pilot.pause(0.3)
        app.apply_data([_sess(K1)], [], ["laptop"], 2, None, first_load=True)
        await pilot.pause(0.3)
        assert isinstance(app.screen, tui.Help) and len(app.screen_stack) == 2


async def test_s_outside_tmux_says_what_it_needs(monkeypatch):
    said = []
    app = _board(monkeypatch, [_sess(K1)], in_tmux=False)
    monkeypatch.setattr(app, "notify", lambda *a, **k: said.append((a, k)))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("s"); await pilot.pause(0.3)
        assert not isinstance(app.screen, tui.StartScreen)
        assert any("the tmux manager (ctx tmux)" in a[0] and k.get("severity") == "warning" for a, k in said)


# -- what its answers do ----------------------------------------------------------------------------------------------
async def test_a_new_choice_asks_for_a_directory_pre_filled_with_the_row_s(monkeypatch):
    app = _board(monkeypatch, [_sess(K1, status="idle", ended=False, cwd=f"{HOME}/repos/elsewhere")])
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.5)
        app._started(("new", "claude", "max", "/x"))
        await pilot.pause(0.3)
        f = app.query_one("#filter")
        assert app.input_mode == "new:claude:max" and f.value == "/x" and f.visible
        app._started(("new", "pi", None, None))               # no row: the selected session's directory
        await pilot.pause(0.3)
        assert app.input_mode == "new:pi:" and app.query_one("#filter").value == f"{HOME}/repos/elsewhere"
        app._started(None)
        await pilot.pause(0.3)
        assert app.input_mode == "new:pi:"                    # nothing said: the prompt stays as it was


async def test_a_resume_choice_outside_tmux_warns_instead(monkeypatch):
    said = []
    app = _board(monkeypatch, [_sess(K1)], in_tmux=False)
    monkeypatch.setattr(app, "notify", lambda *a, **k: said.append((a, k)))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.5)
        app._started(("resume", [K1]))
        await pilot.pause(0.3)
        assert any("resuming needs the tmux manager (ctx tmux)" in a[0] and k.get("severity") == "warning"
                   for a, k in said)


async def test_resuming_several_reports_marks_them_and_refreshes(monkeypatch):
    rows = [_sess(K1), _sess(K2, ago=2880)]
    app = _board(monkeypatch, rows, in_tmux=True, reach={K1: "resume", K2: "ssh"})
    msgs, viewed, resumed = [], [], []
    monkeypatch.setattr(tui.tmux, "resume_many",
                        lambda sessions, ssh=None: (resumed.append(([s["key"] for s in sessions], ssh)),
                                                    "resumed 2: ctx switch, ctx switch")[1])
    monkeypatch.setattr(app, "notify", lambda msg, *a, **k: msgs.append(msg))
    monkeypatch.setattr(app, "mark_opened", lambda key: viewed.append(key))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.5)
        app.resume_many_sessions([K1, K2, "laptop/claude/gone"])
        await pilot.pause(1.2)
        assert resumed[0][0] == [K1, K2] and resumed[0][1] == app.cfg.ssh   # a key gone off the board isn't resumed
        assert viewed == [K1, K2] and msgs == ["resumed 2: ctx switch, ctx switch"]


def test_start_is_bound_and_documented():
    bound = {k.strip(): b.action for b in tui.CtxApp.BINDINGS for k in b.key.split(",")}
    assert bound["s"] == "start"
    assert ("s",) in {keys for keys, _, _ in tui.HELP_KEYS}
    assert any("start" in words for _, shown, words in tui.HELP_KEYS if shown == "s")
    assert "s" in tui.key_bar(tui.CtxApp.BINDINGS, 400).plain         # and it shows in the key bar
    assert any(t == "Starting cold" for t, _ in tui.HELP_TIPS)


