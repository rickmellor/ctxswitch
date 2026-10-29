"""ctx-hook: Claude Code hook → one spool file per event. Stdlib only; it runs on every hooked event.

  ctx-hook claude      reads the hook's JSON on stdin (installed by `ctx install` for the events in EVENTS)

The transcript already holds prompts, replies, questions and plan approvals. Hooks add what it can't show: a
permission dialog waiting on the operator (PermissionRequest / Notification) and how it ended (PostToolUse,
PermissionDenied, Stop, ...). Each event becomes ``<spool>/<time_ns>-<pid>.json`` (written to a dot-file, then
renamed, so the collector never reads half a file). The collector pushes and deletes them.

Tool events fire on every tool call, so they are only spooled while a permission is pending for that session
(a marker file under ``<spool>/.pending/``, set by PermissionRequest and cleared when the turn ends).
This must never fail or slow the agent: every error is swallowed and the exit code is always 0.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EVENTS = ("SessionStart", "SessionEnd", "Notification", "PermissionRequest", "PermissionDenied",
          "PostToolUse", "PostToolUseFailure", "UserPromptSubmit", "Stop")
GATED = {"PostToolUse", "PostToolUseFailure"}               # spooled only while a permission is pending
OPENS = {"PermissionRequest"}
CLOSES = {"Stop", "UserPromptSubmit", "SessionEnd", "SessionStart"}       # SessionStart: a crash left it
KEEP = ("source", "reason", "notification_type", "message", "title", "tool_name", "tool_input", "permission_mode",
        "cwd", "transcript_path", "agent_type")
MAX_STR = 400


def spool_dir() -> Path:
    return Path(os.environ.get("CTXSWITCH_SPOOL") or os.path.expanduser("~/.local/state/ctxswitch/spool"))


def _trim(v, depth=0):
    if isinstance(v, str):
        return v if len(v) <= MAX_STR else v[: MAX_STR - 1] + "…"
    if isinstance(v, dict) and depth < 2:
        return {k: _trim(x, depth + 1) for k, x in list(v.items())[:20]}
    if isinstance(v, list) and depth < 2:
        return [_trim(x, depth + 1) for x in v[:10]]
    return v if isinstance(v, (int, float, bool)) or v is None else str(v)[:MAX_STR]


def write_event(spool: Path, harness: str, sid: str, event: str, data: dict) -> Path:
    spool.mkdir(parents=True, exist_ok=True)
    ts = time.time_ns()
    name = f"{ts}-{os.getpid()}.json"
    rec = {"harness": harness, "session_id": sid, "event": event,
           "ts": datetime.fromtimestamp(ts / 1e9, tz=timezone.utc).isoformat().replace("+00:00", "Z"), "data": data}
    tmp = spool / f".{name}"
    tmp.write_text(json.dumps(rec, ensure_ascii=False))
    os.replace(tmp, spool / name)
    return spool / name


def handle_claude(d: dict, spool: Path) -> Path | None:
    event, sid = d.get("hook_event_name"), d.get("session_id")
    if event not in EVENTS or not isinstance(sid, str) or not sid or "/" in sid:
        return None
    marker = spool / ".pending" / sid
    if event in GATED and not marker.exists():
        return None
    out = write_event(spool, "claude", sid, event, {k: _trim(d[k]) for k in KEEP if k in d})
    if event in OPENS or (event == "Notification" and d.get("notification_type") == "permission_prompt"):
        marker.parent.mkdir(parents=True, exist_ok=True); marker.touch()
    elif event in CLOSES:
        marker.unlink(missing_ok=True)
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        raw = sys.stdin.read(1_000_000)
        if argv[:1] == ["claude"]:
            handle_claude(json.loads(raw), spool_dir())
    except Exception:                                   # noqa: BLE001 - a hook must never break the agent
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
