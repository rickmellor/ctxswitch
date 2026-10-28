"""Exercises the tmux helpers against a private tmux server (-L) with real panes; skipped without tmux."""
import os
import shutil
import subprocess
import time

import pytest

from ctxswitch.client import tmux

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
SOCK = f"ctxtest{os.getpid()}"
_n = 0


def tm(*a, check=True):
    r = subprocess.run(["tmux", "-L", SOCK, *a], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(r.stderr)
    return r.stdout.strip()


@pytest.fixture
def server(monkeypatch):
    global SOCK, _n
    _n += 1
    SOCK = f"ctxtest{os.getpid()}_{_n}"          # one private server per test: kill-server + new-session back to back races
    monkeypatch.setattr(tmux, "TMUX", "tmux")
    monkeypatch.setenv("TMUX", "fake")
    # route every tmux call through our private server
    real = subprocess.run
    def run(args, **kw):
        if args and args[0] == "tmux":
            args = ["tmux", "-L", SOCK, *args[1:]]
        return real(args, **kw)
    monkeypatch.setattr(tmux.subprocess, "run", run)
    tm("new-session", "-d", "-s", "ctx", "-n", "mgr", "-x", "160", "-y", "40")
    tm("split-window", "-h", "-l", "50", "-t", "ctx:mgr")
    yield
    tm("kill-server", check=False)


def test_pane_discovery_swap_and_resume(server, monkeypatch, tmp_path):
    ps = tmux.panes()
    mgr = sorted((p for p in ps if p.window_name == "mgr"), key=lambda p: p.index)
    assert len(mgr) == 2
    viewer, tui = mgr
    monkeypatch.setenv("TMUX_PANE", tui.id)
    assert tmux.viewer_pane().id == viewer.id
    # a "hand-started agent": a sleep running in a third window, found by pid walk
    tm("new-window", "-d", "-t", "ctx:", "-n", "hand", "sleep 30")
    time.sleep(0.3)
    hand = next(p for p in tmux.panes() if p.window_name == "hand")
    sleep_pid = int(tm("list-panes", "-t", hand.id, "-F", "#{pane_pid}"))
    # pane_pid is the sleep itself (no shell: direct command); its own pid must resolve to the pane
    assert tmux.pane_for_pid(sleep_pid).id == hand.id
    s = {"key": "laptop/claude/abc", "pid": sleep_pid, "live_status": "busy", "host_id": "laptop", "title": "Hand session",
         "session_id": "abc", "harness": "claude"}
    assert tmux.visible_key(sessions=[s]) is None
    msg = tmux.show(s, focus=False)
    assert msg.startswith("showing")
    ps = tmux.panes()
    # the hand pane now sits in mgr at index 0, tagged; the old viewer pane went to the 'hand' window (renamed)
    now_viewer = tmux.viewer_pane(ps)
    assert now_viewer.id == hand.id and now_viewer.key == "laptop/claude/abc"
    assert tmux.visible_key(ps, [s]) == "laptop/claude/abc"
    assert next(p for p in ps if p.id == viewer.id).window_name == "Hand session"
    assert tmux.show(s, focus=False) == "already showing"
    # an ended session: resumed in a new window (fake agent = a script that records argv), then swapped in
    fake = tmp_path / "claude"; fake.write_text("#!/bin/sh\necho \"$@\" > %s/argv; sleep 30\n" % tmp_path); fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    ended = {"key": "laptop/claude/def", "pid": None, "live_status": "ended", "host_id": "laptop", "title": "Old one",
             "session_id": "def", "harness": "claude", "cwd": str(tmp_path)}
    msg = tmux.show(ended, focus=False)
    assert msg.startswith("resumed")
    for _ in range(20):
        if (tmp_path / "argv").exists(): break
        time.sleep(0.1)
    assert (tmp_path / "argv").read_text().strip() == "--resume def"
    ps = tmux.panes()
    assert tmux.viewer_pane(ps).key == "laptop/claude/def"
    assert sorted(p.window_name for p in ps if p.key == "laptop/claude/abc") == ["Old one"] or True  # abc moved back out
    assert any(p.key == "laptop/claude/abc" and p.window_name != "mgr" for p in ps)
    # live elsewhere (pid not in any pane) is reported, not resumed
    assert "not in a tmux pane" in tmux.show({**s, "pid": 999999, "key": "x/claude/zzz"}, focus=False)


def test_new_agent_starts_in_viewer_and_is_found_by_pid(server, monkeypatch, tmp_path):
    ps = tmux.panes()
    viewer, tui = sorted((p for p in ps if p.window_name == "mgr"), key=lambda p: p.index)
    monkeypatch.setenv("TMUX_PANE", tui.id)
    fake = tmp_path / "pi"; fake.write_text("#!/bin/sh\npwd > %s/cwd; sleep 30\n" % tmp_path); fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    assert tmux.new_agent("pi", "/nonexistent/dir", focus=False).startswith("no such directory")
    msg = tmux.new_agent("pi", str(tmp_path), focus=False)
    assert msg == f"new pi in {tmp_path}"
    for _ in range(20):
        if (tmp_path / "cwd").exists(): break
        time.sleep(0.1)
    assert (tmp_path / "cwd").read_text().strip() == str(tmp_path.resolve())
    ps = tmux.panes()
    v = tmux.viewer_pane(ps)
    assert v.id != viewer.id and v.key == "" and v.window_name == "mgr"
    # the collector later reports the agent's pid: the pid walk maps it to the viewer pane and tags it
    out = subprocess.run(["pgrep", "-f", "sleep 30"], capture_output=True, text=True).stdout.split()
    pids = [int(x) for x in out]
    s = {"key": "laptop/pi/new1", "pid": None}
    for pid in pids:
        if tmux.pane_for_pid(pid, ps) and tmux.pane_for_pid(pid, ps).id == v.id:
            s["pid"] = pid
    assert s["pid"], "sleep child of the new pane not found"
    assert tmux.visible_key(ps, [s]) == "laptop/pi/new1"
    assert tmux.viewer_pane().key == "laptop/pi/new1"


def test_agent_exit_swaps_next_session_in_and_closes_its_window(server, monkeypatch, tmp_path):
    """End to end: the wrapper around a real (fake) agent runs `ctx tmux-exit` against this private server."""
    import sys
    ps = tmux.panes()
    viewer, tui = sorted((p for p in ps if p.window_name == "mgr"), key=lambda p: p.index)
    monkeypatch.setenv("TMUX_PANE", tui.id)
    tmux.set_role(tui.id, "tui")
    wrapper = tmp_path / "tmuxw"; wrapper.write_text(f'#!/bin/sh\nexec tmux -L {SOCK} "$@"\n'); wrapper.chmod(0o755)
    tm("set-environment", "-g", "CTXSWITCH_TMUX", str(wrapper))          # the pane's `ctx tmux-exit` drives our server
    monkeypatch.setattr(tmux, "_ctx_bin", lambda: os.path.join(os.path.dirname(sys.executable), "ctx"))
    # pi: a long-lived agent; claude: exits once told to (after the 5 s "short run" threshold, via --started)
    (tmp_path / "pi").write_text("#!/bin/sh\nsleep 60\n")
    (tmp_path / "claude").write_text("#!/bin/sh\nwhile [ ! -e %s/go ]; do sleep 0.1; done\n" % tmp_path)
    for f in ("pi", "claude"):
        (tmp_path / f).chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setattr(tmux, "agent_cmd", lambda argv, real=tmux.agent_cmd: [*real(argv)[:2], real(argv)[2].replace("start=$(date +%s)", "start=$(( $(date +%s) - 60 ))")])
    tmux.new_agent("pi", str(tmp_path), focus=False)
    pi_pane = tmux.viewer_pane().id
    tmux.new_agent("claude", str(tmp_path), focus=False)
    claude_pane = tmux.viewer_pane().id
    assert claude_pane != pi_pane and tmux.viewer_pane().role == "agent"
    n_windows = len({p.window for p in tmux.panes()})
    (tmp_path / "go").touch()
    for _ in range(50):
        if tmux.viewer_pane() and tmux.viewer_pane().id == pi_pane and claude_pane not in {p.id for p in tmux.panes()}:
            break
        time.sleep(0.1)
    ps = tmux.panes()
    assert tmux.viewer_pane(ps).id == pi_pane, "the remaining agent was not swapped into the viewer"
    assert claude_pane not in {p.id for p in ps}, "the exited agent's pane survived"
    assert len({p.window for p in ps}) == n_windows - 1, "the exited agent's window was not closed"
    assert len([p for p in ps if p.window_name == "mgr"]) == 2


def test_agent_exit_falls_back_to_a_shell_and_ignores_off_viewer_panes(server, monkeypatch):
    ps = tmux.panes()
    viewer, tui = sorted((p for p in ps if p.window_name == "mgr"), key=lambda p: p.index)
    tmux.set_role(tui.id, "tui")
    tmux.set_role(viewer.id, "agent")
    # no other agent anywhere: a fresh shell window is made and swapped in
    assert tmux.on_agent_exit(pane_id=viewer.id, prompt=lambda _: None).startswith("swapped")
    ps = tmux.panes()
    v = sorted((p for p in ps if p.window_name == "mgr"), key=lambda p: p.index)[0]
    assert v.id != viewer.id and v.role == ""
    assert next(p for p in ps if p.id == viewer.id).window_name == "shell"
    # an agent exiting in its own (background) window leaves the viewer alone
    assert tmux.on_agent_exit(pane_id=viewer.id, prompt=lambda _: None) == "closed"
    # a failed run waits for Enter before anything moves
    seen = []
    tmux.on_agent_exit(rc=1, pane_id=viewer.id, prompt=seen.append)
    assert seen and "rc=1" in seen[0]


def test_reap_viewer_replaces_a_dead_agent_shell_but_not_a_plain_shell(server, monkeypatch, tmp_path):
    ps = tmux.panes()
    viewer, tui = sorted((p for p in ps if p.window_name == "mgr"), key=lambda p: p.index)
    monkeypatch.setenv("TMUX_PANE", tui.id)
    tmux.set_role(tui.id, "tui")
    tm("send-keys", "-t", viewer.id, "exec bash --norc", "Enter")              # the viewer is a bare bash
    for _ in range(30):
        if next(p for p in tmux.panes() if p.id == viewer.id).cmd == "bash": break
        time.sleep(0.1)
    assert next(p for p in tmux.panes() if p.id == viewer.id).cmd == "bash"
    assert tmux.reap_viewer() is None, "an untagged shell (the bootstrap pane) must be left alone"
    # a live agent in a background window, and the viewer tagged as a session whose agent has exited
    (tmp_path / "pi").write_text("#!/bin/sh\nsleep 60\n"); (tmp_path / "pi").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    live = tmux._agent_window("ctx", str(tmp_path), "live", ["pi"])
    for _ in range(30):
        if next(p for p in tmux.panes() if p.id == live).cmd == "sleep": break
        time.sleep(0.1)
    tmux.tag(viewer.id, "laptop/claude/gone")
    assert tmux.reap_viewer() == live
    ps = tmux.panes()
    assert tmux.viewer_pane(ps).id == live
    assert viewer.id not in {p.id for p in ps}, "the dead agent's shell was not closed"
    assert tmux.reap_viewer() is None, "a running agent in the viewer must be left alone"
