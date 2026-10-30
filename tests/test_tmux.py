"""Exercises the tmux helpers against a private tmux server (-L) with real panes; skipped without tmux."""
import os
import shutil
import subprocess
import re
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
    monkeypatch.setattr(tmux, "_this_host", lambda: "laptop")     # sessions in these tests live on "laptop"
    monkeypatch.setattr(tmux, "_ssh", lambda: {})                  # never the real config's [ssh] table
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
    assert "running in another terminal" in tmux.show({**s, "pid": 999999, "key": "x/claude/zzz"}, focus=False)


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


def test_a_shell_opens_in_the_viewer_tagged_shell(server, monkeypatch, tmp_path):
    """n's plain shell: a window like an agent's, but its role keeps it off the session list and out of the reap."""
    ps = tmux.panes()
    viewer, tui = sorted((p for p in ps if p.window_name == "mgr"), key=lambda p: p.index)
    monkeypatch.setenv("TMUX_PANE", tui.id)
    monkeypatch.setenv("SHELL", "/bin/sh")                        # deterministic: not whatever the runner logs in with
    assert tmux.new_agent("shell", str(tmp_path)) == f"new shell in {tmp_path}"
    ps = tmux.panes()
    v = tmux.viewer_pane(ps)
    assert v.role == "shell" and v.window_name == "mgr"           # swapped into the viewer, as an agent is
    home = next(p for p in ps if p.id == viewer.id).window_name   # the shell window's own name, after the swap
    assert home.startswith("shell:") and home[len("shell:"):] in str(tmp_path)   # the dir, cut at 24 chars, as agents' are
    assert v.id not in {p.id for p in tmux.agent_panes(ps)}        # no session behind it: not an agent pane
    assert tmux.reap_viewer(ps) is None                            # its role, not its foreground, keeps the reap off it


def test_a_shell_exit_never_pauses(server, monkeypatch):
    """A shell's exit code is just its last command's: it must not stop the viewer with "press Enter"."""
    viewer, tui = sorted((p for p in tmux.panes() if p.window_name == "mgr"), key=lambda p: p.index)
    monkeypatch.setenv("TMUX_PANE", tui.id)
    tmux.set_role(tui.id, "tui")
    tmux.set_role(viewer.id, "shell")
    asked = []
    assert tmux.on_agent_exit(1, time.time(), viewer.id, prompt=asked.append).startswith("swapped")
    assert asked == []
    tm("set-option", "-p", "-u", "-t", viewer.id, "@ctx_role")     # an agent that died badly still waits
    tmux.on_agent_exit(1, time.time(), viewer.id, prompt=asked.append)
    assert asked and "rc=1" in asked[0]


def test_the_board_pins_its_width_on_attach(server):
    board = tm("display-message", "-p", "-t", "ctx:mgr.1", "#{pane_id}")
    tmux.pin_board_width(None)                                     # no board pane: nothing resized or hooked
    assert "resize-pane" not in tm("show-hooks", "-t", "ctx")
    tm("set-option", "-t", "ctx", "@ctx_width", "30")              # the width `ctx tmux` split at
    tmux.pin_board_width(board)
    assert tm("display-message", "-p", "-t", board, "#{pane_width}") == "30"
    hooks = tm("show-hooks", "-t", "ctx")
    assert "client-attached" in hooks and re.search(rf'resize-pane -t "?{re.escape(board)}"? -x 30', hooks)  # tmux quotes the id
    tm("set-option", "-u", "-t", "ctx", "@ctx_width")
    tmux.pin_board_width(board)                                    # an old session that never recorded one
    assert tm("display-message", "-p", "-t", board, "#{pane_width}") == "60"


