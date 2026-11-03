"""Sessions on another host: they run in a tmux named ``ctx`` over there, so they survive the ssh dropping, and this
board only ever holds a view of one — ``n``/``s`` can start one, Enter on a ⇄ row attaches to it, ``k`` ends it there.

The scripts that do it are run for real here against a fake ``tmux`` on PATH that writes down every call, so what an
odd character in a directory or an agent argument turns into is checked rather than eyeballed, and each script is
syntax-checked by ``sh -n`` the way the other host's shell reads it. The board side is driven like
test_start_board.py (a fake hub, tmux patched out, its screens pushed over it); the local tmux side is in
tests/test_tmux.py.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from ctxswitch.client import tmux
from ctxswitch.client.tui import app as tui
from ctxswitch.common.config import Config

NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
HOME = "/home/me"
K1, K2, K3 = "laptop/claude/aaaa1111", "specul8/pi/bbbb2222", "specul8/claude/cccc3333"
OPTS = [("claude · bedrock", "claude", "bedrock"), ("claude · max", "claude", "max"), ("pi", "pi", None),
        ("antigravity", "agy", None), ("shell", "shell", None)]


@pytest.fixture(autouse=True)
def _home(monkeypatch):
    monkeypatch.setenv("HOME", HOME)


def _sess(key, *, status="ended", ended=True, ago=180, title="ctx switch", cwd=f"{HOME}/repos/ctxswitch", **kw):
    at = (NOW - timedelta(minutes=ago)).isoformat().replace("+00:00", "Z")
    host, harness = key.split("/")[:2]
    s = {"key": key, "host_id": host, "harness": harness, "session_id": key.rsplit("/", 1)[-1], "title": title,
         "live_status": status, "ended": ended, "status_since": at, "last_activity": at, "started_at": at,
         "cwd": cwd, "repo_root": cwd, "git_branch": "main", "turn_count": 3, "meta": {}, "open_asks": []}
    return {**s, **kw}


class FakeClient:
    """The board's hub as these tests need it: the rows it hands out, and nothing else."""

    hosts_here = ["laptop"]

    def __init__(self, rows):
        self.rows = rows

    def sessions(self, host=None, live_only=False):
        return {"sessions": [dict(x) for x in self.rows], "change": 1}

    def session(self, key, turns=50):
        return None

    def attention(self, host=None):
        return {"items": []}

    def hosts(self):
        return [{"host_id": h} for h in self.hosts_here]

    def spend(self, frm, to, host=None):
        return None

    def viewed(self, key, at):
        pass

    def ws_url(self):
        return "ws://x/v1"


def _board(monkeypatch, rows, in_tmux=True, reach=None, ssh=None, hosts=None) -> tui.CtxApp:
    cfg = Config(); cfg.hub.token = "t"; cfg.collector.host_id = "laptop"; cfg.ssh = dict(ssh or {})
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
    if hosts is not None:
        app.client.hosts_here = list(hosts)           # what a refresh brings back, which is what n and s offer
        app.hosts = list(hosts)
    return app


async def _board_only(app, pilot) -> None:
    """A board of nothing but ended rows opens the Start screen on its own; these tests want the list under it."""
    if isinstance(app.screen, tui.StartScreen):
        await pilot.press("escape")
        await pilot.pause(0.4)


def _key_press(widget, key: str) -> None:
    from textual.events import Key
    widget.post_message(Key(key, character=None))


# -- the remote scripts, run for real against a fake tmux ----------------------------------------------------------
def _fake_tmux(tmp_path, mark: str) -> dict:
    """A bin dir with a ``tmux`` that writes down every call it gets (a CALL line, then one argument each) and
    answers with a window id. ``has-session`` succeeds only while ``mark`` exists — which is how both branches of
    the script get run."""
    d = tmp_path / "bin"; d.mkdir()
    log = tmp_path / "calls"
    (d / "tmux").write_text('#!/bin/sh\n'
                            f'if [ "$1" = has-session ]; then [ -e "{mark}" ] || exit 1; fi\n'
                            f'printf "CALL\\n" >> "{log}"; printf "%s\\n" "$@" >> "{log}"\n'
                            'printf "@9\\n"\n')
    (d / "tmux").chmod(0o755)
    return {"dir": d, "log": log}


def _run_script(script: str, fake, cwd) -> list[list[str]]:
    """Run one of those scripts as the other host's shell would, and read back the tmux calls it made."""
    fake["log"].write_text("")
    r = subprocess.run(["sh", "-c", script], cwd=str(cwd),
                       env={**os.environ, "PATH": f"{fake['dir']}:{os.environ['PATH']}"},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    calls: list[list[str]] = []
    for line in fake["log"].read_text().splitlines():
        if line == "CALL":
            calls.append([])
        elif calls:
            calls[-1].append(line)
    return calls


def _shn(script: str) -> None:
    """The other host's shell has to be able to read it: sh's own syntax check, no command run."""
    r = subprocess.run(["sh", "-n", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _dir_with_a_quote(tmp_path) -> str:
    d = tmp_path / "a b'c"; d.mkdir()
    return str(d)


AGENT = ["claude", "--session-id", "x'1"]


def test_the_remote_script_makes_a_window_in_the_ctx_session_that_is_already_up(tmp_path):
    cwd = _dir_with_a_quote(tmp_path)
    script = tmux.remote_script("specul8/claude", cwd, AGENT, K3)
    _shn(script)
    for want in ("if tmux has-session -t ctx 2>/dev/null; then", "tmux new-window -d -t ctx:", "tmux new-session -d -s ctx",
                 '-c "$d"', "-P -F '#{window_id}'", f"@ctx_key {K3}", "exec tmux new-session -t ctx -s ctxview-"):
        assert want in script, want
    (tmp_path / "ctx-up").touch()                     # has-session succeeds: its ctx is already up, so new-window
    fake = _fake_tmux(tmp_path, tmp_path / "ctx-up")
    calls = _run_script(script, fake, tmp_path)
    made = [c for c in calls if "new-window" in c]
    assert made, calls
    assert made[0][made[0].index("-c") + 1] == cwd    # a directory with a quote and a space in it lands whole
    wrapped = [shlex.split(a) for a in made[0] if a.startswith("bash -lc ")]
    assert len(wrapped) == 1 and wrapped[0][:2] == ["bash", "-lc"]
    assert wrapped[0][2].startswith(f"{shlex.join(AGENT)}; rc=$?;")       # an agent argument carrying a quote survives
    assert "read -r _" in wrapped[0][2]                                   # and one that fails keeps its window

    assert ["set-option", "-p", "-t", "@9", "@ctx_key", K3] in [c for c in calls if "@ctx_key" in c]
    view = [c for c in calls if "ctxview-" in " ".join(c)]       # it ends by attaching a view that dies on detach
    assert view and "destroy-unattached" in view[0] and "-t" in view[0][:6]


def test_the_remote_script_makes_the_ctx_session_when_there_isn_t_one_yet(tmp_path):
    cwd = _dir_with_a_quote(tmp_path)
    script = tmux.remote_script("specul8/claude", cwd, AGENT, None)
    _shn(script)
    fake = _fake_tmux(tmp_path, tmp_path / "nothing-here")       # has-session fails, so ctx gets made
    calls = _run_script(script, fake, tmp_path)
    made = [c for c in calls if "new-session" in c and "-d" in c]
    assert made and made[0][made[0].index("-s") + 1] == "ctx", calls
    assert made[0][made[0].index("-c") + 1] == cwd
    assert not any("new-window" in c for c in calls)
    assert not any("@ctx_key" in c for c in calls)               # no key known: nothing to tag it with


def test_the_view_command_shows_one_window_of_a_running_session_without_taking_over():
    cmd = tmux.view_command("ctx:@3.%7")
    assert "tmux new-session -t ctx -s ctxview-" in cmd and "destroy-unattached on" in cmd
    assert "status off" in cmd and "prefix None" in cmd
    assert "select-window -t %7" in cmd and "select-pane -t %7" in cmd   # by its pane: the window may have moved
    assert tmux.view_command("@3").count("select-window") == 1   # a bare window id: nothing to name or select by
    grouped = tmux.view_command("other:@1")
    assert "-t other " in grouped and "select-window -t @1" in grouped and "select-pane" not in grouped
    assert tmux.view_command("S:@3.%7").count("select-") == 2
    _shn(cmd); _shn(grouped)


def test_remote_argv_hands_the_script_to_the_remote_shell_as_one_argument():
    script = tmux.view_command("ctx:@3.%7")
    argv = tmux.remote_argv("rick@specul8", script)
    assert argv[:2] == ["ssh", "-t"] and "rick@specul8" in argv and argv[-3:] == ["sh", "-c", shlex.quote(script)]
    assert ";" not in " ".join(argv[:-1])               # the script is one argument: none of it runs on this side


# -- n: which host, then which agent -------------------------------------------------------------------------------
async def test_n_asks_which_host_when_another_one_is_reachable(monkeypatch):
    started = []
    monkeypatch.setattr(tui.tmux, "new_remote_agent",
                        lambda h, a, c, ssh=None: started.append((h, a, c)) or f"new {a} on {h}")
    app = _board(monkeypatch, [_sess(K1, status="idle", ended=False)], reach={K1: "pane"},
                 ssh={"specul8": "rick@specul8"}, hosts=["laptop", "specul8"])
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("n"); await pilot.pause(0.4)
        assert isinstance(app.screen, tui.Picker)
        assert [o[0] for o in app.screen.options] == ["laptop (here)", "specul8"]      # this host first
        _key_press(app.screen, "2"); await pilot.pause(0.4)
        assert [o[1] for o in app.screen.options] == ["claude", "pi", "shell"]          # its profiles are its own
        _key_press(app.screen, "1"); await pilot.pause(0.4)
        f = app.query_one("#filter")
        assert app.input_mode == "new@specul8:claude:" and f.value == "~" and f.visible
        tui.CtxApp.on_input_submitted(app, type("E", (), {"value": "~"})())
        await pilot.pause(1.2)
        assert started == [("specul8", "claude", "~")] and app.input_mode == "filter"


async def test_n_on_this_host_goes_on_to_the_local_picker(monkeypatch):
    started = []
    monkeypatch.setattr(tui.tmux, "new_agent", lambda h, c, **k: started.append((h, c)) or f"new {h}")
    app = _board(monkeypatch, [_sess(K1, status="idle", ended=False, cwd="/x")], reach={K1: "pane"},
                 ssh={"specul8": "rick@specul8"}, hosts=["laptop", "specul8"])
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("n"); await pilot.pause(0.4)
        _key_press(app.screen, "1"); await pilot.pause(0.4)      # "laptop (here)"
        assert [o[1] for o in app.screen.options] == ["claude", "pi", "agy", "shell"]     # today's picker
        _key_press(app.screen, "4"); await pilot.pause(0.4)
        assert app.input_mode == "new:shell:" and app.query_one("#filter").value == "/x"
        tui.CtxApp.on_input_submitted(app, type("E", (), {"value": "/x"})())
        await pilot.pause(1.2)
        assert started == [("shell", "/x")]


async def test_n_with_no_other_host_to_pick_goes_straight_to_today_s_picker(monkeypatch):
    app = _board(monkeypatch, [_sess(K1, status="idle", ended=False)], reach={K1: "pane"},
                 ssh={"specul8": "rick@specul8"}, hosts=["laptop"])
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("n"); await pilot.pause(0.4)
        assert isinstance(app.screen, tui.Picker)
        assert [o[1] for o in app.screen.options] == [o[1] for o in tui.new_session_options(app.cfg)] + ["shell"]


def test_only_hosts_an_ssh_route_reaches_are_offered(monkeypatch):
    app = _board(monkeypatch, [], ssh={"specul8": "", "gpu": "me@gpu"}, hosts=["laptop", "specul8", "gpu", "laptop"])
    assert app._remote_hosts() == ["gpu"]                     # "" = not reachable from here; no repeats, no this host


# -- s: the Start screen's other host ------------------------------------------------------------------------------
async def test_h_cycles_the_start_screen_onto_another_host(monkeypatch):
    seen: list = []
    app = _board(monkeypatch, [_sess(K1, status="idle", ended=False, cwd="/x")], reach={K1: "pane"},
                 ssh={"specul8": "rick@specul8"}, hosts=["laptop", "specul8"])
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.5)
        screen = tui.StartScreen(OPTS, [_sess(K1, cwd="/x")], {K1: "resume"}, NOW, hosts=["laptop", "specul8"])
        app.push_screen(screen, lambda r: seen.append(r))
        await pilot.pause(0.4)
        assert screen.body().plain.splitlines()[0] == "START on laptop   h: another host"
        await pilot.press("h"); await pilot.pause(0.4)
        assert screen.host == "specul8"
        assert screen.body().plain.splitlines()[0] == "START on specul8   h: another host"
        assert "1 claude   2 pi   3 shell" in screen.body().plain          # its profiles are its own, so no accounts
        await pilot.press("h"); await pilot.pause(0.4)
        assert screen.host is None and "1 claude · bedrock" in screen.body().plain
        await pilot.press("h"); await pilot.pause(0.4)
        await pilot.press("1"); await pilot.pause(0.4)
        assert seen == [("new", "claude", None, None, "specul8")]          # no profile, no directory of ours to carry
        app._started(seen[0])
        await pilot.pause(0.4)
        assert app.input_mode == "new@specul8:claude:" and app.query_one("#filter").value == "~"


async def test_a_digit_on_this_host_still_carries_the_row_s_directory(monkeypatch):
    seen: list = []
    app = _board(monkeypatch, [_sess(K1, status="idle", ended=False, cwd="/x")], reach={K1: "pane"},
                 hosts=["laptop", "specul8"], ssh={"specul8": "rick@specul8"})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.5)
        screen = tui.StartScreen(OPTS, [_sess(K1, cwd="/x")], {K1: "resume"}, NOW, hosts=["laptop"])
        app.push_screen(screen, lambda r: seen.append(r))
        await pilot.pause(0.4)
        assert screen.body().plain.splitlines()[0] == "START"              # nothing else to start on: no host line
        await pilot.press("h"); await pilot.pause(0.4)
        assert seen == [] and screen.host is None                          # and h has nothing to cycle through
        await pilot.press("5"); await pilot.pause(0.4)
        assert seen == [("new", "shell", None, "/x")]


# -- k on a session running in a tmux on its own host --------------------------------------------------------------
async def test_k_ends_a_remote_session_on_its_own_host(monkeypatch):
    ended, locals_, msgs = [], [], []
    monkeypatch.setattr(tui.tmux, "end_remote", lambda s, ssh=None: ended.append((s["key"], ssh)) or "ended on specul8")
    monkeypatch.setattr(tui.tmux, "agent_pane", lambda key, ps=None: locals_.append(key) or None)
    monkeypatch.setattr(tui.tmux, "end_agent", lambda p: 1)
    app = _board(monkeypatch, [_sess(K3, status="idle", ended=False)], in_tmux=True,
                 reach={K3: "attach"}, ssh={"specul8": "rick@specul8"})
    monkeypatch.setattr(app, "notify", lambda msg, *a, **k: msgs.append(msg))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.5)
        app.select_key(K3)
        await pilot.press("k"); await pilot.pause(0.4)
        assert isinstance(app.screen, tui.Confirm) and "on specul8" in app.screen.prompt
        await pilot.press("n"); await pilot.pause(0.4)
        assert ended == [] and any("cancelled" in m for m in msgs)         # a y is needed, as everywhere
        await pilot.press("k"); await pilot.pause(0.4)
        await pilot.press("y"); await pilot.pause(1.2)
        assert ended == [(K3, app.cfg.ssh)] and "ended on specul8" in msgs


