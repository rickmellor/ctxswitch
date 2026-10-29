"""Formatting helpers shared by CLI and TUI: ages, status glyphs, one-line session summaries."""
from __future__ import annotations

from datetime import datetime, timezone

GLYPH = {"busy": "●", "waiting": "◐", "idle": "○", "shell": "$", "unknown": "◌", "ended": "–"}
HARNESS = {"claude": "cc", "pi": "pi", "claude-win": "cw"}


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def age(s: str | None, now: datetime | None = None) -> str:
    d = parse_iso(s)
    if d is None:
        return "?"
    now = now or datetime.now(timezone.utc)
    sec = max(0, int((now - d).total_seconds()))
    if sec < 60: return f"{sec}s"
    if sec < 3600: return f"{sec // 60}m"
    if sec < 86400: return f"{sec // 3600}h{(sec % 3600) // 60:02d}"
    return f"{sec // 86400}d"


def one_line(text: str | None, width: int) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= width else t[: width - 1] + "…"


def display_title(s: dict) -> str:
    """Title, else the last real prompt; command wrappers never make a title."""
    if s.get("title"):
        return s["title"]
    last = (s.get("last_turn") or {}).get("user_text") or ""
    return "" if last.lstrip().startswith("<") else last


def session_state(s: dict) -> str:
    """busy / waiting / idle / shell / unknown / ended."""
    st = s.get("live_status", "unknown")
    if s.get("ended") and st not in ("busy", "waiting", "idle"):
        return "ended"
    if s.get("open_asks"):
        return "waiting"
    return st


def state_glyph(s: dict) -> str:
    st = session_state(s)
    return "◐" if st == "waiting" else GLYPH.get(st, "?")


def short_key(s: dict) -> str:
    return f"{s.get('host_id','?')}/{HARNESS.get(s.get('harness',''), s.get('harness',''))}/{(s.get('session_id') or '')[:8]}"


def repo_label(s: dict) -> str:
    root = s.get("repo_root") or s.get("cwd") or ""
    name = root.rstrip("/").rsplit("/", 1)[-1] if root else ""
    br = s.get("git_branch") or ""
    return f"{name}@{br}" if name and br and br != "HEAD" else name or "?"


CLAIM_GLYPH = {"done": "✓", "tests_pass": "✓", "tests_fail": "✗", "blocked": "!", "other": "·"}


def instructions(s: dict) -> list[dict]:
    """Your standing instructions to this session that are still in force, oldest first."""
    return [r for r in s.get("ledger") or [] if r["kind"] == "instruction" and r["active"]]


def claims(s: dict, n: int = 4) -> list[dict]:
    """The agent's latest claims (done / tests / blocked), newest last."""
    return [r for r in s.get("ledger") or [] if r["kind"] == "claim"][-n:]


def decisions(s: dict, n: int = 6) -> list[tuple[int, str, str]]:
    """(turn, question, answer), oldest first: answered asks (hard or plain-text) and the choices the tracker read
    from your prompts. A choice that answers one ask is shown once: the ask verbatim, then the choice instead of
    your raw prompt."""
    asks = {a["ref"]: a for a in s.get("asks") or [] if a["answered"]}
    out, merged = [], set()
    for r in s.get("ledger") or []:
        if r["kind"] != "decision":
            continue
        extra = r.get("extra") or {}
        linked = [asks[ref] for ref in extra.get("answers") or [] if ref in asks]
        merged.update(a["ref"] for a in linked)
        q = linked[0]["text"] if len(linked) == 1 else extra.get("question") or ""
        out.append((r["turn_idx"], q, r["text"]))
    out += [(a.get("turn_idx") or 0, a["text"], a.get("answer") or "") for ref, a in asks.items() if ref not in merged]
    return sorted(out, key=lambda d: d[0])[-n:]


MSG_GLYPH = {"queued": "…", "sent": "→", "delivered": "✓", "replied": "↩", "failed": "✗", "cancelled": "–"}
HELD_MODES = ("plan", "bypassPermissions")          # Claude holds an unenveloped peer message for approval in these


