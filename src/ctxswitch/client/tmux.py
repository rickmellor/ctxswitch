"""tmux integration: a manager window with the agent session in pane 0 and `ctx tui` in pane 1.

Every agent session lives in its own tmux window (so it survives terminal clients);
"show" swaps the chosen session's pane into the viewer pane with ``tmux swap-pane``, and whatever was in the viewer
goes back out to that session's window. Sessions started by hand inside tmux are found by walking the agent's pid
up the process tree to a pane's shell pid. Panes we touch are tagged with the pane option ``@ctx_key`` so the
mapping survives pid changes. Agent windows we start run through a wrapper: when the agent exits, ``ctx tmux-exit``
swaps the most recently active other agent into the viewer (or a fresh shell if there is none) and the dead
session's window closes. The TUI pane carries ``@ctx_role=tui`` so the viewer window can be recognised.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

from ctxswitch.common.schema import LIVE

TMUX = os.environ.get("CTXSWITCH_TMUX", "tmux")
SESSION = os.environ.get("CTXSWITCH_TMUX_SESSION", "ctx")
VIEWER_WINDOW = "mgr"


def in_tmux() -> bool:
    return bool(os.environ.get("TMUX")) and bool(os.environ.get("TMUX_PANE"))


def _run(*args: str, check: bool = True) -> str:
    r = subprocess.run([TMUX, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"tmux {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


@dataclass
class Pane:
    id: str            # %N — stable for the pane's life
    window: str        # @N
    window_name: str
    index: int         # pane index within its window
    pid: int           # the pane's shell pid
    key: str           # @ctx_key or ""
    session: str       # tmux session name
    role: str = ""     # @ctx_role: "tui" for the ctx TUI pane, "agent" for agent panes we started
    activity: int = 0  # window_activity (epoch seconds)
    cmd: str = ""      # pane_current_command


def panes() -> list[Pane]:
    out = _run("list-panes", "-a", "-F", "#{pane_id}\t#{window_id}\t#{window_name}\t#{pane_index}\t#{pane_pid}\t#{@ctx_key}\t#{session_name}"
               "\t#{@ctx_role}\t#{window_activity}\t#{pane_current_command}", check=False)
    res = []
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) < 10:
            continue
        res.append(Pane(f[0], f[1], f[2], int(f[3] or 0), int(f[4] or 0), f[5], f[6], f[7], int(f[8] or 0), f[9]))
    return res


def _ppid(pid: int, proc: str = "/proc") -> int | None:
    try:
        with open(f"{proc}/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def pane_for_pid(pid: int | None, ps: list[Pane] | None = None, proc: str = "/proc") -> Pane | None:
    """The pane whose shell is an ancestor of ``pid`` (an agent started by hand inside tmux)."""
    if not pid:
        return None
    ps = ps if ps is not None else panes()
    by_pid = {p.pid: p for p in ps}
    cur: int | None = pid
    for _ in range(12):
        if cur is None or cur <= 1:
            return None
        if cur in by_pid:
            return by_pid[cur]
        cur = _ppid(cur, proc)
    return None


def pane_for_session(s: dict, ps: list[Pane] | None = None) -> Pane | None:
    ps = ps if ps is not None else panes()
    key = s.get("key", "")
    tagged = next((p for p in ps if p.key == key), None)
    if tagged:
        return tagged
    return pane_for_pid(s.get("pid"), ps)


def viewer_pane(ps: list[Pane] | None = None) -> Pane | None:
    """Pane 0 of the window the TUI runs in (the TUI itself is expected in pane 1)."""
    ps = ps if ps is not None else panes()
    me = os.environ.get("TMUX_PANE")
    mine = next((p for p in ps if p.id == me), None)
    if mine is None:
        return None
    others = sorted((p for p in ps if p.window == mine.window and p.id != me), key=lambda p: p.index)
    return others[0] if others else None


def tag(pane_id: str, key: str) -> None:
    _run("set-option", "-p", "-t", pane_id, "@ctx_key", key, check=False)


def set_role(pane_id: str | None, role: str) -> None:
    if pane_id:
        _run("set-option", "-p", "-t", pane_id, "@ctx_role", role, check=False)


def _ctx_bin() -> str:
    me = sys.argv[0] if sys.argv else ""
    if os.path.basename(me).startswith("ctx"):
        return os.path.abspath(me)
    return shutil.which("ctx") or "ctx"


def agent_cmd(argv: list[str]) -> list[str]:
    """Run ``argv``, then hand the pane to ``ctx tmux-exit``; a shell is the fallback if ctx itself fails."""
    script = (f"start=$(date +%s); {shlex.join(argv)}; rc=$?; "
              f"{shlex.quote(_ctx_bin())} tmux-exit --rc \"$rc\" --started \"$start\" || exec \"${{SHELL:-bash}}\"")
    return ["sh", "-c", script]


def _agent_window(target_session: str, cwd: str, name: str, argv: list[str]) -> str:
    pane_id = _run("new-window", "-d", "-t", target_session + ":", "-c", cwd, "-n", name, "-P", "-F", "#{pane_id}",
                   *agent_cmd(argv)).strip()
    set_role(pane_id, "agent")
    _run("set-option", "-w", "-t", pane_id, "allow-rename", "off", check=False)
    return pane_id


def on_agent_exit(rc: int = 0, started: float | None = None, pane_id: str | None = None, prompt=input) -> str:
    """The agent in ``pane_id`` has exited. If its pane is in the viewer, swap the most recently active other agent
    in (or a fresh shell); then return so the pane's process ends and its now-lone window closes.

    A failed or very short run (rc != 0, or under 5 s — e.g. a bad --resume) waits for Enter first so the error
    stays readable.
    """
    pane_id = pane_id or os.environ.get("TMUX_PANE")
    if rc != 0 or (started is not None and time.time() - started < 5):
        try:
            prompt(f"[agent exited rc={rc}] press Enter to close ")
        except EOFError:
            pass
    ps = panes()
    mine = next((p for p in ps if p.id == pane_id), None)
    if mine is None:
        return "pane gone"
    if not any(p.window == mine.window and p.role == "tui" for p in ps):
        return "closed"                                     # not on show: just let the window close
    return f"swapped {_hand_off(mine, ps)} into the viewer"


def _hand_off(mine: Pane, ps: list[Pane]) -> str:
    """Swap the most recently active other agent pane (or a fresh shell) into ``mine``'s place; returns its id."""
    others = [p for p in ps if p.session == mine.session and p.window != mine.window and p.id != mine.id
              and p.role != "tui" and (p.role == "agent" or p.key) and p.cmd not in SHELLS]
    others.sort(key=lambda p: p.activity, reverse=True)
    if others:
        repl = others[0].id
    else:
        repl = _run("new-window", "-d", "-t", mine.session + ":", "-c", os.path.expanduser("~"), "-n", "shell",
                    "-P", "-F", "#{pane_id}").strip()
    _run("swap-pane", "-s", repl, "-t", mine.id)
    _run("select-pane", "-t", repl, check=False)
    return repl