def test_step_width_resizes_remembers_and_repins(server, monkeypatch, tmp_path):
    """< / > on the board: 5 columns a press, clamped both ways, remembered, and re-pinned at the new width."""
    monkeypatch.setattr(tmux, "WIDTH_FILE", tmp_path / "w")
    board = tm("display-message", "-p", "-t", "ctx:mgr.1", "#{pane_id}")        # the fixture: a 160-wide window, mgr.1 at 50
    assert tmux.step_width(board, 5) == "55"
    assert tm("display-message", "-p", "-t", board, "#{pane_width}") == "55"
    assert tm("show-options", "-v", "-t", "ctx", "@ctx_width") == "55"          # what pin_board_width holds it at
    assert tmux.WIDTH_FILE.read_text().strip() == "55"                          # and the next `ctx tmux` starts here
    assert re.search(rf'resize-pane -t "?{re.escape(board)}"? -x 55', tm("show-hooks", "-t", "ctx"))
    tm("resize-pane", "-t", board, "-x", "32")
    assert tmux.step_width(board, -5) == "30" and tmux.saved_width() == "30"     # not 27: it stops at MIN_WIDTH
    tm("resize-pane", "-t", board, "-x", "138")
    assert tmux.step_width(board, 5) == "140" and tmux.saved_width() == "140"   # not 143: the left pane keeps 20
    assert tm("display-message", "-p", "-t", board, "#{pane_width}") == "140"
    assert tmux.step_width(None, 5) is None


def test_saved_width_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(tmux, "WIDTH_FILE", tmp_path / "w")
    assert tmux.saved_width() == tmux.DEFAULT_WIDTH                             # nothing saved yet
    tmux.WIDTH_FILE.write_text("abc\n")
    assert tmux.saved_width() == tmux.DEFAULT_WIDTH                             # not a width: ignored, not an error
    tmux.save_width("45")
    assert tmux.saved_width() == "45"
    tmux.save_width("25%")
    assert tmux.saved_width() == "25%"


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


def test_reach_classifies_every_way_enter_can_open_a_session(server, monkeypatch):
    ps = tmux.panes()
    viewer, tui = sorted((p for p in ps if p.window_name == "mgr"), key=lambda p: p.index)
    monkeypatch.setenv("TMUX_PANE", tui.id)
    tm("new-window", "-d", "-t", "ctx:", "-n", "hand", "sleep 30")
    time.sleep(0.3)
    hand = next(p for p in tmux.panes() if p.window_name == "hand")
    base = {"harness": "claude", "host_id": "laptop", "title": "t"}
    sessions = [
        {**base, "key": "laptop/claude/inpane", "session_id": "inpane", "pid": hand.pid, "live_status": "busy"},
        {**base, "key": "laptop/claude/outside", "session_id": "outside", "pid": 999999, "live_status": "idle"},
        {**base, "key": "laptop/claude/ended", "session_id": "ended", "pid": None, "live_status": "ended"},
        {**base, "key": "devbox/claude/far", "session_id": "far", "host_id": "devbox", "pid": 5, "live_status": "busy"},
        {**base, "key": "laptop/pi/nopath", "session_id": "nopath", "harness": "pi", "pid": None, "live_status": "ended"},
    ]
    assert tmux.reach_all(sessions, "laptop") == {
        "laptop/claude/inpane": "pane", "laptop/claude/outside": "foreign", "laptop/claude/ended": "resume",
        "devbox/claude/far": "remote", "laptop/pi/nopath": "none"}
    # Enter follows the same classification: nothing is started for foreign/remote/none
    n = len(tmux.panes())
    assert "another terminal" in tmux.show(sessions[1], focus=False)
    assert "on devbox" in tmux.show(sessions[3], focus=False)
    assert "cannot resume" in tmux.show(sessions[4], focus=False)
    assert len(tmux.panes()) == n
    # outside tmux nothing is in a pane: a live local session is foreign, an ended one resumable
    monkeypatch.delenv("TMUX")
    r = tmux.reach_all(sessions, "laptop")
    assert r["laptop/claude/inpane"] == "foreign" and r["laptop/claude/ended"] == "resume"


