"""Formatting helpers shared by CLI and TUI: ages, status glyphs, one-line session summaries."""
from __future__ import annotations

from datetime import datetime, timezone

GLYPH = {"busy": "●", "idle": "○", "shell": "$", "unknown": "◌", "ended": "–"}
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
    if s.get("ended") and st not in ("busy", "idle"):
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
