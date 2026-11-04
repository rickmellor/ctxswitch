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
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ctxswitch.common.schema import LIVE

TMUX = os.environ.get("CTXSWITCH_TMUX", "tmux")
SESSION = os.environ.get("CTXSWITCH_TMUX_SESSION", "ctx")
VIEWER_WINDOW = "mgr"
# the board's width, remembered across runs: `<` `>` on the board and `ctx tmux --width` both write it here
WIDTH_FILE = Path.home() / ".local/state/ctxswitch/board-width"
DEFAULT_WIDTH = "60"
WIDTH_STEP = 5
MIN_WIDTH = 30


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
    role: str = ""     # @ctx_role: "tui" for the ctx TUI pane, "agent" for agent panes we started,
                       # "shell" for a plain shell session's pane
    activity: int = 0  # window_activity (epoch seconds)
    cmd: str = ""      # pane_current_command
    on_screen: bool = False   # in the current window of a tmux session a client is attached to


def panes() -> list[Pane]:
    out = _run("list-panes", "-a", "-F", "#{pane_id}\t#{window_id}\t#{window_name}\t#{pane_index}\t#{pane_pid}\t#{@ctx_key}\t#{session_name}"
               "\t#{@ctx_role}\t#{window_activity}\t#{pane_current_command}\t#{&&:#{window_active},#{session_attached}}", check=False)
    res = []
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) < 11:
            continue
        res.append(Pane(f[0], f[1], f[2], int(f[3] or 0), int(f[4] or 0), f[5], f[6], f[7], int(f[8] or 0), f[9], f[10] == "1"))
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
    # its own process, else the one showing it (a Claude window whose conversation went to a background job)
    return pane_for_pid(s.get("pid"), ps) or pane_for_pid(s.get("via_pid"), ps)


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


def _agent_window(target_session: str, cwd: str, name: str, argv: list[str], role: str = "agent") -> str:
    pane_id = _run("new-window", "-d", "-t", target_session + ":", "-c", cwd, "-n", name, "-P", "-F", "#{pane_id}",
                   *agent_cmd(argv)).strip()
    set_role(pane_id, role)
    _run("set-option", "-w", "-t", pane_id, "allow-rename", "off", check=False)
    return pane_id


def on_agent_exit(rc: int = 0, started: float | None = None, pane_id: str | None = None, prompt=input) -> str:
    """The agent in ``pane_id`` has exited. If its pane is in the viewer, swap the most recently active other agent
    in (or a fresh shell); then return so the pane's process ends and its now-lone window closes.

    A failed or very short run (rc != 0, or under 5 s — e.g. a bad --resume) waits for Enter first so the error
    stays readable. A plain shell's exit code is just its last command's, so it never pauses.
    """
    pane_id = pane_id or os.environ.get("TMUX_PANE")
    asked = _run("display-message", "-p", "-t", pane_id or "", "#{@ctx_ending}", check=False).strip() == "1"
    role = _run("display-message", "-p", "-t", pane_id or "", "#{@ctx_role}", check=False).strip()
    if not asked and role != "shell" and (rc != 0 or (started is not None and time.time() - started < 5)):   # k ended it: no pause
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
    """Swap the most recently active other session pane — an agent's or one of our shell sessions' — (or a fresh
    shell) into ``mine``'s place; returns its id."""
    others = [p for p in ps if p.session == mine.session and p.window != mine.window and p.id != mine.id
              and managed(p) and (p.role == "shell" or p.cmd not in SHELLS)]
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


def restore_viewer(board: str | None = None, ps: list[Pane] | None = None) -> str | None:
    """The board alone in its window — the left pane closed (Ctrl+b x, a killed wrapper): put one back on its left,
    the most recently active session pane from another window of the ctx session, else a fresh shell in ~, then hold
    the board at its width again. Only in the ctx session and only when nothing else shares the board's window, so a
    pane you made yourself always stays the viewer. Returns what it did, or None."""
    if not in_tmux():
        return None
    ps = ps if ps is not None else panes()
    board = board or os.environ.get("TMUX_PANE")
    me = next((p for p in ps if p.id == board and p.role == "tui"), None)
    if me is None or me.session != SESSION or any(p.window == me.window and p.id != me.id for p in ps):
        return None
    others = [p for p in ps if p.session == me.session and p.window != me.window and managed(p)
              and (p.role == "shell" or p.cmd not in SHELLS)]
    others.sort(key=lambda p: p.activity, reverse=True)
    if others:
        _run("join-pane", "-d", "-h", "-b", "-s", others[0].id, "-t", me.id)
        said = f"put {others[0].id} back on the left"
    else:
        _run("split-window", "-d", "-h", "-b", "-t", me.id, "-c", os.path.expanduser("~"))
        said = "put a shell back on the left"
    pin_board_width(me.id)
    return said


