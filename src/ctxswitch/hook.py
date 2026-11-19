"""ctx-hook: Claude Code hook → one spool file per event. Stdlib only; it runs on every hooked event.

  ctx-hook claude             reads the hook's JSON on stdin (installed by `ctx install` for the events in EVENTS)
  ctx-hook agy <Event>        Antigravity's hook for one of AGY_EVENTS, synchronous (it blocks agy's loop): prints
                              exactly one JSON object, whatever happens (see the agy section below)
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

Antigravity's hooks are in the GLOBAL ~/.gemini/config/hooks.json and fire for EVERY agy session on this host,
synchronously, blocking agy's loop (measured on 1.5.12). So `ctx-hook agy <Event>` prints exactly one JSON object and
exits 0 whatever happens, and PreToolUse must answer an explicit decision there — `{}` DENIES the tool, so the neutral
answer is {"decision":"ask"}. Every other event may answer {}.
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

# Antigravity (measured on 1.5.12): the events `ctx install` hooks. Only these four are spooled — PreInvocation fires
# on every model call, so spooling it would add a file per call for nothing. toolCall.args is normalized to the
# Claude-style keys the hub's hook applier reads.
AGY_EVENTS = ("SessionStart", "PreInvocation", "PreToolUse", "PostToolUse", "Stop")
AGY_ASK_TOOL = "ask_question"                               # agy's AskUserQuestion
ANY = object()                                              # spooled whatever the tool is
# what gets spooled, and the tool whose calls do: PreInvocation fires on every model call, so it isn't spooled at all
AGY_SPOOL = {"SessionStart": ANY, "Stop": ANY, "PreToolUse": AGY_ASK_TOOL, "PostToolUse": AGY_ASK_TOOL}
AGY_KEYS = (("tool_name", ("toolCall", "name")),            # agy's names → the ones the hub's applier reads
            ("tool_input", ("toolCall", "args")),
            ("cwd", ("cwd",)), ("transcript_path", ("transcriptPath",)),
            ("reason", ("terminationReason",)), ("error", ("error",)))
AGY_ASK = {"decision": "ask"}                               # PreToolUse's neutral answer; {} there DENIES the tool
INBOX_DIR_NAME = "agy-inbox"                                # one <conversationId>.jsonl per conversation (see below)


def spool_dir() -> Path:
    return Path(os.environ.get("CTXSWITCH_SPOOL") or os.path.expanduser("~/.local/state/ctxswitch/spool"))


def agy_inbox_dir() -> Path:
    """Where the collector drops messages for an agy conversation; the hook below claims them."""
    return Path(os.environ.get("CTXSWITCH_AGY_INBOX") or os.path.expanduser(f"~/.local/state/ctxswitch/{INBOX_DIR_NAME}"))


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


def prompt_check(d: dict, *, cfg=None, client=None, path: Path | None = None, harness: str = "claude") -> str | None:
    """One synchronous prompt check: None = let it through (say nothing), else the text to block it with.

    The hub's deterministic matcher says "other" only when the prompt clearly belongs to a live session elsewhere;
    everything else — ok, no hub, an error, a timeout — lets it through. A blocked prompt re-sent within
    CONFIRM_TTL_S is let through and the hub is told the row is "resent" (the operator insisted).

    ``harness`` only picks the session key and where the text lives: Claude's UserPromptSubmit carries "prompt",
    Antigravity's PreInvocation(0) carries the turn's prompt in "lastUserInput"."""
    if harness == "claude":
        sid, prompt = d.get("session_id"), d.get("prompt")
        if d.get("hook_event_name") != "UserPromptSubmit":
            return None
    else:
        sid, prompt = d.get("conversationId"), d.get("lastUserInput")
    if not isinstance(sid, str) or not sid or "/" in sid:
        return None
    if not isinstance(prompt, str) or not prompt.strip() or _misplace_skip(prompt):
        return None
    from ctxswitch.common.config import load
    from ctxswitch.common.schema import session_key
    cfg = cfg or load()
    now = _now()
    path = CONFIRM_FILE if path is None else path
    key = session_key(cfg.host_id, harness, sid)
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


def _prompt_reason(d: dict, harness: str, *, cfg=None, client=None, path: Path | None = None) -> str | None:
    """The misplaced-prompt check over the whole command's deadline, failing open: a hub that's down or slow never
    stops a prompt (Claude's UserPromptSubmit) or a turn (Antigravity's first PreInvocation)."""
    if client is None:
        from ctxswitch.client.hubclient import HubClient
        from ctxswitch.common.config import load
        cfg = cfg or load()
        client = HubClient(cfg)
    try:
        return _call(lambda: prompt_check(d, cfg=cfg, client=client, path=path, harness=harness),
                     PROMPT_CHECK_DEADLINE_S)
    except Exception:                                       # noqa: BLE001 - fail open
        return None


# -- Antigravity (agy 1.5.12) -------------------------------------------------------------------------------------
def _get(d: dict, keys: tuple):
    """d's value under the first of ``keys`` that's there (agy nests a couple of the ones we want)."""
    x = d
    for k in keys:
        x = x.get(k) if isinstance(x, dict) else None
    return x


def _agy_data(d: dict) -> dict:
    """agy's payload under the names the hub's hook applier reads (Claude's)."""
    out = {}
    for dst, keys in AGY_KEYS:
        v = _get(d, keys)
        if v is not None:
            out[dst] = _trim(v)
    if out.get("tool_name") == AGY_ASK_TOOL:                # the hub asks the question, not "allow ask_question: …"
        out["questions"] = _agy_questions(_get(d, ("toolCall", "args")))
    return out


def _agy_questions(args) -> list[dict]:
    """ask_question's args as Claude-style question entries. agy 1.5.12 sends exactly Claude's AskUserQuestion shape
    (measured): {"questions": [{"question", "options": [str, ...], "is_multi_select"}]}; anything else falls back to
    the one-question guess below."""
    qs = args.get("questions") if isinstance(args, dict) else None
    out = []
    for q in qs if isinstance(qs, list) else []:
        if isinstance(q, dict) and isinstance(q.get("question"), str) and q["question"].strip():
            out.append({"question": q["question"], **({"options": q["options"]} if isinstance(q.get("options"), list) else {})})
    return out or [_agy_question(args)]


def _agy_question(args) -> dict:
    """ask_question's args as one Claude-style question entry; the hub's question_text() makes the ask's text from
    it, exactly as for Claude. agy's arg key names aren't documented, so: the question is the first string whose key
    mentions "question" (else the first string), the options any list whose key mentions "option"."""
    if not isinstance(args, dict):
        return {"question": str(args or "")}
    low = {k.lower(): v for k, v in args.items()}
    q = next((v for k, v in low.items() if isinstance(v, str) and v.strip() and "question" in k), "")
    opts = next((v for k, v in low.items() if isinstance(v, list) and "option" in k), None)
    if not q:
        q = next((v for k, v in args.items() if isinstance(v, str) and v.strip()), "")
    return {"question": q, "options": opts} if opts else {"question": q}


def _spools(event: str, d: dict) -> bool:
    """Whether this agy event goes in the spool: the four that say something the transcript can't, and for the two
    tool events only ask_question — every other tool call would be a file per call."""
    if event not in AGY_SPOOL:
        return False
    want = AGY_SPOOL[event]
    return want is ANY or want == _get(d, ("toolCall", "name"))


def _agy_inbox(sid) -> list[str]:
    """The texts waiting for this conversation, in file order. The whole file is claimed at once with a rename (the
    collector appends while we work), read, then deleted; a missing file is nothing pending, an unparseable line is
    skipped. Their contents only ever leave through the injected step."""
    if not isinstance(sid, str) or not sid or "/" in sid or "\x00" in sid:
        return []
    f = agy_inbox_dir() / f"{sid}.jsonl"
    taken = f.with_name(f"{f.name}.taking-{os.getpid()}")
    try:
        os.rename(f, taken)
    except OSError:
        return []
    try:
        lines = taken.read_text(errors="replace").splitlines()
    finally:
        try:
            taken.unlink()
        except OSError:
            pass
    out = []
    for ln in lines:
        try:
            text = json.loads(ln).get("text")
        except (ValueError, AttributeError):
            continue
        if isinstance(text, str) and text.strip():
            out.append(text)
    return out


def handle_agy(event: str, d: dict, spool: Path, *, cfg=None, client=None, path: Path | None = None) -> dict:
    """One Antigravity hook event → the single JSON object to print. Called for every agy session on the host,
    blocking its loop, so it answers fast and never raises (``main`` prints the event's safe default if it does).

    SessionStart   the hub's briefing, injected as a system message (`{}` when there's nothing to say)
    PreInvocation  the conversation's inbox messages, one user message each; else, on the turn's first model call
                   only, the misplaced-prompt check on what the operator just typed
    PreToolUse     the neutral decision (only ask_question's call is spooled; every other tool must not reach us)
    PostToolUse    nothing (ask_question's answer resolved from the transcript)
    Stop           the inbox again — an idle session gets no PreInvocation, so this is what delivers to it
    """
    sid = d.get("conversationId")
    ok_sid = isinstance(sid, str) and bool(sid) and "/" not in sid
    if ok_sid and _spools(event, d):
        try:
            write_event(spool, "agy", sid, event, _agy_data(d))
        except Exception:                                   # noqa: BLE001 - a full or blocked spool changes no answer
            pass
    if event == "PreToolUse":
        return dict(AGY_ASK)                                # {} here would deny the tool: always answer explicitly
    if event == "SessionStart":
        try:
            text = fetch_brief("agy", sid) if ok_sid else None
        except Exception:                                   # noqa: BLE001 - no hub, no token, too slow: say nothing
            text = None
        return {"injectSteps": [{"systemMessage": {"systemMessage": text}}]} if text else {}
    if event == "PreInvocation":
        texts = _agy_inbox(sid)
        if texts:
            return {"injectSteps": [{"userMessage": t} for t in texts]}
        # invocationNum 0 is the turn's first model call, the only one whose lastUserInput is what was just typed.
        if d.get("invocationNum") == 0 and isinstance(d.get("lastUserInput"), str) and d["lastUserInput"].strip():
            reason = _prompt_reason(d, "agy", cfg=cfg, client=client, path=path)
            if reason:
                return {"injectSteps": [{"errorMessage": {"userMessage": reason}}], "terminationBehavior": "terminate"}
        return {}
    if event == "Stop":
        texts = _agy_inbox(sid)
        return {"decision": "continue", "reason": "\n\n".join(texts)} if texts else {}
    return {}


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
        elif argv[:1] == ["agy"]:
            event = argv[1] if len(argv) > 1 else ""
            try:
                d = json.loads(raw)
                out = handle_agy(event, d if isinstance(d, dict) else {}, spool_dir())
            except Exception:                               # noqa: BLE001 - an agy loop must still get an answer
                out = {}
            print(json.dumps(out or (dict(AGY_ASK) if event == "PreToolUse" else {}), ensure_ascii=False))
        elif argv[:1] == ["prompt-check"]:
            # The whole check must finish inside PROMPT_CHECK_DEADLINE_S even if the hub hangs: run it in a thread
            # and say nothing when it doesn't (a hub that's down never stops a prompt).
            reason = _prompt_reason(json.loads(raw), "claude")
            if reason:
                print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    except Exception:                                   # noqa: BLE001 - a hook must never break the agent
        if argv[:1] == ["agy"]:                         # ... and agy needs an answer: {} would deny a PreToolUse
            print(json.dumps(dict(AGY_ASK) if argv[1:2] == ["PreToolUse"] else {}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