# a pane whose foreground is one of these has no agent running. "sh" is left out on purpose: it is the agent
# wrapper itself for the instant before the agent starts.
SHELLS = {"bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh"}


def reap_viewer(ps: list[Pane] | None = None) -> str | None:
    """Safety net run by the TUI: if the viewer holds a pane that had an agent (tagged by ctx) but is now at a bare
    shell — an agent started by hand in the viewer, or a window from before the exit wrapper — hand the viewer to
    the next session and close that pane."""
    if not in_tmux():
        return None
    ps = ps if ps is not None else panes()
    v = viewer_pane(ps)
    if v is None or not (v.key or v.role == "agent") or v.cmd not in SHELLS:
        return None
    repl = _hand_off(v, ps)
    _run("kill-pane", "-t", v.id, check=False)
    return repl


def resume_argv(s: dict) -> list[str] | None:
    harness, sid = s.get("harness"), s.get("session_id")
    if harness == "claude":
        return ["claude", "--resume", sid]
    if harness == "pi":
        path = (s.get("meta") or {}).get("transcript_path") or s.get("transcript_path")
        return ["pi", "--session", path] if path else None
    return None


def new_session_window(s: dict) -> Pane:
    """Resume the session in a new (detached) window of the viewer's tmux session; returns its pane."""
    argv = resume_argv(s)
    if not argv:
        raise RuntimeError(f"cannot resume a {s.get('harness')} session")
    cwd = s.get("cwd") or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    name = (s.get("title") or s.get("session_id") or "ctx")[:24].replace(":", "-")
    target_session = next((p.session for p in panes() if p.id == os.environ.get("TMUX_PANE")), SESSION)
    pane_id = _agent_window(target_session, cwd, name, argv)
    tag(pane_id, s["key"])
    ps = panes()
    return next(p for p in ps if p.id == pane_id)


