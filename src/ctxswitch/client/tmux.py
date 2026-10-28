"""tmux integration: a manager window with the agent session in pane 0 and `ctx tui` in pane 1.

Every agent session lives in its own tmux window (so it survives terminal clients and nothing is ever killed);
"show" swaps the chosen session's pane into the viewer pane with ``tmux swap-pane``, and whatever was in the viewer
goes back out to that session's window. Sessions started by hand inside tmux are found by walking the agent's pid
up the process tree to a pane's shell pid. Panes we touch are tagged with the pane option ``@ctx_key`` so the
mapping survives pid changes.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass

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


def panes() -> list[Pane]:
    out = _run("list-panes", "-a", "-F", "#{pane_id}\t#{window_id}\t#{window_name}\t#{pane_index}\t#{pane_pid}\t#{@ctx_key}\t#{session_name}", check=False)
    res = []
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) < 7:
            continue
        res.append(Pane(f[0], f[1], f[2], int(f[3] or 0), int(f[4] or 0), f[5], f[6]))
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
    # keep the shell alive after the agent exits so the pane (and its scrollback) stays reachable
    cmd = shlex.join(argv) + "; exec ${SHELL:-bash}"
    pane_id = _run("new-window", "-d", "-t", target_session + ":", "-c", cwd, "-n", name, "-P", "-F", "#{pane_id}", cmd).strip()
    tag(pane_id, s["key"])
    _run("set-option", "-w", "-t", pane_id, "allow-rename", "off", check=False)
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
    cmd = shlex.join(argv) + "; exec ${SHELL:-bash}"
    pane_id = _run("new-window", "-d", "-t", target_session + ":", "-c", cwd, "-n", name, "-P", "-F", "#{pane_id}", cmd).strip()
    # no session key yet: the collector will see the transcript shortly and the pid walk maps it to this pane
    _run("set-option", "-w", "-t", pane_id, "allow-rename", "off", check=False)
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
    pane = pane_for_session(s, ps)
    created = False
    if pane is None:
        if s.get("live_status") in ("busy", "idle", "shell") and s.get("pid"):
            return f"live on {s.get('host_id')} but not in a tmux pane here"
        if s.get("host_id") and s["host_id"] != os.uname().nodename and s.get("host_id") != _this_host():
            return f"on {s['host_id']}: run `ctx show {s['session_id'][:8]}` there (cross-host resume is Phase 5)"
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


def bootstrap(width: int = 56) -> str:
    """`ctx tmux`: create (or join) the manager layout — viewer left, TUI right — and attach."""
    ctx_cmd = shlex.join([os.path.abspath(os.sys.argv[0]), "tui"]) if os.sys.argv else "ctx tui"
    if in_tmux():
        _run("split-window", "-h", "-l", str(width), "-c", os.getcwd(), ctx_cmd)
        _run("select-pane", "-t", "{left}", check=False)
        return "split: TUI on the right"
    has = subprocess.run([TMUX, "has-session", "-t", SESSION], capture_output=True).returncode == 0
    if not has:
        _run("new-session", "-d", "-s", SESSION, "-n", VIEWER_WINDOW, "-c", os.path.expanduser("~"))
        _run("split-window", "-h", "-l", str(width), "-t", f"{SESSION}:{VIEWER_WINDOW}", "-c", os.getcwd(), ctx_cmd)
        _run("set-option", "-t", SESSION, "allow-rename", "off", check=False)
        _run("select-pane", "-t", f"{SESSION}:{VIEWER_WINDOW}.0", check=False)
    os.execvp(TMUX, [TMUX, "attach-session", "-t", SESSION])
    return ""