async def test_k_on_a_remote_session_with_a_local_window_tagged_to_it_ends_it_there_too(monkeypatch):
    pane = tmux.Pane("%7", "@3", "specul8/claude", 2, 9, K3, "ctx", "agent", cmd="ssh")
    ended, signalled = [], []
    monkeypatch.setattr(tui.tmux, "end_remote", lambda s, ssh=None: ended.append(s["key"]) or "ended on specul8")
    monkeypatch.setattr(tui.tmux, "agent_pane", lambda key, ps=None: pane if key == K3 else None)
    monkeypatch.setattr(tui.tmux, "end_agent", lambda p: signalled.append(p.id) or 1)
    app = _board(monkeypatch, [_sess(K3, status="idle", ended=False)], in_tmux=True,
                 reach={K3: "pane"}, ssh={"specul8": "rick@specul8"})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.5)
        app.select_key(K3)
        await pilot.press("k"); await pilot.pause(0.4)
        await pilot.press("y"); await pilot.pause(1.2)
        assert ended == [K3] and signalled == []        # the view's own window goes with it, inside end_remote


async def test_k_on_a_local_session_is_unchanged(monkeypatch):
    pane = tmux.Pane("%4", "@2", "ctxswitch", 1, 6, K1, "ctx", "agent", cmd="claude")
    ended, signalled = [], []
    monkeypatch.setattr(tui.tmux, "end_remote", lambda s, ssh=None: ended.append(s["key"]) or "ended")
    monkeypatch.setattr(tui.tmux, "agent_pane", lambda key, ps=None: pane if key == K1 else None)
    monkeypatch.setattr(tui.tmux, "end_agent", lambda p: signalled.append(p.id) or 1)
    app = _board(monkeypatch, [_sess(K1, status="idle", ended=False)], in_tmux=True, reach={K1: "pane"})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.5)
        app.select_key(K1)
        await pilot.press("k"); await pilot.pause(0.4)
        await pilot.press("y"); await pilot.pause(1.2)
        assert signalled == ["%4"] and ended == []