def reap_viewer(ps: list[Pane] | None = None) -> str | None:
    """Safety net run by the TUI: if the viewer holds a pane that had an agent (tagged by ctx) but is now at a bare
    shell — an agent started by hand in the viewer, or a window from before the exit wrapper — hand the viewer to
    the next session and close that pane. One of our shell sessions is never reaped: at a bare prompt it is idle,
    not dead."""
    if not in_tmux():
        return None
    ps = ps if ps is not None else panes()
    v = viewer_pane(ps)
    if v is None or v.role == "shell":
        return None
    if not (v.key or v.role == "agent") or v.cmd not in SHELLS:
        return None
    repl = _hand_off(v, ps)
    _run("kill-pane", "-t", v.id, check=False)
    return repl


def _profiles() -> list:
    from ctxswitch.common.config import load
    return load().claude_profiles


def resume_argv(s: dict, profiles: list | None = None, extra: list[str] | None = None) -> list[str] | None:
    """The argv that resumes ``s`` on its own host. A Claude session resumes under the profile (account, config
    directory) its transcript lives in, so a Max session comes back as Max. ``extra`` goes after ``--resume``."""
    from ctxswitch.common.config import profile_for
    harness, sid = s.get("harness"), s.get("session_id")
    if harness == "claude":
        return [*profile_for(s, _profiles() if profiles is None else profiles).argv(), "--resume", sid, *(extra or [])]
    if harness == "pi":
        path = (s.get("meta") or {}).get("transcript_path") or s.get("transcript_path")
        return ["pi", "--session", path] if path else None
    if harness == "agy":
        return ["agy", "--conversation", sid, *(extra or [])]
    if harness == "shell":
        return [_ctx_bin(), "shell", "--id", sid]      # a new bash on the same transcript and history
    return None


def ssh_target(host: str | None, ssh: dict[str, str]) -> str | None:
    """The ssh destination for ``host``: its [ssh] entry, else the host id itself; None when set to ""."""
    if not host:
        return None
    return ssh.get(host, host) or None


def ssh_argv(s: dict, target: str) -> list[str] | None:
    """Resume ``s`` on its own host over ssh. A login shell, because the agents are often only on a login PATH.
    Profiles are per machine, so the remote resumes with its own config's profile for that transcript."""
    argv = resume_argv(s, [])
    if not argv:
        return None
    inner = f"cd {shlex.quote(s.get('cwd') or '~')} 2>/dev/null || cd; exec {shlex.join(argv)}"
    return ["ssh", "-t", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=30", target, shlex.join(["bash", "-lc", inner])]


# The agent on the other host runs in a tmux session named ``ctx`` there — its own tmux, as its own user — so it
# survives the ssh dropping. The local window only ever runs one of these two: remote_script (start or resume an
# agent over there) and view_command (attach and watch what's already running).
REMOTE_SESSION = "ctx"


def _tmux_at_parts(tmux_at: str | None) -> tuple[str, str, str]:
    """``"ctx:@1.%2"`` -> ("ctx", "@1", "%2"); anything that isn't that shape gives ("ctx", "", "")."""
    sess, _, rest = (tmux_at or "").rpartition(":")
    window, dot, pane = rest.partition(".")
    return (sess or REMOTE_SESSION, window, pane) if window else (REMOTE_SESSION, "", "")


def _remote_dir(cwd: str | None) -> str:
    """A directory as remote shell text: quoted, except a leading ``~``, which the remote shell expands to that
    account's own home — ``~`` on the other host is not this one's, and a resumed row's ``cwd`` names that
    machine's filesystem, so it is never expanded or rewritten here."""
    p = (cwd or "~").strip() or "~"
    if p == "~":
        return "~"
    return "~/" + shlex.quote(p[2:]) if p.startswith("~/") else shlex.quote(p)


def remote_script(name: str, cwd: str, agent: list[str], key: str | None) -> str:
    """One POSIX sh script that runs on the other host: make the agent's window in its ``ctx`` tmux (the session
    itself when it isn't up yet), tag its pane with @ctx_key when a key is known, then attach a view of it that ends
    when you detach. A login shell for the agent: agents are often only on a login PATH."""
    d, s = _remote_dir(cwd), shlex.quote(REMOTE_SESSION)
    if agent and os.path.basename(agent[0]).startswith("ctx"):
        # this host's ctx path means nothing over there: its own ctx on the login PATH, else the venv a collector
        # install leaves (~/repos/ctxswitch/.venv/bin/ctx)
        run = ('c=$(command -v ctx || echo "$HOME/repos/ctxswitch/.venv/bin/ctx"); "$c" '
               + shlex.join(agent[1:]))
    else:
        run = shlex.join(agent)
    # an agent that fails keeps its window, with why, instead of closing it (and the ctx session, when it was the
    # only window) out from under the view; one that exits cleanly closes it
    run += '; rc=$?; [ "$rc" -eq 0 ] || { echo; echo "[ctx] it exited with $rc: Enter closes this"; read -r _; }'
    cmd = shlex.quote(f"bash -lc {shlex.quote(run)}")
    mk = f"tmux new-session -d -s {s} -n {shlex.quote(name)} -c \"$d\" -P -F '#{{window_id}}' {cmd}"
    win = f"tmux new-window -d -t {s}: -n {shlex.quote(name)} -c \"$d\" -P -F '#{{window_id}}' {cmd}"
    tagline = f' tmux set-option -p -t "$w" @ctx_key {shlex.quote(key)};' if key else ""   # the window's one pane
    return (f'd={d}; cd "$d" 2>/dev/null || {{ d="$HOME"; cd "$HOME" 2>/dev/null || d=.; }}; '
            f'if tmux has-session -t {s} 2>/dev/null; then w=$({win}); else w=$({mk}); fi;{tagline} exec {view_command("$w")}')


def _arg(value: str) -> str:
    """A tmux id as remote shell text: quoted, unless it's a variable the script itself set ($w)."""
    return value if re.fullmatch(r"\$\w+", value) else shlex.quote(value)


def view_command(target_window: str, pane: str | None = None) -> str:
    """The remote shell text that shows one window of a running tmux session without taking over your keys: a
    grouped session that dies when you detach, with no status line and no prefix of its own — the tmux you drive is
    the local one. ``target_window`` is a window id (``@1``), a ``S:@w`` tmux_at, or a variable holding one."""
    sess, window, at_pane = _tmux_at_parts(target_window or "")
    pane = pane or at_pane
    # a pane id names its window wherever the pane is now: ctx swaps panes between windows, so the window a session
    # was recorded in at start goes stale while its pane id doesn't
    sel = f" \\; select-window -t {_arg(pane or window)}" if (pane or window) else ""
    sel += f" \\; select-pane -t {_arg(pane)}" if pane else ""
    return (f"tmux new-session -t {shlex.quote(sess)} -s ctxview-{uuid.uuid4().hex[:6]}"
            f" \\; set-option destroy-unattached on \\; set-option status off \\; set-option prefix None{sel}")


def remote_argv(host_target: str, script: str) -> list[str]:
    """Run ``script`` on the other host. sshd hands the string to the user's shell, so it goes as one argument of
    ``sh -c`` — quoted once here, which is the only level this side controls."""
    return ["ssh", "-t", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=30", host_target,
            "sh", "-c", shlex.quote(script)]


def new_remote_agent(host: str, harness: str, cwd: str, ssh: dict[str, str]) -> str:
    """Start a fresh agent on ``host``: a local window running the ssh that makes its window in that host's own
    ``ctx`` tmux and shows it. Its profiles are its own, so the harness is all we can pick from here."""
    if not in_tmux():
        return "not inside tmux"
    target = ssh_target(host, ssh or {})
    if not target:
        return f"no ssh route to {host} ([ssh] in the config)"
    if harness == "claude":
        sid = str(uuid.uuid4())                         # the id it starts under is the key the board tags it with
        agent, key = ["claude", "--session-id", sid], f"{host}/claude/{sid}"
    elif harness == "pi":
        agent, key = ["pi"], None
    elif harness == "shell":
        sid = str(uuid.uuid4())
        agent, key = [_ctx_bin(), "shell", "--id", sid], f"{host}/shell/{sid}"
    else:
        return f"unknown harness {harness}"
    ps = panes()
    viewer = viewer_pane(ps)
    if viewer is None:
        return "no viewer pane: split the window so the TUI has a neighbour (ctx tmux)"
    cwd = (cwd or "~").strip() or "~"
    target_session = next((p.session for p in ps if p.id == os.environ.get("TMUX_PANE")), SESSION)
    name = f"{host}/{harness}"[:24].replace(":", "-")
    argv = remote_argv(target, remote_script(name, cwd, agent, key))
    pane_id = _agent_window(target_session, os.path.expanduser("~"), name, argv)
    if key:
        tag(pane_id, key)
    _run("set-option", "-p", "-t", pane_id, "@ctx_remote", f"{host}|pending", check=False)
    _run("swap-pane", "-s", pane_id, "-t", viewer.id)
    return f"new {harness} on {host} in {cwd}"


def end_remote(s: dict, ssh: dict[str, str] | None = None) -> str:
    """k on a session running in a tmux on its own host: kill that pane (the agent dies with it, its transcript
    stays) and close the local window that was showing it."""
    target = ssh_target(s.get("host_id"), ssh or {})
    at = s.get("tmux_at") or ""
    if ":" not in at:
        return f"it isn't in a tmux on {s.get('host_id')}: end it there"
    pane = _tmux_at_parts(at)[2]
    if not pane:
        return f"it isn't in a tmux on {s.get('host_id')}: end it there"
    r = subprocess.run([*remote_argv(target or s.get("host_id") or "", f"tmux kill-pane -t {shlex.quote(pane)}")],
                       capture_output=True, text=True, timeout=10) if target else None
    said = f"ended on {s.get('host_id')}" if r is not None and r.returncode == 0 else \
        f"couldn't reach {s.get('host_id')}: {(r.stderr if r is not None else '').strip()[:60] or 'no ssh route'}"
    p = next((x for x in panes() if x.key and x.key == s.get("key") and x.session == (my_session() or SESSION)), None)
    if p is not None:
        end_agent(p)
    return said


def window_name(s: dict) -> str:
    """A session's tmux window name: its title, prefixed by its host when that isn't this one."""
    host = f"{s.get('host_id')}/" if _is_remote(s, _this_host()) else ""
    return (host + (s.get("title") or s.get("session_id") or "ctx"))[:24].replace(":", "-")


def new_session_window(s: dict, ssh: dict[str, str] | None = None, extra: list[str] | None = None) -> Pane:
    """Resume the session in a new (detached) window of the viewer's tmux session; returns its pane. A session
    from another host is resumed there over ssh (``ssh`` = the config's [ssh] table). ``extra``: more arguments
    for a local resume (``--model`` after a move between profiles)."""
    remote = _is_remote(s, _this_host())
    target = ssh_target(s.get("host_id"), ssh or {}) if remote else None
    name = window_name(s)
    if remote:
        # it starts in a tmux on its own host, and this window is the ssh that shows it
        agent = resume_argv(s, [])
        argv = (remote_argv(target, remote_script(name, s.get("cwd") or "~", agent, s.get("key")))
                if target and agent else None)
    else:
        argv = resume_argv(s, None, extra)
    if not argv:
        raise RuntimeError(f"cannot resume a {s.get('harness')} session" + (f" from {s.get('host_id')}" if remote else ""))
    cwd = os.path.expanduser("~") if remote else s.get("cwd") or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    role = "shell" if s.get("harness") == "shell" else "agent"    # a resumed shell stays a shell session, not an agent
    target_session = next((p.session for p in panes() if p.id == os.environ.get("TMUX_PANE")), SESSION)
    pane_id = _agent_window(target_session, cwd, name, argv, role=role)
    if remote:
        _run("set-option", "-p", "-t", pane_id, "@ctx_remote", f"{s.get('host_id')}|pending", check=False)
    tag(pane_id, s["key"])
    ps = panes()
    return next(p for p in ps if p.id == pane_id)


def resume_many(sessions: list[dict], ssh: dict[str, str] | None = None) -> str:
    """Resume each session in its own new window (the Start screen's Enter on several marked rows). A session that
    can't be resumed is named in the message and the rest go on. One ``show`` at the end, for the last window that
    came up: it lands in the viewer pane and names its window — per session it would rename them all."""
    if not in_tmux():
        return "not inside tmux"
    if not sessions:
        return "nothing to resume"
    done: list[dict] = []
    fails: list[str] = []
    for s in sessions:
        try:
            new_session_window(s, ssh)
            done.append(s)
        except Exception as e:                          # noqa: BLE001 - one session's failure can't stop the rest
            fails.append(f"{(s.get('title') or s.get('session_id') or s.get('key') or '?')[:20]}: {e}")
    if done:                                            # the last window it made goes into the viewer pane
        try:
            show(done[-1], ssh=ssh)
        except Exception as e:                          # noqa: BLE001 - the windows are up; only the viewer missed
            fails.append(f"viewer: {e}")
    said = "resumed " + str(len(done)) + (": " + ", ".join(_short_title(s) for s in done) if done else "")
    return said + "".join(f" · failed: {f}" for f in fails)


def _short_title(s: dict) -> str:
    """A session's title as a summary names it: cut to 20 columns, its session id or key when it has no title."""
    return (s.get("title") or s.get("session_id") or s.get("key") or "?")[:20]


def new_agent(harness: str, cwd: str, extra: list[str] | None = None, focus: bool = True,
              profile: str | None = None, profiles: list | None = None) -> str:
    """Start a fresh claude/pi session in a new window of this tmux session and bring it into the viewer pane.
    ``profile`` names a [[claude_profiles]] entry (default: the first); the window is named after it."""
    from ctxswitch.common.config import profile_named
    if not in_tmux():
        return "not inside tmux"
    label = harness
    if harness == "claude":
        profiles = _profiles() if profiles is None else profiles
        prof = profile_named(profile, profiles)
        if prof is None:
            return f"no claude profile {profile!r} (have: {', '.join(p.name for p in profiles)})"
        argv = [*prof.argv(), *(["--model", prof.model] if prof.model else [])]
        if len(profiles) > 1:
            label = f"claude[{prof.name}]"
    elif harness in ("pi", "agy"):
        argv = [harness]
    elif harness == "shell":
        sid = str(uuid.uuid4())                             # its session id: the collector keys it <host>/shell/<sid>
        argv = [_ctx_bin(), "shell", "--id", sid]           # the recording bash (see ctx shell / the collector)
    else:
        return f"unknown harness {harness}"
    if not shutil.which(argv[0]) and not os.path.isabs(argv[0]):
        return f"{argv[0]} is not installed on this host"
    argv = [*argv, *(extra or [])]
    cwd = os.path.expanduser(cwd.strip() or "~")
    if not os.path.isdir(cwd):
        return f"no such directory: {cwd}"
    ps = panes()
    viewer = viewer_pane(ps)
    if viewer is None:
        return "no viewer pane: split the window so the TUI has a neighbour (ctx tmux)"
    target_session = next((p.session for p in ps if p.id == os.environ.get("TMUX_PANE")), SESSION)
    name = f"{label}:{os.path.basename(cwd.rstrip('/')) or cwd}"[:24]
    if harness == "shell":
        pane_id = _agent_window(target_session, cwd, name, argv, role="shell")
        tag(pane_id, f"{_this_host()}/shell/{sid}")        # Enter and Alt+n find it before the collector reports it
    else:
        # no session key yet: the collector will see the transcript shortly and the pid walk maps it to this pane
        pane_id = _agent_window(target_session, cwd, name, argv)
    _run("swap-pane", "-s", pane_id, "-t", viewer.id)
    if focus:
        _run("select-pane", "-t", pane_id, check=False)
    return f"new {label} in {cwd}"


def show(s: dict, focus: bool = True, ssh: dict[str, str] | None = None, extra: list[str] | None = None) -> str:
    """Bring session ``s`` into the viewer pane. Returns a short status message for the TUI. ``extra``: arguments
    for the resume command if one is needed (ignored when the session is already in a pane)."""
    if not in_tmux():
        return "not inside tmux"
    ps = panes()
    viewer = viewer_pane(ps)
    if viewer is None:
        return "no viewer pane: split the window so the TUI has a neighbour (ctx tmux)"
    if s.get("harness") == "claude-win":
        from ctxswitch.client.launch import windows_hint
        return windows_hint(s)
    ssh = _ssh() if ssh is None else ssh
    r, pane = _reach(s, ps, _this_host(), ssh)
    created = False
    if r == "remote":
        return reach_help(s, r, ssh)
    if r == "foreign":
        return f"running in another terminal (pid {s.get('pid')}): quit it there, then Enter resumes it here"
    if r == "none":
        return f"cannot resume a {s.get('harness')} session"
    if r == "attach":
        pane = _remote_view(s, ssh or _ssh(), viewer)
        created = True
    elif pane is None:
        pane = new_session_window(s, ssh, extra)
        created = True
    if focus:                                           # select-pane alone doesn't change the window you're looking at
        _run("select-window", "-t", viewer.window, check=False)
    if pane.id == viewer.id:
        if focus:
            _run("select-pane", "-t", viewer.id, check=False)
        return "already showing"
    tag(pane.id, s["key"])
    # name the session's home window after the session; the viewer window keeps its own name
    _run("rename-window", "-t", pane.window, window_name(s), check=False)
    _run("swap-pane", "-s", pane.id, "-t", viewer.id)
    if focus:
        _run("select-pane", "-t", pane.id, check=False)      # after the swap the session pane sits where the viewer was
    return ("attached " if r == "attach" else "resumed " if created else "showing ") + (s.get("title") or s.get("session_id", ""))[:40]


def _remote_view(s: dict, ssh: dict[str, str], viewer: Pane) -> Pane:
    """A local window running the ssh that shows a session already running in a tmux on its own host: a grouped
    "view" of that one window, which dies when you detach and leaves the agent running there."""
    sess, window, pane = _tmux_at_parts(s.get("tmux_at"))
    target = ssh_target(s.get("host_id"), ssh) or s.get("host_id")
    ps = panes()
    here = next((p.session for p in ps if p.id == os.environ.get("TMUX_PANE")), SESSION)
    pane_id = _agent_window(here, os.path.expanduser("~"), window_name(s),
                            remote_argv(target or "", view_command(f"{sess}:{window}", pane)))
    if s.get("key"):
        tag(pane_id, s["key"])
    _run("set-option", "-p", "-t", pane_id, "@ctx_remote", f"{s.get('host_id')}|{s.get('tmux_at')}", check=False)
    return next(p for p in panes() if p.id == pane_id)


# How Enter can reach a session, as shown in the TUI's reach column:
#   pane     in a pane of this tmux server: Enter swaps it into the viewer
#   resume   not running here: Enter resumes it in a new window
#   foreign  running on this host in a terminal outside tmux: can't be adopted (quit it there, then resume)
#   ssh      ended on another host: Enter resumes it there over ssh, in a new window here
#   attach   running in a tmux on another host: Enter shows it here over ssh (an agent survives the ssh dropping)
#   remote   on another host and Enter can't help: running there, or no ssh route from here
#   none     not running and no way to resume it
REACH_GLYPH = {"pane": "◆", "resume": "↻", "ssh": "↗", "attach": "⇄", "foreign": "⊘", "remote": "⇢", "none": " "}
REACH_HELP = {"pane": "in ctx's tmux: Enter brings it to the left pane",
              "resume": "not running: Enter resumes it in a new window",
              "ssh": "on another host: Enter resumes it there over ssh",
              "attach": "in a tmux on its host: Enter attaches to it over ssh",
              "foreign": "running in another terminal: can't be adopted; quit it there, then Enter resumes it here",
              "remote": "on another host: open it there",
              "none": "can't be resumed"}


def reach_help(s: dict, reach: str, ssh: dict[str, str] | None = None) -> str:
    """The detail pane's line on what Enter does, with the host spelled out for other-host sessions."""
    host = s.get("host_id")
    if reach == "ssh":
        return f"on {host}: Enter resumes it there over ssh ({ssh_target(host, ssh or {})})"
    if reach == "attach":
        return f"in a tmux on {host}: Enter attaches to it over ssh ({ssh_target(host, ssh or {})})"
    if reach != "remote":
        return REACH_HELP.get(reach, "")
    if s.get("live_status") in LIVE and s.get("pid"):
        return f"running on {host}: quit it there, then Enter resumes it here over ssh"
    if not resume_argv(s):
        return f"on {host}: can't be resumed"
    return f"on {host}, no ssh route from here ([ssh] {host} is \"\"): run `ctx show {(s.get('session_id') or '')[:8]}` there"


def _is_remote(s: dict, this_host: str) -> bool:
    host = s.get("host_id")
    return bool(host) and host != this_host and host != os.uname().nodename


def _ssh() -> dict[str, str]:
    try:
        from ctxswitch.common.config import load
        return load().ssh
    except Exception:                                      # noqa: BLE001
        return {}


def _reach(s: dict, ps: list[Pane], this_host: str, ssh: dict[str, str] | None = None) -> tuple[str, Pane | None]:
    if _is_remote(s, this_host):
        # only ctx's tag finds it: a pid from another host means nothing here
        pane = next((p for p in ps if p.key and p.key == s.get("key")), None)
        if pane is not None:
            return "pane", pane
        # live in a tmux on its own host, and we can ssh there: Enter shows that pane (see _remote_view)
        if s.get("live_status") in LIVE and s.get("tmux_at") and ssh_target(s.get("host_id"), ssh or {}):
            return "attach", None
        if s.get("live_status") in LIVE and s.get("pid"):
            return "remote", None
        return ("ssh" if resume_argv(s) and ssh_target(s.get("host_id"), ssh or {}) else "remote"), None
    pane = pane_for_session(s, ps) if ps else None
    if pane is not None:
        return "pane", pane
    if s.get("live_status") in LIVE and s.get("pid"):
        return "foreign", None
    return ("resume" if resume_argv(s) else "none"), None


def reach_all(sessions: list[dict], this_host: str | None = None, ssh: dict[str, str] | None = None) -> dict[str, str]:
    """key -> reach for every session, with one tmux query."""
    ps = panes() if in_tmux() else []
    this_host = this_host or _this_host()
    ssh = _ssh() if ssh is None else ssh
    return {s["key"]: _reach(s, ps, this_host, ssh)[0] for s in sessions}


def my_session(ps: list[Pane] | None = None) -> str | None:
    ps = ps if ps is not None else panes()
    return next((p.session for p in ps if p.id == os.environ.get("TMUX_PANE")), None)


def detach() -> None:
    """Detach this terminal; the TUI and every agent keep running (``ctx tmux`` re-attaches)."""
    _run("detach-client")


def managed(p: Pane) -> bool:
    """A pane ctx manages: one of its agent windows, one tagged with a session key, or one of its plain shell
    sessions (role "shell", whose foreground is bash at a prompt, so the agent rule can't cover it)."""
    return p.role != "tui" and (p.role in ("agent", "shell") or bool(p.key))


def agent_panes(ps: list[Pane] | None = None) -> list[Pane]:
    """Panes in this tmux session with an agent still running in them, or a shell session at its prompt."""
    ps = ps if ps is not None else panes()
    me = my_session(ps)
    return [p for p in ps if p.session == me and managed(p) and (p.role == "shell" or p.cmd not in SHELLS)]


def agent_pane(key: str, ps: list[Pane] | None = None) -> Pane | None:
    """The pane in this tmux session with ``key``'s agent running in it, if any."""
    return next((p for p in agent_panes(ps) if p.key == key), None)


def _children(pid: int, proc: str = "/proc") -> list[int]:
    out: list[int] = []
    try:
        for tid in os.listdir(f"{proc}/{pid}/task"):
            with open(f"{proc}/{pid}/task/{tid}/children") as f:
                out += [int(x) for x in f.read().split()]
    except (OSError, ValueError):
        pass
    return out


def end_agent(p: Pane, proc: str = "/proc", kill=os.kill) -> int:
    """End the agent in pane ``p`` (k): SIGTERM to the processes its wrapper started (the agent, or the ssh to it),
    so the wrapper's `ctx tmux-exit` hands the viewer on and the window closes, as when you quit it yourself. The pane
    is marked first so that exit doesn't stop on "press Enter". A shell session gets SIGHUP instead: interactive bash
    ignores TERM. Returns how many processes were signalled."""
    _run("set-option", "-p", "-t", p.id, "@ctx_ending", "1", check=False)
    sig = signal.SIGHUP if p.role == "shell" else signal.SIGTERM
    pids = _children(p.pid, proc)
    for pid in pids:
        try:
            kill(pid, sig)
        except OSError:
            pass
    return len(pids)


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
    gone = {s["key"] for s in sessions or [] if s.get("live_status") == "ended"}
    if v.key and (not known or v.key in known) and v.key not in gone:
        return v.key
    here = _this_host()
    for s in sessions or []:                            # untagged, stale or ended tag: map by the agent's pid, or by
        if _is_remote(s, here) or s.get("live_status") == "ended":      # the window showing a parked background job
            continue                                    # its pid is another machine's
        p = pane_for_pid(s.get("pid"), ps) or pane_for_pid(s.get("via_pid"), ps)
        if p and p.id == v.id:
            tag(v.id, s["key"])
            return s["key"]
    return None


def on_screen_keys(ps: list[Pane] | None = None, sessions: list[dict] | None = None) -> set[str]:
    """Keys of the sessions in a pane someone can see: the current window of an attached tmux session. The viewer
    only counts while its window is the one shown; a session's own window counts when that's the one shown."""
    if not in_tmux():
        return set()
    ps = ps if ps is not None else panes()
    here, out = _this_host(), set()
    known = {s["key"] for s in sessions or []}
    for p in ps:
        if not p.on_screen:
            continue
        if p.key and (not known or p.key in known):
            out.add(p.key)
            continue
        for s in sessions or []:                        # untagged: map by the agent's pid, as visible_key does
            if not _is_remote(s, here) and (q := pane_for_pid(s.get("pid"), ps)) is not None and q.id == p.id:
                out.add(s["key"])
    return out


def bind_slot_keys(board: str | None, n: int = 9) -> None:
    """Alt+1…n in the ctx session go to the board (which opens its row N), from whichever pane has focus: a root-table
    binding, so it's server-wide, but outside the ctx session the key goes through as typed. Bound by the board at
    start with its own pane id, so a restarted board takes them over."""
    if not board:
        return
    for k in range(1, n + 1):
        _run("bind-key", "-n", f"M-{k}", "if-shell", "-F", f"#{{==:#{{session_name}},{SESSION}}}",
             f"send-keys -t {board} M-{k}", f"send-keys M-{k}", check=False)


def pin_board_width(board: str | None) -> None:
    """Keep the board ``@ctx_width`` wide (the width `ctx tmux` split it at) when a client attaches or resizes.

    tmux created the session detached (80x24) and split it at ``-l width``; when a wide terminal then attaches it
    rescales both panes proportionally, so a 270-column client leaves the board ~155 wide. The hooks re-assert the
    width on every attach/resize. Called by the board at start with its own pane id, so a restarted board re-pins
    with its new id."""
    if not board:
        return
    width = _run("show-options", "-v", "-t", SESSION, "@ctx_width", check=False).strip() or DEFAULT_WIDTH
    _run("resize-pane", "-t", board, "-x", width, check=False)
    for hook in ("client-attached", "client-resized"):
        _run("set-hook", "-t", SESSION, hook, f"resize-pane -t {board} -x {width}", check=False)


def saved_width() -> str:
    """The board width to start at: the last one set with `<` `>` or `ctx tmux --width`, else 60 columns. A stray
    or half-written file must never stop `ctx tmux`, so anything that isn't a column count or a percentage is ignored."""
    try:
        width = WIDTH_FILE.read_text().strip()
    except OSError:
        return DEFAULT_WIDTH
    return width if re.fullmatch(r"\d+%?", width) else DEFAULT_WIDTH


def save_width(width: str) -> None:
    """Remember a width for the next `ctx tmux`. Best effort: a read-only or full home changes nothing else."""
    try:
        WIDTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        WIDTH_FILE.write_text(width + "\n")
    except OSError:
        pass


def step_width(board: str | None, delta: int) -> str | None:
    """`<` / `>` on the board: WIDTH_STEP columns narrower or wider, remembered, and re-pinned right away.

    The board never gets narrower than MIN_WIDTH, and never so wide that the viewer on its left loses its 20
    columns. Returns the new width, so the board can say it."""
    if not board:
        return None
    current = int(_run("display-message", "-p", "-t", board, "#{pane_width}").strip())
    window = int(_run("display-message", "-p", "-t", board, "#{window_width}").strip())
    new = min(max(MIN_WIDTH, current + delta), max(MIN_WIDTH, window - 20))
    _run("set-option", "-t", SESSION, "@ctx_width", str(new), check=False)
    save_width(str(new))
    pin_board_width(board)                                        # resizes now and re-hooks the new width
    return str(new)


def ensure_board(width: str, ctx_cmd: str, restart: bool = False) -> str:
    """The ctx session's board pane: put back on the right of the viewer window if it's gone (closed, crashed: the
    agents stay, but nothing manages them), restarted in place with ``restart`` (new code). '' if nothing changed."""
    boards = [p for p in panes() if p.session == SESSION and p.role == "tui"]
    if boards:
        if not restart:
            return ""
        _run("respawn-pane", "-k", "-t", boards[0].id, ctx_cmd)
        return "board restarted"
    target = f"{SESSION}:{VIEWER_WINDOW}"
    if _run("list-windows", "-t", SESSION, "-F", "#{window_name}", check=False).split().count(VIEWER_WINDOW) == 0:
        _run("new-window", "-d", "-t", SESSION, "-n", VIEWER_WINDOW, "-c", os.path.expanduser("~"))
    pid = _run("split-window", "-d", "-f", "-h", "-l", width, "-t", target, "-P", "-F", "#{pane_id}", "-c", os.getcwd(), ctx_cmd).strip()
    set_role(pid, "tui")
    return "board put back on the right of the viewer window"


def bootstrap(width: str | None = None, restart: bool = False) -> str:
    """`ctx tmux`: create (or join) the manager layout — viewer left, TUI right — and attach. Joining puts the board
    back if it's gone; ``restart`` restarts it on the installed code. Inside the ctx session it never splits the
    window you're in: that board wouldn't manage anything.

    ``width`` is a tmux size: a column count ("56") or a percentage ("18%"). Left out, it's the width you last set
    (with `ctx tmux --width` or `<` `>` on the board); giving it explicitly remembers it for the next time.
    """
    if width is None:
        width = saved_width()
    else:
        save_width(width)
    ctx_cmd = shlex.join([os.path.abspath(os.sys.argv[0]), "tui"]) if os.sys.argv else "ctx tui"
    if in_tmux() and _run("display-message", "-p", "#{session_name}", check=False).strip() == SESSION:
        _run("set-option", "-t", SESSION, "@ctx_width", width, check=False)   # what pin_board_width holds the board at
        said = ensure_board(width, ctx_cmd, restart)
        restore_viewer(next((p.id for p in panes() if p.session == SESSION and p.role == "tui"), None))
        _run("select-window", "-t", f"{SESSION}:{VIEWER_WINDOW}", check=False)
        return said or "the board is already running (window mgr); --restart restarts it"
    if in_tmux():
        _run("split-window", "-h", "-l", width, "-c", os.getcwd(), ctx_cmd)
        _run("select-pane", "-t", "{left}", check=False)
        return "split: TUI on the right"
    has = subprocess.run([TMUX, "has-session", "-t", SESSION], capture_output=True).returncode == 0
    if has:
        _run("set-option", "-t", SESSION, "@ctx_width", width, check=False)
        ensure_board(width, ctx_cmd, restart)
        restore_viewer(next((p.id for p in panes() if p.session == SESSION and p.role == "tui"), None))
    else:
        _run("new-session", "-d", "-s", SESSION, "-n", VIEWER_WINDOW, "-c", os.path.expanduser("~"))
        _run("set-option", "-t", SESSION, "@ctx_width", width, check=False)
        _run("split-window", "-h", "-l", width, "-t", f"{SESSION}:{VIEWER_WINDOW}", "-c", os.getcwd(), ctx_cmd)
        _run("set-option", "-t", SESSION, "allow-rename", "off", check=False)
        # a new session's left pane is an empty shell; you start at the board (its Start screen, when nothing's live)
        _run("select-pane", "-t", f"{SESSION}:{VIEWER_WINDOW}.1", check=False)
    os.execvp(TMUX, [TMUX, "attach-session", "-t", SESSION])
    return ""
