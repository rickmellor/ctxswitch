"""Pop-ups for new attention items: a tmux status-line message (display-message), nothing outside tmux.

The hub's attention list is the source; each TUI diffs it against what it has already seen, so a pop-up is per
machine (config ``notify``) and nothing is shown for what was already waiting when the TUI started. Questions and
dialogs always pop up; an agent that merely finished its turn ("idle") only with ``notify_idle``.
"""
from __future__ import annotations

import subprocess

from ctxswitch.client import fmt

SHOW_MS = 8000


def item_id(i: dict) -> str:
    """Stable per occurrence: an ask by its ref, a waiting/idle session by when that state began."""
    if i.get("item") == "ask":
        return f"{i['key']}|{i['ref']}"
    return f"{i['key']}|{i.get('item')}|{i.get('asked_at')}"


class Notifier:
    def __init__(self, idle: bool = False):
        self.idle = idle
        self.seen: set[str] | None = None                # None until the first list: what's already there is not news

    def fresh(self, items: list[dict], skip: str | None = None) -> list[dict]:
        """Items not seen before (the first call only primes); ``skip`` = the session on screen right now."""
        ids = {item_id(i): i for i in items}
        if self.seen is None:
            self.seen = set(ids); return []
        new = [i for k, i in ids.items() if k not in self.seen
               and (i.get("item") != "idle" or self.idle) and i.get("key") != skip]
        self.seen = set(ids)                             # forget what left, so the same session can pop up again
        return new


def text(new: list[dict]) -> str:
    """One status line for one or more new items."""
    i = new[0]
    what = fmt.one_line(i.get("text"), 120) if i.get("item") == "ask" else "waiting on a dialog" if i.get("item") == "waiting" else "finished, your turn"
    more = f"  (+{len(new) - 1} more)" if len(new) > 1 else ""
    return f"ctx · {i.get('host_id', '?')} · {fmt.one_line(i.get('title') or i.get('key'), 30)}: {what}{more}"


def tmux_popup(msg: str, run=subprocess.run) -> bool:
    """display-message expands #{…} and #[…], so every # is doubled; argv, no shell."""
    r = run(["tmux", "display-message", "-d", str(SHOW_MS), msg.replace("#", "##")], capture_output=True, text=True)
    return r.returncode == 0