async def test_k_on_an_ended_remote_session_still_says_it_isn_t_running(monkeypatch):
    said = []
    app = _board(monkeypatch, [_sess(K3)], in_tmux=True, reach={K3: "ssh"}, ssh={"specul8": "rick@specul8"})
    monkeypatch.setattr(tui.tmux, "agent_pane", lambda key, ps=None: None)
    monkeypatch.setattr(app, "notify", lambda *a, **k: said.append(a[0] if a else ""))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(0.5)
        await _board_only(app, pilot)
        app.select_key(K3)
        await pilot.press("k"); await pilot.pause(0.4)
        assert not isinstance(app.screen, tui.Confirm)
        assert any("can't end it from here" in m for m in said)


# -- what the board says about them --------------------------------------------------------------------------------
def test_the_attach_reach_is_documented_wherever_a_glyph_is_listed():
    assert tmux.REACH_GLYPH["attach"] == "⇄"
    assert "over ssh" in tmux.REACH_HELP["attach"] and "<host>" not in tmux.REACH_HELP["attach"]
    assert "attach" in tui.REACH_STYLE                                      # and it isn't left unstyled
    enter = [row for title, rows in tui.legend_sections() for row in rows if title.startswith("ENTER")]
    assert ("⇄", tui.REACH_STYLE["attach"], tmux.REACH_HELP["attach"]) in enter     # the legend's REACH rows
    n_text = tui.HELP_KEYS[[k for k, _, _ in tui.HELP_KEYS].index(("n",))][2]
    assert "this host or another" in n_text and "ssh" in n_text
    tip = dict(tui.HELP_TIPS)["Other hosts"]
    for want in ("another host", "tmux named ctx", "⇄", "`k` ends it there"):
        assert want in tip, want


