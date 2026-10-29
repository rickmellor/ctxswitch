"""Which sessions are alive right now, and since when.

Claude Code writes ~/.claude/sessions/<pid>.json for every interactive/-p session (status busy|idle,
statusUpdatedAt, messagingSocketPath, procStart). Files outlive crashed sessions, so an entry counts as live only
when the pid exists AND its /proc start time matches procStart (a reused pid fails the second check).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ctxswitch.common.schema import Liveness, session_key


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
        if proc_start(pid, proc) != str(d.get("procStart")):
            continue
        status = d.get("status") if d.get("status") in ("busy", "idle", "shell") else "unknown"
        out[session_key(host_id, "claude", sid)] = Liveness(
            key=session_key(host_id, "claude", sid), status=status,
            status_since=_iso_ms(d.get("statusUpdatedAt") or d.get("updatedAt")), pid=pid,
            socket=d.get("messagingSocketPath") if isinstance(d.get("messagingSocketPath"), str) else None)
    return out


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
