"""Pop-ups for new attention items: a tmux popup box (display-popup) or a status-line message (display-message).

The hub's attention list is the source; each TUI diffs it against what it has already seen, so a pop-up is per
machine (config ``notify``). What's waiting when the TUI starts pops up once (one line, "+N more"). Questions and
dialogs always pop up; an agent that merely finished its turn ("idle") only with ``notify_idle``. Each attempt is
logged to ~/.local/state/ctxswitch/popup.log. ``notify_style``: "popup" (default) or "status".
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ctxswitch.client import fmt

SHOW_MS = 8000
BOX_ITEMS = 6                                            # lines in a box before "+N more"
# the box shows $CTX_POPUP (-e passes it through literally: no format or shell expansion) and closes on any key
BOX_CMD = """exec bash -c 'printf "%s\\n" "$CTX_POPUP"; read -rsn1 _'"""
LOG = Path.home() / ".local/state/ctxswitch/popup.log"


def item_id(i: dict) -> str:
    """Stable per occurrence: an ask by its ref, a waiting/idle session by when that state began."""
    if i.get("item") == "ask":
        return f"{i['key']}|{i['ref']}"
    return f"{i['key']}|{i.get('item')}|{i.get('asked_at')}"


class Notifier:
    def __init__(self, idle: bool = False):
        self.idle = idle
        self.seen: set[str] = set()

    def fresh(self, items: list[dict], skip: str | None = None) -> list[dict]:
        """Items not seen before; ``skip`` = the session on screen right now."""
        ids = {item_id(i): i for i in items}
        new = [i for k, i in ids.items() if k not in self.seen
               and (i.get("item") != "idle" or self.idle) and i.get("key") != skip]
        # forget what left, so the same session can pop up again; what was skipped on screen stays unseen, so it
        # pops up once you move off it with the question still open
        self.seen = {k for k, i in ids.items() if k in self.seen or i.get("key") != skip}
        return new


def _line(i: dict, width: int = 120) -> str:
    what = fmt.one_line(i.get("text"), width) if i.get("item") == "ask" else "waiting on a dialog" if i.get("item") == "waiting" else "finished, your turn"
    return f"{i.get('host_id', '?')} · {fmt.one_line(i.get('title') or i.get('key'), 30)}: {what}"


def text(new: list[dict]) -> str:
    """One status line for one or more new items."""
    more = f"  (+{len(new) - 1} more)" if len(new) > 1 else ""
    return f"ctx · {_line(new[0])}{more}"


def box_lines(new: list[dict]) -> list[str]:
    out = [_line(i, 90) for i in new[:BOX_ITEMS]]
    if len(new) > BOX_ITEMS:
        out.append(f"+{len(new) - BOX_ITEMS} more on the board")
    return out + ["", "any key closes · Enter on the board opens it"]


def tmux_box(new: list[dict], popen=subprocess.Popen):
    """A centred box over every pane until a key is pressed. display-popup blocks until it closes, so it's started,
    not waited on; the caller polls it (rc 1 = no client to show it on). tmux drops a popup silently (rc 0) while
    another is open, so don't start one while ours is."""
    lines = box_lines(new)
    w, h = min(max(map(len, lines)) + 4, 120), len(lines) + 2
    title = f" ctx: {len(new)} waiting on you " if len(new) > 1 else " ctx: waiting on you "
    return popen(["tmux", "display-popup", "-E", "-w", str(w), "-h", str(h), "-T", title,
                  "-e", "CTX_POPUP=" + "\n".join(lines), BOX_CMD],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def tmux_popup(msg: str, run=subprocess.run) -> bool:
    """The status-line style. display-message expands #{…} and #[…], so every # is doubled; argv, no shell. -N: any
    key event clears a message, and with the mouse on, motion over a mouse-tracking pane (the Textual board) is one,
    so without it the pop-up flashes and is gone. rc 0 even with no client attached (the message goes nowhere), so
    this only catches a missing or refusing tmux."""
    r = run(["tmux", "display-message", "-N", "-d", str(SHOW_MS), msg.replace("#", "##")], capture_output=True, text=True)
    return r.returncode == 0


def log(line: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass
