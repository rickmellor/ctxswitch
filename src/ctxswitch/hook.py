"""ctx-hook: Claude Code hook → one spool file per event. Stdlib only; it runs on every hooked event.

  ctx-hook claude             reads the hook's JSON on stdin (installed by `ctx install` for the events in EVENTS)
  ctx-hook brief claude       SessionStart(resume), synchronous: prints {"systemMessage": <briefing>} from the hub
  ctx-hook brief pi <sid>     the pi extension's session_start(resume): prints the briefing as plain text
  ctx-hook prompt-check       UserPromptSubmit, synchronous: asks the hub whether this prompt belongs to another live
                              session and blocks it when it clearly does (see CONFIRM_TTL_S below)

The transcript already holds prompts, replies, questions and plan approvals. Hooks add what it can't show: a
permission dialog waiting on the operator (PermissionRequest / Notification) and how it ended (PostToolUse,
PermissionDenied, Stop, ...). Each event becomes ``<spool>/<time_ns>-<pid>.json`` (written to a dot-file, then
renamed, so the collector never reads half a file). The collector pushes and deletes them.

Tool events fire on every tool call, so they are only spooled while a permission is pending for that session
(a marker file under ``<spool>/.pending/``, set by PermissionRequest and cleared when the turn ends).
This must never fail or slow the agent: every error is swallowed and the exit code is always 0. The briefing and the
prompt check are the two blocking calls: each gives the hub its own timeout and says nothing if that isn't enough.
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

# The misplaced-prompt check (UserPromptSubmit, synchronous): how long to wait on the hub, the whole command's
# deadline, and how long a blocked prompt may be re-sent to force it through (the operator's ↑ then Enter).
# CONFIRM_FILE maps sha256(key + "\n" + prompt) → {"ts", "id"}; the id is the hub's row, marked "resent" on the
# re-send so the board stops nagging about it.
MISPLACE_TIMEOUT_S = 1.5
PROMPT_CHECK_DEADLINE_S = 1.8
CONFIRM_TTL_S = 60.0
CONFIRM_FILE = Path(os.path.expanduser("~/.local/state/ctxswitch/prompt-confirm.json"))


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


def _misplace_skip(prompt: str) -> bool:
    """What is never checked: slash commands, ctxswitch's own deliveries (operator messages and peer messages are
    injected, not typed; the two heads are collector/deliver.py's HEADER and agent_header's first line, repeated here
    because this runs on every prompt and stays stdlib-light), and prompts too short to say anything about."""
    return (prompt.startswith("/") or prompt.lstrip().startswith("<") or len(prompt.split()) < 4 or
            prompt.startswith("Message from the operator (sent with ctxswitch)") or
            prompt.startswith("Message from another agent session, not from the operator"))


def _confirm_hash(key: str, prompt: str) -> str:
    import hashlib
    return hashlib.sha256((key + "\n" + prompt).encode()).hexdigest()


def _now() -> float:
    return time.time()                                          # unix time: comparable across processes and reboots


def _call(fn, timeout: float):
    """``fn()`` in a daemon thread, given up on after ``timeout`` (raising TimeoutError): a hub that hangs must
    never hold a prompt, and a daemon thread lets the process leave without waiting for it."""
    import threading
    box: dict = {}

    def go():
        try:
            box["r"] = fn()
        except BaseException as e:                                        # noqa: BLE001 - reported to the caller
            box["e"] = e

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"took over {timeout}s")
    if "e" in box:
        raise box["e"]
    return box.get("r")


def _confirm_load(path: Path, now: float) -> dict:
    """The ledger's still-young entries, anything older than CONFIRM_TTL_S dropped (and the file rewritten, but only
    when that changed it — a prompt of nobody's business doesn't touch it); {} when absent or unreadable."""
    try:
        d = json.loads(path.read_text())
        if not isinstance(d, dict):
            d = {}
    except (OSError, ValueError):
        return {}
    conf = {h: v for h, v in d.items() if isinstance(v, dict) and now - v.get("ts", 0) < CONFIRM_TTL_S}
    if conf != d:
        _confirm_save(path, conf)
    return conf


def _confirm_save(path: Path, d: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(d))


def _resend(cfg, row_id: int) -> None:
    """Tell the hub the blocked prompt was re-sent: mark its row "resent" (the operator insisted)."""
    if not row_id:
        return
    import urllib.request
    req = urllib.request.Request(f"{cfg.hub.url.rstrip('/')}/v1/misplaced/{row_id}/state",
                                 data=json.dumps({"state": "resent"}).encode(),
                                 headers={"Authorization": f"Bearer {cfg.hub.token}"} if cfg.hub.token else {},
                                 method="POST")
    urllib.request.urlopen(req, timeout=MISPLACE_TIMEOUT_S).close()


def prompt_check(d: dict, *, cfg=None, client=None, path: Path | None = None) -> str | None:
    """One synchronous prompt check: None = let it through (say nothing), else the text to block it with.

    The hub's deterministic matcher says "other" only when the prompt clearly belongs to a live session elsewhere;
    everything else — ok, no hub, an error, a timeout — lets it through. A blocked prompt re-sent within
    CONFIRM_TTL_S is let through and the hub is told the row is "resent" (the operator insisted)."""
    sid, prompt = d.get("session_id"), d.get("prompt")
    if d.get("hook_event_name") != "UserPromptSubmit" or not isinstance(sid, str) or not sid or "/" in sid:
        return None
    if not isinstance(prompt, str) or not prompt.strip() or _misplace_skip(prompt):
        return None
    from ctxswitch.common.config import load
    from ctxswitch.common.schema import session_key
    cfg = cfg or load()
    now = _now()
    path = CONFIRM_FILE if path is None else path
    key = session_key(cfg.host_id, "claude", sid)
    h = _confirm_hash(key, prompt)
    conf = _confirm_load(path, now)
    entry = conf.get(h)
    if isinstance(entry, dict):                                        # re-sent within the TTL: let it through
        del conf[h]
        _confirm_save(path, conf)
        try:
            _call(lambda: _resend(cfg, int(entry.get("id") or 0)), 0.5)
        except Exception:                                               # noqa: BLE001 - never hold the prompt for this
            pass
        return None
    if client is None:                                                  # no client to ask: fail open
        return None
    try:
        r = _call(lambda: client.prompt_check(key, prompt, d.get("prompt_id"), d.get("cwd")), MISPLACE_TIMEOUT_S)
    except Exception:                                                    # noqa: BLE001 - a dead hub never stops a prompt
        return None
    if not isinstance(r, dict) or r.get("verdict") != "other":
        return None
    _confirm_save(path, {**conf, h: {"ts": now, "id": r.get("id")}})
    return (f"ctx: this looks like it's for \"{r.get('target_title')}\" ({r.get('target_where')}) — {r.get('why')}.\n"
            "Press ↑ then Enter within a minute to send it here anyway, or move it from the ctx board (t).")


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
        elif argv[:1] == ["prompt-check"]:
            # The whole check must finish inside PROMPT_CHECK_DEADLINE_S even if the hub hangs: run it in a thread
            # and say nothing when it doesn't (a hub that's down never stops a prompt).
            from ctxswitch.client.hubclient import HubClient
            from ctxswitch.common.config import load
            cfg = load()
            try:
                reason = _call(lambda: prompt_check(json.loads(raw), cfg=cfg, client=HubClient(cfg)),
                               PROMPT_CHECK_DEADLINE_S)
            except Exception:                                             # noqa: BLE001 - fail open
                reason = None
            if reason:
                print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    except Exception:                                   # noqa: BLE001 - a hook must never break the agent
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