def can_message(s: dict) -> str | None:
    """None if a message can be queued for this session, else why not."""
    if s.get("harness") == "claude-win":
        return "Windows-native Claude sessions can't take messages"
    return None


def messages(s: dict, n: int = 4) -> list[dict]:
    """Your newest messages to this session (cancelled ones left out), oldest first."""
    return [m for m in s.get("messages") or [] if m["status"] != "cancelled"][-n:]


def message_note(s: dict, m: dict) -> str:
    """What's happening to a message, in words."""
    st = m["status"]
    if st == "queued":
        return "delivering…" if s.get("live_status") in ("busy", "waiting", "idle", "shell") else "queued: delivered when it next runs"
    if st == "sent":
        mode = (s.get("meta") or {}).get("permission_mode")
        if s.get("harness") == "claude" and mode in HELD_MODES:
            return f"sent; Claude holds it for your approval in {mode} mode"
        return "sent; not in the transcript yet"
    if st == "delivered":
        return f"delivered as #{m.get('turn_idx')}, working on it"
    if st == "replied":
        return f"#{m.get('turn_idx')}: " + (m.get("reply") or "(no text reply)")
    if st == "failed":
        return f"failed: {m.get('error') or '?'}"
    return st


REPLYABLE = ("plain", "text")                       # hub/store.py: the asks a message can answer


def reply_target(s: dict) -> tuple[dict | None, str | None]:
    """(the open ask a quick-reply would answer, None) or (None, why there isn't one). The oldest replyable ask; a
    dialog (permission, plan, AskUserQuestion, pi prompt) can only be answered in the session itself."""
    why = can_message(s)
    if why:
        return None, why
    opens = [a for a in s.get("asks") or s.get("open_asks") or [] if not a.get("answered")]
    for a in opens:
        if a["kind"] in REPLYABLE:
            return a, None
    if opens:
        return None, f"it's waiting on a {opens[0]['kind']} dialog: answer it in the session (enter opens it)"
    return None, "nothing to answer: it isn't asking you anything"


def since_viewed(s: dict) -> dict | None:
    """What happened after your last look, from a full session (store.session): None if you never looked or
    nothing has. Ledger rows and asks from exchanges after ``seen_through``, plus the replies to your messages."""
    through = s.get("seen_through")
    if through is None or not s.get("unseen"):
        return None
    rows = [r for r in s.get("ledger") or [] if r["turn_idx"] > through and r["kind"] in ("decision", "claim", "instruction")]
    asks = [a for a in s.get("asks") or [] if (a.get("turn_idx") or 0) > through]
    replies = [m for m in s.get("messages") or [] if m["status"] == "replied" and (m.get("turn_idx") or -1) > through]
    return {"exchanges": s["unseen"], "rows": rows, "asks": asks, "replies": replies}


def since_line(d: dict) -> str:
    """One line: "3 exchanges · 1 question · 2 decisions · 1 claim"."""
    def n(k: int, word: str, plural: str = "") -> str:
        return f"{k} {word if k == 1 else plural or word + 's'}"
    kinds = [r["kind"] for r in d["rows"]]
    parts = [n(d["exchanges"], "exchange")]
    parts += [n(len(d["asks"]), "question")] if d["asks"] else []
    parts += [n(kinds.count(k), k) for k in ("decision", "claim", "instruction") if k in kinds]
    parts += [n(len(d["replies"]), "reply", "replies")] if d["replies"] else []
    return " · ".join(parts)


def workstream_label(ws: str | None) -> str:
    """"owner/name@branch" -> "name@branch"; host:/path/x@b -> "x@b"."""
    if not ws:
        return "(no repo)"
    repo, _, br = ws.rpartition("@")
    name = repo.rstrip("/").rsplit("/", 1)[-1]
    return f"{name}@{br}" if br else name