def new_agent(harness: str, cwd: str, extra: list[str] | None = None, focus: bool = True) -> str:
    """Start a fresh claude/pi session in a new window of this tmux session and bring it into the viewer pane."""
    if not in_tmux():
        return "not inside tmux"
    argv = {"claude": ["claude"], "pi": ["pi"]}.get(harness)
    if argv is None:
        return f"unknown harness {harness}"
    argv = [*argv, *(extra or [])]
    cwd = os.path.expanduser(cwd.strip() or "~")
    if not os.path.isdir(cwd):
        return f"no such directory: {cwd}"
    ps = panes()
    viewer = viewer_pane(ps)
    if viewer is None:
        return "no viewer pane: split the window so the TUI has a neighbour (ctx tmux)"
    target_session = next((p.session for p in ps if p.id == os.environ.get("TMUX_PANE")), SESSION)
    name = f"{harness}:{os.path.basename(cwd.rstrip('/')) or cwd}"[:24]
    # no session key yet: the collector will see the transcript shortly and the pid walk maps it to this pane
    pane_id = _agent_window(target_session, cwd, name, argv)
    _run("swap-pane", "-s", pane_id, "-t", viewer.id)
    if focus:
        _run("select-pane", "-t", pane_id, check=False)
    return f"new {harness} in {cwd}"


def show(s: dict, focus: bool = True) -> str:
    """Bring session ``s`` into the viewer pane. Returns a short status message for the TUI."""
    if not in_tmux():
        return "not inside tmux"
    ps = panes()
    viewer = viewer_pane(ps)
    if viewer is None:
        return "no viewer pane: split the window so the TUI has a neighbour (ctx tmux)"
    if s.get("harness") == "claude-win":
        from ctxswitch.client.launch import windows_hint
        return windows_hint(s)
    r, pane = _reach(s, ps, _this_host())
    created = False
    if r == "remote":
        return f"on {s['host_id']}: run `ctx show {s['session_id'][:8]}` there (cross-host resume is Phase 5)"
    if r == "foreign":
        return f"running in another terminal (pid {s.get('pid')}): quit it there, then Enter resumes it here"
    if r == "none":
        return f"cannot resume a {s.get('harness')} session"
    if pane is None:
        pane = new_session_window(s)
        created = True
    if pane.id == viewer.id:
        if focus:
            _run("select-pane", "-t", viewer.id, check=False)
        return "already showing"
    tag(pane.id, s["key"])
    # name the session's home window after the session; the viewer window keeps its own name
    _run("rename-window", "-t", pane.window, (s.get("title") or s.get("session_id") or "ctx")[:24], check=False)
    _run("swap-pane", "-s", pane.id, "-t", viewer.id)
    if focus:
        _run("select-pane", "-t", pane.id, check=False)      # after the swap the session pane sits where the viewer was
    return ("resumed " if created else "showing ") + (s.get("title") or s.get("session_id", ""))[:40]


# How Enter can reach a session, as shown in the TUI's reach column:
#   pane     in a pane of this tmux server: Enter swaps it into the viewer
#   resume   not running here: Enter resumes it in a new window
#   foreign  running on this host in a terminal outside tmux: can't be adopted (quit it there, then resume)
#   remote   on another host: open it there
#   none     not running and no way to resume it
REACH_GLYPH = {"pane": "◆", "resume": "↻", "foreign": "⊘", "remote": "⇢", "none": " "}
REACH_HELP = {"pane": "in ctx's tmux: Enter brings it to the left pane",
              "resume": "not running: Enter resumes it in a new window",
              "foreign": "running in another terminal: can't be adopted; quit it there, then Enter resumes it here",
              "remote": "on another host: open it there",
              "none": "can't be resumed"}