def test_the_remote_helpers_are_the_contract_the_board_builds_on():
    for name in ("remote_script", "view_command", "remote_argv", "new_remote_agent", "end_remote", "_remote_view",
                 "_is_remote", "ssh_target"):
        assert callable(getattr(tmux, name)), name
    assert tmux._is_remote({"host_id": "specul8"}, "laptop") and not tmux._is_remote({"host_id": "laptop"}, "laptop")
    assert tmux.ssh_target("specul8", {"specul8": "rick@specul8"}) == "rick@specul8"
    assert tmux.ssh_target("specul8", {"specul8": ""}) is None


def test_the_remote_script_finds_ctx_over_there_not_by_this_hosts_path():
    """ctx's own path here means nothing on the other host: there it's ctx on the login PATH, else the venv."""
    script = tmux.remote_script("s/sh", "~", ["/home/rick/.local/bin/ctx", "shell", "--id", "X1"], "h/shell/X1")
    assert "/home/rick/.local/bin/ctx" not in script
    assert 'command -v ctx || echo "$HOME/repos/ctxswitch/.venv/bin/ctx"' in shlex.split(script.split("w=$(", 1)[1])[-1] \
        or "command -v ctx" in script
    assert "shell --id X1" in script


async def test_a_board_on_this_host_also_lists_what_it_runs_on_other_hosts(monkeypatch):
    """Filtered to this host, the board still shows a remote session in that host's ctx tmux (started from here),
    with its host on the row; another tmux over there stays under that host's own filter."""
    here = _sess("laptop/claude/a1", status="busy", ended=False)
    ours = _sess("specul8/shell/s1", status="idle", ended=False, tmux_at="ctx:@0.%0")
    theirs = _sess("specul8/claude/c1", status="idle", ended=False, tmux_at="work:@2.%5")
    app = _board(monkeypatch, [here, ours, theirs], hosts=["laptop", "specul8"], ssh={"specul8": "rick@specul8"})
    sessions = app.client.sessions
    monkeypatch.setattr(app.client, "sessions", lambda host=None, live_only=False: (
        {"sessions": [here], "change": 1} if host == "laptop" else sessions(host, live_only)))
    monkeypatch.setattr(tui.tmux, "panes", lambda: [])
    app.host_filter = "laptop"
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(1)
        keys = {s["key"] for s in app.sessions}
        assert keys == {"laptop/claude/a1", "specul8/shell/s1"}
        t = app.query_one("#table")
        labels = [t.get_row_at(i)[3].plain for i in range(t.row_count)]
        assert any("sh·specul8" in lb for lb in labels) and not any("·laptop" in lb for lb in labels)
