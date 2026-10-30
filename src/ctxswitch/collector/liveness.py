"""Which sessions are alive right now, and since when.

Claude Code writes ~/.claude/sessions/<pid>.json for every interactive/-p session (status busy|idle,
statusUpdatedAt, messagingSocketPath, procStart). Files outlive crashed sessions, so an entry counts as live only
when the pid exists AND its /proc start time matches procStart (a reused pid fails the second check).
Windows-native Claude (read from WSL) writes the same registry; its pids are Windows pids, checked with
tasklist.exe (no start time available, so a reused pid would read as live until the next check).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ctxswitch.common.schema import LIVE, Liveness, session_key


def _iso_ms(ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return None


def proc_start(pid: int, proc: str = "/proc") -> str | None:
    """Field 22 of /proc/<pid>/stat (clock ticks since boot), as the registry's ``procStart`` string."""
    try:
        with open(f"{proc}/{pid}/stat") as f:
            stat = f.read()
    except OSError:
        return None
    # comm may contain spaces/parens: split after the last ')'
    rest = stat.rsplit(")", 1)[1].split()
    return rest[19] if len(rest) > 19 else None


def read_claude_registry(sessions_dir: Path, host_id: str, proc: str = "/proc") -> dict[str, Liveness]:
    """Live Claude sessions keyed by session_key. Stale registry files are ignored (not deleted)."""
    return _claude_registry(sessions_dir, host_id, "claude", lambda pid, d: proc_start(pid, proc) == str(d.get("procStart")))


def read_claude_win_registry(sessions_dir: Path, host_id: str, pids=None) -> dict[str, Liveness] | None:
    """Live Windows-native Claude sessions; None when liveness can't be known (tasklist.exe not reachable).
    ``pids`` is the Windows pid set, or a callable producing it (only called when there are registry files)."""
    files = list(sessions_dir.glob("*.json")) if sessions_dir.is_dir() else []
    if not files:
        return {}
    alive = pids() if callable(pids) else pids
    if alive is None:
        return None
    return _claude_registry(sessions_dir, host_id, "claude-win", lambda pid, d: pid in alive)


def _user_name(d: dict) -> str | None:
    """The registry's session name when the user chose it (/rename, --name). A resumed session can carry a name
    the transcript has no custom-title for; "derived"/"auto" names are generated and the ai-title covers them."""
    n = d.get("name")
    if not isinstance(n, str) or not n.strip() or d.get("nameSource") in ("derived", "auto"):
        return None
    return n.strip()


def _claude_registry(sessions_dir: Path, host_id: str, harness: str, alive) -> dict[str, Liveness]:
    out: dict[str, Liveness] = {}
    if not sessions_dir.is_dir():
        return out
    for p in sessions_dir.glob("*.json"):
        try:
            with open(p) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        pid, sid = d.get("pid"), d.get("sessionId")
        if not isinstance(pid, int) or not isinstance(sid, str):
            continue
        if not alive(pid, d):
            continue
        status = d.get("status") if d.get("status") in LIVE else "unknown"
        out[session_key(host_id, harness, sid)] = Liveness(
            key=session_key(host_id, harness, sid), status=status,
            status_since=_iso_ms(d.get("statusUpdatedAt") or d.get("updatedAt")), pid=pid,
            socket=d.get("messagingSocketPath") if isinstance(d.get("messagingSocketPath"), str) else None,
            name=_user_name(d))
    return out


def read_agy_presence(conversations_dir: Path, host_id: str, proc: str = "/proc") -> dict[str, Liveness]:
    """Antigravity holds an flock on ../presence/<conversation id>.lock while a conversation is open. Locked = live.
    busy/idle comes from conversation_summaries.db (status BUSY/RUNNING, not_fully_idle); the pid is the agy process
    holding the lock (found through /proc, so the board can map it to its pane)."""
    import fcntl
    from ctxswitch.collector.adapters.agy import read_summary
    out: dict[str, Liveness] = {}
    presence = conversations_dir.parent / "presence"
    if not presence.is_dir():
        return out
    holders: dict[str, int] | None = None
    for lock in presence.glob("*.lock"):
        try:
            fd = os.open(lock, os.O_RDWR)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                continue                                  # nobody holds it: not running
            except OSError:
                pass                                      # held: a live conversation
        finally:
            os.close(fd)
        sid = lock.stem
        if holders is None:
            holders = _agy_lock_holders(proc)
        summ = read_summary(str(conversations_dir.parent / "conversation_summaries.db"), sid)
        st = summ.get("status", "")
        status = "busy" if summ.get("busy") or st.endswith(("BUSY", "RUNNING")) else "idle" if st.endswith("IDLE") else "unknown"
        k = session_key(host_id, "agy", sid)
        out[k] = Liveness(key=k, status=status, status_since=_iso_sql(summ.get("last_modified")), pid=holders.get(sid))
    return out


