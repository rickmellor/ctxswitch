"""Resume briefing: what you need back in your head when you resume a session, from hard signals plus the tracker's
purpose and your standing instructions when it has them.

Shown to the operator (not the model) when a session is resumed: Claude via the SessionStart hook's
systemMessage, pi via ui.notify. Nothing when the session was busy moments ago (BRIEF_AFTER_S): you remember.

  ctx · <title> · away 3h12
  for    : <what the session is for, when the tracker knows>
  recap  : <one-line recap, when the hub has one>
  said   : <a standing instruction you gave it>  (up to MAX_RULES, newest)
  you    : <your last prompt>
  agent  : <its last reply>
  open   : [plan] <open ask> (2h ago)          (up to MAX_ASKS, oldest first)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

BRIEF_AFTER_S = 15 * 60
MAX_ASKS = 3
MAX_RULES = 3
WIDTH = 160


def _ts(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _age(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}" if h < 48 else f"{h // 24}d"


def _line(s: Any) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= WIDTH else s[: WIDTH - 1] + "…"


def brief(s: dict[str, Any] | None, now: datetime | None = None, after_s: float = BRIEF_AFTER_S) -> str | None:
    """The briefing for one store.session() dict, or None (unknown session, nothing said yet, or away too briefly)."""
    if not s:
        return None
    now = now or datetime.now(timezone.utc)
    last = _ts(s.get("last_activity"))
    away = (now - last).total_seconds() if last else None
    if away is not None and away < after_s:
        return None
    turns = [t for t in s.get("turns") or [] if t.get("user_text") or t.get("assistant_text")]
    if not turns:
        return None
    you = next((t["user_text"] for t in reversed(turns) if t.get("user_text") and t.get("user_origin") in (None, "human")), None)
    agent = next((t["assistant_text"] for t in reversed(turns) if t.get("assistant_text")), None)
    head = "ctx · " + _line(s.get("title") or s.get("key"))
    if away is not None:
        head += f" · away {_age(away)}"
    out = [head]
    if s.get("purpose"):
        out.append(f"for    : {_line(s['purpose'])}")
    if s.get("recap"):
        out.append(f"recap  : {_line(s['recap'])}")
    rules = [r for r in s.get("ledger") or [] if r.get("kind") == "instruction" and r.get("active")]
    out += [f"said   : {_line(r['text'])}" for r in rules[-MAX_RULES:]]
    if you:
        out.append(f"you    : {_line(you)}")
    if agent:
        out.append(f"agent  : {_line(agent)}")
    opens = [a for a in s.get("asks") or [] if not a.get("answered")]
    for a in opens[:MAX_ASKS]:
        at = _ts(a.get("asked_at"))
        out.append(f"open   : [{a.get('kind')}] {_line(a.get('text'))}" + (f" ({_age((now - at).total_seconds())} ago)" if at else ""))
    if len(opens) > MAX_ASKS:
        out.append(f"         … and {len(opens) - MAX_ASKS} more (ctx show {s.get('key')})")
    return "\n".join(out)