def show_lines(s: dict, turns: int = 6, n_decisions: int = 8, now: datetime | None = None) -> list[str]:
    """`ctx show` (and the MCP get_session): meta, what it's for, your instructions, open asks, decisions, messages,
    the same work elsewhere, then the last ``turns`` exchanges."""
    now = now or datetime.now(timezone.utc)
    out: list[str] = []
    out.append(f"{state_glyph(s)} {s['key']}   [{session_state(s)} since {age(s.get('status_since') or s.get('last_activity'), now)}]")
    out.append(f"  title : {s.get('title') or '-'}")
    out.append(f"  where : {s.get('cwd') or '-'}  ({repo_label(s)})   model: {s['meta'].get('model') or '-'}  perm: {s['meta'].get('permission_mode') or '-'}")
    out.append(f"  turns : {s.get('turn_count')}   started {age(s.get('started_at'), now)} ago, last activity {age(s.get('last_activity'), now)} ago"
          + (f", viewed {age(s.get('last_viewed_at'), now)} ago" if s.get('last_viewed_at') else ""))
    new = since_viewed(s)
    if new:
        out.append(f"  since you looked ({age(s.get('last_viewed_at'), now)} ago): {since_line(new)}")
        for r in new["rows"][-5:]:
            out.append(f"        {r['kind']}: {one_line(r['text'], 100)}")
    if s.get("purpose"):
        out.append(f"  for   : {s['purpose']}")
    if s.get("recap"):
        out.append(f"  recap : {s['recap']}")
    for line in (s.get("reentry") or "").splitlines():
        out.append(f"        › {line}")
    rules = instructions(s)
    if rules:
        out.append("  your instructions:")
        for r in rules:
            out.append(f"    • {one_line(r['text'], 100)}   (#{r['turn_idx']}, ledger id {r['id']})")
    said = claims(s)
    if said:
        out.append("  agent says:")
        for r in said:
            out.append(f"    {CLAIM_GLYPH.get(r['extra'].get('claim'), '·')} {one_line(r['text'], 100)}   (#{r['turn_idx']})")
    opens = [x for x in s.get("asks", []) if not x["answered"]]
    if opens:
        out.append("  OPEN ASKS:")
        for x in opens:
            out.append(f"    ◐ [{x['kind']}] {one_line(x['text'], 100)}   (waiting {age(x.get('asked_at'), now)})")
    decided = decisions(s, n_decisions)
    if decided:
        out.append("  decisions:")
        for _, q, ans in decided:
            out.append(f"    ✓ {one_line(q, 70) + '  →  ' if q else ''}{one_line(ans, 60)}")
    msgs = messages(s, 6)
    if msgs:
        out.append("  your messages:")
        for m in msgs:
            out.append(f"    {MSG_GLYPH.get(m['status'], '?')} [{m['id']}] {one_line(m['body'], 70)}")
            out.append(f"        {one_line(message_note(s, m), 100)}")
    sibs = s.get("siblings") or []
    if sibs:
        out.append(f"  same work elsewhere ({workstream_label(s.get('workstream'))}):")
        for x in sibs:
            out.append(f"    {x['key']}  {x.get('live_status')}  {one_line(x.get('title'), 60)}")
            for r in x.get("instructions") or []:
                out.append(f"        told it: {one_line(r, 90)}")
    out.append("  exchanges:")
    for t in s.get("turns", [])[-turns:]:
        flag = "" if t.get("complete") else " (in progress)"
        out.append(f"    #{t['idx']} {age(t.get('started_at'), now):>5} ago [{t.get('user_origin')}]{flag}")
        out.append(f"       you  : {one_line(t.get('user_text'), 110)}")
        if t.get("assistant_text"):
            out.append(f"       agent: {one_line(t.get('assistant_text'), 110)}")
        tally = t.get("tally") or {}
        ev = t.get("events") or []
        extras = ", ".join(f"{v} {k}" for k, v in tally.items() if v) + ("; " + ", ".join(ev) if ev else "")
        if extras:
            out.append(f"       tools: {extras}")
    return out