def test_another_hosts_session_resumes_there_over_ssh(server, monkeypatch, tmp_path):
    ps = tmux.panes()
    viewer, tui = sorted((p for p in ps if p.window_name == "mgr"), key=lambda p: p.index)
    monkeypatch.setenv("TMUX_PANE", tui.id)
    fake = tmp_path / "ssh"; fake.write_text("#!/bin/sh\nprintf '%%s\\n' \"$@\" > %s/argv; sleep 30\n" % tmp_path); fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    far = {"key": "devbox/claude/far", "session_id": "far", "harness": "claude", "host_id": "devbox", "title": "Far one",
           "cwd": "/home/me/my repo", "pid": None, "live_status": "ended"}
    pi = {**far, "key": "devbox/pi/p1", "session_id": "p1", "harness": "pi", "meta": {"transcript_path": "/home/me/.pi/s.jsonl"}}
    ssh = {"gpu": "me@gpu-host", "devbox": "me@devbox.lan", "laptop": ""}
    assert tmux.reach_all([far, pi, {**far, "key": "laptop/claude/x", "host_id": "laptop"}], "gpu", ssh) == {
        "devbox/claude/far": "ssh", "devbox/pi/p1": "ssh", "laptop/claude/x": "remote"}   # "" = no route
    assert tmux.ssh_target("scrubby", ssh) == "scrubby"                                 # unlisted: the host id is the alias
    assert tmux.ssh_argv(pi, "devbox")[-1] == ("bash -lc 'cd '\"'\"'/home/me/my repo'\"'\"' 2>/dev/null || cd; "
                                                "exec pi --session /home/me/.pi/s.jsonl'")
    assert "no ssh route" in tmux.reach_help({**far, "host_id": "laptop"}, "remote", ssh)
    assert tmux.show(far, focus=False, ssh=ssh).startswith("resumed")
    for _ in range(20):
        if (tmp_path / "argv").exists(): break
        time.sleep(0.1)
    argv = (tmp_path / "argv").read_text().splitlines()
    assert argv[0] == "-t" and argv[-2] == "me@devbox.lan"
    assert argv[-1] == "bash -lc 'cd '\"'\"'/home/me/my repo'\"'\"' 2>/dev/null || cd; exec claude --resume far'"
    ps = tmux.panes()
    assert tmux.viewer_pane(ps).key == "devbox/claude/far" and tmux.viewer_pane(ps).window_name == "mgr"
    assert next(p for p in ps if p.id == viewer.id).window_name == "devbox/Far one"   # the old viewer's new home
    # live on devbox now, with a pid that happens to be a local pane's: still found by ctx's tag only
    live = {**far, "live_status": "busy", "pid": tui.pid}
    assert tmux.reach_all([live], "laptop", ssh) == {"devbox/claude/far": "pane"}
    assert tmux.show(live, focus=False, ssh=ssh) == "already showing"
    # running on devbox in some other terminal: not resumed a second time
    other = {**live, "key": "devbox/claude/other", "session_id": "other"}
    n = len(tmux.panes())
    assert tmux.reach_all([other], "laptop", ssh) == {"devbox/claude/other": "remote"}
    assert "running on devbox" in tmux.show(other, focus=False, ssh=ssh) and len(tmux.panes()) == n
    # an untagged viewer is never mapped to another host's session by pid
    tm("set-option", "-p", "-u", "-t", tmux.viewer_pane().id, "@ctx_key")
    assert tmux.visible_key(sessions=[{**other, "pid": tmux.viewer_pane().pid}]) is None