def _reach(s: dict, ps: list[Pane], this_host: str) -> tuple[str, Pane | None]:
    host = s.get("host_id")
    if host and host != this_host and host != os.uname().nodename:
        return "remote", None
    pane = pane_for_session(s, ps) if ps else None
    if pane is not None:
        return "pane", pane
    if s.get("live_status") in LIVE and s.get("pid"):
        return "foreign", None
    return ("resume" if resume_argv(s) else "none"), None


def reach_all(sessions: list[dict], this_host: str | None = None) -> dict[str, str]:
    """key -> reach for every session, with one tmux query."""
    ps = panes() if in_tmux() else []
    this_host = this_host or _this_host()
    return {s["key"]: _reach(s, ps, this_host)[0] for s in sessions}


def my_session(ps: list[Pane] | None = None) -> str | None:
    ps = ps if ps is not None else panes()
    return next((p.session for p in ps if p.id == os.environ.get("TMUX_PANE")), None)


def detach() -> None:
    """Detach this terminal; the TUI and every agent keep running (``ctx tmux`` re-attaches)."""
    _run("detach-client")


def agent_panes(ps: list[Pane] | None = None) -> list[Pane]:
    """Panes in this tmux session with an agent still running in them."""
    ps = ps if ps is not None else panes()
    me = my_session(ps)
    return [p for p in ps if p.session == me and p.role != "tui" and (p.role == "agent" or p.key) and p.cmd not in SHELLS]


def kill_all() -> None:
    """End this tmux session: every agent in it gets SIGHUP (transcripts stay; they show as resumable)."""
    me = my_session()
    if me:
        _run("kill-session", "-t", me)


def _this_host() -> str:
    try:
        from ctxswitch.common.config import load
        return load().host_id
    except Exception:                                      # noqa: BLE001
        return os.uname().nodename


def visible_key(ps: list[Pane] | None = None, sessions: list[dict] | None = None) -> str | None:
    """Key of the session currently in the viewer pane, if any."""
    if not in_tmux():
        return None
    ps = ps if ps is not None else panes()
    v = viewer_pane(ps)
    if v is None:
        return None
    known = {s["key"] for s in sessions or []}
    if v.key and (not known or v.key in known):
        return v.key
    for s in sessions or []:                            # untagged (or stale tag): map by the agent's pid
        p = pane_for_pid(s.get("pid"), ps)
        if p and p.id == v.id:
            tag(v.id, s["key"])
            return s["key"]
    return None


def bootstrap(width: str = "25%") -> str:
    """`ctx tmux`: create (or join) the manager layout — viewer left, TUI right — and attach.

    ``width`` is a tmux size: a column count ("56") or a percentage ("25%").
    """
    ctx_cmd = shlex.join([os.path.abspath(os.sys.argv[0]), "tui"]) if os.sys.argv else "ctx tui"
    if in_tmux():
        _run("split-window", "-h", "-l", width, "-c", os.getcwd(), ctx_cmd)
        _run("select-pane", "-t", "{left}", check=False)
        return "split: TUI on the right"
    has = subprocess.run([TMUX, "has-session", "-t", SESSION], capture_output=True).returncode == 0
    if not has:
        _run("new-session", "-d", "-s", SESSION, "-n", VIEWER_WINDOW, "-c", os.path.expanduser("~"))
        _run("split-window", "-h", "-l", width, "-t", f"{SESSION}:{VIEWER_WINDOW}", "-c", os.getcwd(), ctx_cmd)
        _run("set-option", "-t", SESSION, "allow-rename", "off", check=False)
        _run("select-pane", "-t", f"{SESSION}:{VIEWER_WINDOW}.0", check=False)
    os.execvp(TMUX, [TMUX, "attach-session", "-t", SESSION])
    return ""
