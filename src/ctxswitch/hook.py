"""ctx-hook: Claude Code hook → one spool file per event. Stdlib only; it runs on every hooked event.

  ctx-hook claude          reads the hook's JSON on stdin (installed by `ctx install` for the events in EVENTS)
  ctx-hook brief claude    SessionStart(resume), synchronous: prints {"systemMessage": <briefing>} from the hub
  ctx-hook brief pi <sid>  the pi extension's session_start(resume): prints the briefing as plain text

The transcript already holds prompts, replies, questions and plan approvals. Hooks add what it can't show: a
permission dialog waiting on the operator (PermissionRequest / Notification) and how it ended (PostToolUse,
PermissionDenied, Stop, ...). Each event becomes ``<spool>/<time_ns>-<pid>.json`` (written to a dot-file, then
renamed, so the collector never reads half a file). The collector pushes and deletes them.

Tool events fire on every tool call, so they are only spooled while a permission is pending for that session
(a marker file under ``<spool>/.pending/``, set by PermissionRequest and cleared when the turn ends).
This must never fail or slow the agent: every error is swallowed and the exit code is always 0. The briefing
is the one blocking call: it gives the hub BRIEF_TIMEOUT_S and prints nothing if that isn't enough.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EVENTS = ("SessionStart", "SessionEnd", "Notification", "PermissionRequest", "PermissionDenied",
          "PostToolUse", "PostToolUseFailure", "UserPromptSubmit", "Stop", "PreToolUse")
# A dialog's tool_use can reach the transcript late (2.1.312: missing for a minute while the dialog was up), so the
# question also comes from PreToolUse (installed with this matcher; anything else is dropped here too)
DIALOG_TOOLS = ("AskUserQuestion", "ExitPlanMode")
GATED = {"PostToolUse", "PostToolUseFailure"}               # spooled only while a permission is pending
OPENS = {"PermissionRequest"}
CLOSES = {"Stop", "UserPromptSubmit", "SessionEnd", "SessionStart"}       # SessionStart: a crash left it
KEEP = ("source", "reason", "notification_type", "message", "title", "tool_name", "tool_input", "permission_mode",
        "cwd", "transcript_path", "agent_type")
MAX_STR = 400
BRIEF_TIMEOUT_S = 2.0


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
    if event == "PreToolUse":
        if d.get("tool_name") not in DIALOG_TOOLS or not isinstance(d.get("tool_use_id"), str):
            return None
        qs = (d.get("tool_input") or {}).get("questions") if isinstance(d.get("tool_input"), dict) else None
        return write_event(spool, "claude", sid, event, {"tool_name": d["tool_name"], "tool_use_id": d["tool_use_id"],
                                                         "questions": [_trim(q.get("question")) for q in qs or [] if isinstance(q, dict)]})
    out = write_event(spool, "claude", sid, event, {k: _trim(d[k]) for k in KEEP if k in d})
    if event in OPENS or (event == "Notification" and d.get("notification_type") == "permission_prompt"):
        marker.parent.mkdir(parents=True, exist_ok=True); marker.touch()
    elif event in CLOSES:
        marker.unlink(missing_ok=True)
    return out


def fetch_brief(harness: str, sid: str, cfg=None) -> str | None:
    """The hub's resume briefing for this host's session, or None (no hub, no token, too slow, nothing to say)."""
    import urllib.parse
    import urllib.request
    from ctxswitch.common.config import load
    from ctxswitch.common.schema import session_key
    cfg = cfg or load()
    key = urllib.parse.quote(session_key(cfg.host_id, harness, sid), safe="/")
    req = urllib.request.Request(f"{cfg.hub.url.rstrip('/')}/v1/sessions/{key}/brief",
                                 headers={"Authorization": f"Bearer {cfg.hub.token}"} if cfg.hub.token else {})
    with urllib.request.urlopen(req, timeout=BRIEF_TIMEOUT_S) as r:
        text = json.loads(r.read(200_000)).get("text")
    return text if isinstance(text, str) and text.strip() else None


def brief_claude(d: dict) -> str | None:
    sid = d.get("session_id")
    if d.get("hook_event_name") != "SessionStart" or d.get("source") != "resume" or not isinstance(sid, str) or not sid:
        return None
    text = fetch_brief("claude", sid)
    return json.dumps({"systemMessage": text}, ensure_ascii=False) if text else None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv[:2] == ["brief", "pi"] and len(argv) > 2:
            text = fetch_brief("pi", argv[2])
            if text:
                print(text)
            return 0
        raw = sys.stdin.read(1_000_000)
        if argv[:2] == ["brief", "claude"]:
            out = brief_claude(json.loads(raw))
            if out:
                print(out)
        elif argv[:1] == ["claude"]:
            handle_claude(json.loads(raw), spool_dir())
    except Exception:                                   # noqa: BLE001 - a hook must never break the agent
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