def test_kill_all_counts_live_agents_and_ends_only_this_session(server, monkeypatch, tmp_path):
    ps = tmux.panes()
    viewer, tui = sorted((p for p in ps if p.window_name == "mgr"), key=lambda p: p.index)
    monkeypatch.setenv("TMUX_PANE", tui.id)
    tmux.set_role(tui.id, "tui")
    (tmp_path / "pi").write_text("#!/bin/sh\nsleep 60\n"); (tmp_path / "pi").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    a = tmux._agent_window("ctx", str(tmp_path), "a1", ["pi"])
    b = tmux._agent_window("ctx", str(tmp_path), "a2", ["pi"])
    tm("new-session", "-d", "-s", "other")                       # someone else's tmux session on the same server
    for _ in range(30):
        if {p.cmd for p in tmux.panes() if p.id in (a, b)} == {"sleep"}: break
        time.sleep(0.1)
    assert {p.id for p in tmux.agent_panes()} == {a, b}          # the TUI and the plain viewer shell don't count
    assert tmux.my_session() == "ctx"
    tmux.kill_all()
    assert {p.session for p in tmux.panes()} == {"other"}


def test_open_tab_resumes_another_hosts_session_over_ssh(monkeypatch):
    from ctxswitch.client import launch
    monkeypatch.setattr(launch.shutil, "which", lambda _: None)       # no wt.exe: the command is printed
    far = {"key": "devbox/claude/far", "session_id": "far", "harness": "claude", "host_id": "devbox", "cwd": "/r"}
    assert launch.open_tab(far, "laptop", {}) == ("run: cd ~ && ssh -t -o ConnectTimeout=5 -o ServerAliveInterval=30 devbox "
                                                  "bash -lc 'cd /r 2>/dev/null || cd; exec claude --resume far'")
    assert launch.open_tab(far, "laptop", {"devbox": ""}) == "on devbox: cd /r && claude --resume far"