def _agy_lock_holders(proc: str = "/proc") -> dict[str, int]:
    """{conversation id: pid} for every agy process with a presence lock open."""
    out: dict[str, int] = {}
    try:
        pids = [int(p) for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return out
    for pid in pids:
        try:
            with open(f"{proc}/{pid}/comm") as f:
                if f.read().strip() != "agy":
                    continue
            for fd in os.listdir(f"{proc}/{pid}/fd"):
                target = os.readlink(f"{proc}/{pid}/fd/{fd}")
                if "/presence/" in target and target.endswith(".lock"):
                    out.setdefault(target.rsplit("/", 1)[1][:-5], pid)
        except OSError:
            continue
    return out


def _iso_sql(v: Any) -> str | None:
    """'2026-10-30 18:29:28.131084492+00:00' (sqlite datetime) -> ISO with Z."""
    if not isinstance(v, str) or len(v) < 19:
        return None
    return v[:19].replace(" ", "T") + "Z"


def runtime_dir() -> Path:
    """Where the pi extension registers (same order as pi_ext/ctxswitch.ts)."""
    if os.environ.get("CTXSWITCH_RUNTIME"):
        return Path(os.environ["CTXSWITCH_RUNTIME"])
    if os.environ.get("XDG_RUNTIME_DIR"):
        return Path(os.environ["XDG_RUNTIME_DIR"])
    run = Path(f"/run/user/{os.getuid()}")
    return run if run.exists() else Path.home() / ".local" / "state" / "ctxswitch" / "run"


def read_pi_registry(runtime_dir: Path, host_id: str, proc: str = "/proc") -> dict[str, Liveness]:
    """pi sessions register through the ctxswitch extension as <runtime>/ctxswitch/pi-<pid>.json. A crashed pi
    leaves its file behind: live only if the pid exists and, when the extension recorded it, its start matches."""
    out: dict[str, Liveness] = {}
    d = runtime_dir / "ctxswitch"
    if not d.is_dir():
        return out
    for p in d.glob("pi-*.json"):
        try:
            with open(p) as f:
                e = json.load(f)
        except (OSError, ValueError):
            continue
        pid, sid = e.get("pid"), e.get("sessionId")
        if not isinstance(pid, int) or not isinstance(sid, str) or not os.path.exists(f"{proc}/{pid}"):
            continue
        if e.get("procStart") is not None and proc_start(pid, proc) != str(e["procStart"]):
            continue
        k = session_key(host_id, "pi", sid)
        out[k] = Liveness(key=k, status=e.get("status") if e.get("status") in ("busy", "idle") else "unknown", status_since=e.get("statusSince"), pid=pid,
                          socket=e.get("socket"))
    return out


def read_shell_registry(runtime_dir: Path, host_id: str, proc: str = "/proc") -> dict[str, Liveness]:
    """A ``ctx shell`` registers as <runtime>/ctxswitch/shell-<pid>.json while it runs (busy while a command is
    running, idle at its prompt). An exited shell deletes its file; one that died leaves it behind, so it counts as
    live only when the pid exists and, when recorded, its start time still matches."""
    out: dict[str, Liveness] = {}
    d = runtime_dir / "ctxswitch"
    if not d.is_dir():
        return out
    for p in d.glob("shell-*.json"):
        try:
            with open(p) as f:
                e = json.load(f)
        except (OSError, ValueError):
            continue
        pid, sid = e.get("pid"), e.get("sessionId")
        if not isinstance(pid, int) or not isinstance(sid, str) or not os.path.exists(f"{proc}/{pid}"):
            continue
        if e.get("procStart") is not None and proc_start(pid, proc) != str(e["procStart"]):
            continue
        k = session_key(host_id, "shell", sid)
        out[k] = Liveness(key=k, status=e.get("status") if e.get("status") in ("busy", "idle") else "unknown",
                          status_since=e.get("statusSince"), pid=pid)
    return out