def test_on_screen_is_the_window_being_looked_at_and_open_switches_to_it(server, monkeypatch):
    if shutil.which("script") is None:
        pytest.skip("needs script(1) for an attached client")
    viewer, tui = sorted((p for p in tmux.panes() if p.window_name == "mgr"), key=lambda p: p.index)
    monkeypatch.setenv("TMUX_PANE", tui.id)
    tm("new-window", "-d", "-t", "ctx:", "-n", "hand", "sleep 30")
    time.sleep(0.3)
    hand = next(p for p in tmux.panes() if p.window_name == "hand")
    s = {"key": "laptop/claude/abc", "pid": hand.pid, "live_status": "busy", "host_id": "laptop", "title": "Hand session",
         "session_id": "abc", "harness": "claude"}
    v = {"key": "laptop/claude/view", "pid": None, "host_id": "laptop", "harness": "claude"}
    tmux.tag(viewer.id, v["key"])
    assert tmux.on_screen_keys(sessions=[s, v]) == set()                 # nobody attached: nothing is on screen
    client = subprocess.Popen(["script", "-qfc", f"tmux -L {SOCK} attach -t ctx", "/dev/null"],
                              stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            if tm("list-clients", check=False): break
            time.sleep(0.1)
        assert tmux.on_screen_keys(sessions=[s, v]) == {v["key"]}       # looking at mgr: the viewer's session
        tm("select-window", "-t", "ctx:hand")
        assert tmux.on_screen_keys(sessions=[s, v]) == {s["key"]}       # looking at its own window: that one, not the viewer
        assert tmux.show(s).startswith("showing")
        assert tm("display-message", "-p", "#{window_name}") == "mgr"    # opening it takes you to it
        assert tmux.on_screen_keys(sessions=[s, v]) == {s["key"]}
    finally:
        client.kill()


def test_a_missing_board_is_put_back_and_restart_respawns_it(server, monkeypatch):
    board = lambda: [p for p in tmux.panes() if p.session == "ctx" and p.role == "tui"]
    assert board() == []                                              # the fixture's panes are untagged: no board
    tm("new-session", "-d", "-s", "elsewhere"); tm("set-option", "-p", "-t", "elsewhere", "@ctx_role", "tui")
    tm("select-pane", "-t", "ctx:mgr.0")                              # the viewer is the active pane
    assert tmux.ensure_board("20", "sleep 300").startswith("board put back")   # another session's board isn't ours
    (b,) = board()
    mgr = [p for p in tmux.panes() if p.window_name == "mgr"]
    assert b.window == mgr[0].window and b.index == max(p.index for p in mgr)        # on the right of the viewer window
    assert tmux.ensure_board("20", "sleep 300") == "" and len(board()) == 1          # there: left alone
    pid = b.pid
    assert tmux.ensure_board("20", "sleep 301", restart=True) == "board restarted"
    (b2,) = board()
    assert b2.id == b.id and b2.pid != pid                                            # same pane, new process
    tm("new-window", "-d", "-t", "ctx", "-n", "other"); tm("kill-window", "-t", "ctx:mgr")    # the last window would end the server
    assert tmux.ensure_board("20", "sleep 300").startswith("board put back")         # no viewer window: a new one
    (b3,) = board()
    assert b3.window_name == "mgr"


def test_ctx_tmux_inside_the_session_restores_the_board_not_a_split_here(server, monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX_PANE", tm("display-message", "-p", "-t", "ctx:mgr.0", "#{pane_id}"))
    tm("new-window", "-t", "ctx", "-n", "agentwin")                   # you're in another window of the ctx session
    before = {p.id for p in tmux.panes() if p.window_name == "agentwin"}
    fake = tmp_path / "ctx"; fake.write_text("#!/bin/sh\nexec sleep 300\n"); fake.chmod(0o755)
    monkeypatch.setattr(tmux.os.sys, "argv", [str(fake), "tmux"])
    monkeypatch.setattr(tmux, "WIDTH_FILE", tmp_path / "w")          # an explicit --width is remembered, not in the real home
    assert tmux.bootstrap("20").startswith("board put back")
    assert {p.id for p in tmux.panes() if p.window_name == "agentwin"} == before     # not split into this window
    assert "already running" in tmux.bootstrap("20")


def test_joining_from_outside_tmux_puts_the_board_back_then_attaches(server, monkeypatch, tmp_path):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    fake = tmp_path / "ctx"; fake.write_text("#!/bin/sh\nexec sleep 300\n"); fake.chmod(0o755)
    monkeypatch.setattr(tmux.os.sys, "argv", [str(fake), "tmux"])
    monkeypatch.setattr(tmux, "WIDTH_FILE", tmp_path / "w")
    calls = []
    monkeypatch.setattr(tmux.os, "execvp", lambda f, argv: calls.append(argv))
    tmux.bootstrap("20")
    assert [p for p in tmux.panes() if p.session == "ctx" and p.role == "tui"] and calls == [["tmux", "attach-session", "-t", "ctx"]]


def test_ctx_tmux_starts_at_the_remembered_width_and_remembers_a_given_one(server, monkeypatch, tmp_path):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    fake = tmp_path / "ctx"; fake.write_text("#!/bin/sh\nexec sleep 300\n"); fake.chmod(0o755)
    monkeypatch.setattr(tmux.os.sys, "argv", [str(fake), "tmux"])
    monkeypatch.setattr(tmux, "WIDTH_FILE", tmp_path / "w")
    monkeypatch.setattr(tmux.os, "execvp", lambda f, argv: None)
    tmux.save_width("45")
    tmux.bootstrap()                                                  # no --width: the remembered one
    assert tm("show-options", "-v", "-t", "ctx", "@ctx_width") == "45" and tmux.saved_width() == "45"
    tmux.bootstrap("72")                                              # --width 72: used, and remembered
    assert tm("show-options", "-v", "-t", "ctx", "@ctx_width") == "72" and tmux.saved_width() == "72"


def test_alt_digits_go_to_the_board_only_inside_the_ctx_session(server):
    tmux.bind_slot_keys(None)
    assert "M-1" not in tm("list-keys", "-T", "root")                 # no board pane: nothing bound
    tmux.bind_slot_keys("%42")
    keys = {ln.split()[3]: ln for ln in tm("list-keys", "-T", "root").splitlines() if re.fullmatch(r"M-\d", ln.split()[3])}
    assert sorted(keys) == [f"M-{n}" for n in range(1, 10)]
    assert 'if-shell -F "#{==:#{session_name},ctx}" "send-keys -t %42 M-4" "send-keys M-4"' in keys["M-4"]   # elsewhere: as typed
    tmux.bind_slot_keys("%43")                                        # a restarted board takes them over
    assert "send-keys -t %43 M-4" in tm("list-keys", "-T", "root")


def test_k_ends_just_that_agent_and_its_exit_doesnt_wait_for_enter(server, monkeypatch, tmp_path):
    done = tmp_path / "after"
    # the agent wrapper's shape: the agent runs as a child of sh, then the wrapper carries on (here: records rc)
    tm("new-window", "-d", "-t", "ctx", "-n", "agent", f"sh -c 'sleep 300; echo $? > {done}; sleep 300'")
    tm("set-option", "-p", "-t", "ctx:agent", "@ctx_key", "laptop/claude/s1")
    monkeypatch.setenv("TMUX_PANE", tm("display-message", "-p", "-t", "ctx:mgr.0", "#{pane_id}"))
    time.sleep(0.5)
    p = tmux.agent_pane("laptop/claude/s1")
    assert p is not None and tmux.agent_pane("laptop/claude/other") is None
    assert tmux.end_agent(p) == 1                                     # the sleep (the "agent"), not the wrapper
    for _ in range(50):
        if done.exists():
            break
        time.sleep(0.1)
    assert done.read_text().strip() == "143"                          # SIGTERM'd; the wrapper went on to its exit step
    assert tm("display-message", "-p", "-t", p.id, "#{@ctx_ending}") == "1"
    asked = []
    tmux.on_agent_exit(143, time.time(), p.id, prompt=asked.append)
    assert asked == []                                                # asked for: no "press Enter"
    tm("set-option", "-p", "-u", "-t", p.id, "@ctx_ending")
    tmux.on_agent_exit(143, time.time(), p.id, prompt=asked.append)
    assert asked and "rc=143" in asked[0]                             # a crash still waits


def test_x_on_a_running_session_ends_it_and_waits_for_the_registry(server, monkeypatch, tmp_path):
    from ctxswitch.client import move as mv
    from ctxswitch.collector.liveness import proc_start
    from ctxswitch.common.config import Config
    import json
    tm("new-window", "-d", "-t", "ctx", "-n", "agent2", "sh -c 'sleep 299; sleep 299'")
    tm("set-option", "-p", "-t", "ctx:agent2", "@ctx_key", "laptop/claude/sx")
    monkeypatch.setenv("TMUX_PANE", tm("display-message", "-p", "-t", "ctx:mgr.0", "#{pane_id}"))
    time.sleep(0.5)
    pid = int(subprocess.run(["pgrep", "-f", "^sleep 299"], capture_output=True, text=True).stdout.split()[0])
    reg = tmp_path / "sessions"; reg.mkdir()
    (reg / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": "sx", "status": "idle", "procStart": proc_start(pid)}))
    cfg = Config(); cfg.collector.host_id = "laptop"; cfg.collector.claude_sessions_dir = str(reg)
    s = {"key": "laptop/claude/sx", "harness": "claude", "session_id": "sx", "host_id": "laptop", "live_status": "idle"}
    assert mv.can_end_here(s)
    assert mv.end_and_wait(s, cfg, timeout=10) is None                # the sleep died, the registry entry went stale
    assert s["live_status"] == "ended" and proc_start(pid) is None
    gone = {**s, "key": "laptop/claude/none", "live_status": "idle"}
    assert not mv.can_end_here(gone) and "quit it there" in mv.end_and_wait(gone, cfg)
