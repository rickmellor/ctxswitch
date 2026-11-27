"""``ctx deck`` — an Elgato Stream Deck as a button box for the board.

The board (`ctx tui`) listens on a unix socket (PROTOCOL.md: NDJSON, one object per line). This talks that protocol
and paints the 15 keys of a Stream Deck MK.2 with what the board is showing: five session slots; the middle row's
`gpu` `pool` `spend` `exchange` `detach`, the two panels and the spend screen lit while they are up; and ▲▼ to move
the highlight (hold one and it opens the row it landed on), `restart`, `new` and `kill` — the last of which makes the
board ask, so the key becomes KILL ✓ / cancel. While a modal is up the middle row is cancel with the bottom one, the
two exceptions being `spend` while the spend screen is showing, which closes it, and the Start screen, whose own
cursor, marks and Enter are worth five keys of their own — ▲▼ over its recent rows, `select` to mark one, `cancel`,
and `enter` to resume them — with the board's commands blank in between.

Everything that decides *what a key looks like* or *what a press sends* lives in `Layout`, which needs neither
Pillow, nor a device, nor a board — so that is the part the tests read. `render` and `DeckApp` only carry it out.

The board can have more rows than the five the top row has keys for, and the settings file says which of two ways the
top row deals with that. Paging is `pages()`: a row count laid out over as many pages of five keys as it needs, four
rows plus a `▶` on the first, `◀` plus three on a middle one, `◀` plus what is left on the last. Scrolling is
`scroll_window()`: a window of four consecutive rows that one arrow moves along by exactly one row, `▶` on the last
key while rows remain beyond it and `◀` on the first once they don't, so the far right is the last four rows. Either
way the move is the deck's own business — an arrow press never reaches the board — and the deck follows the highlight,
so walking ▲▼ off the edge of the window brings the window along.

Those settings — the top row, the key brightness, and whether the faces animate — come from one small JSON file
(`common/deck_settings.py`, `docs/deck-settings.md`) that the board's `D` screen writes: the deck reads it at start
and once a second after that, and applies what changed while it runs. An explicit `--brightness` wins over the file
for that run.

The faces move: a waiting session breathes, a busy one sweeps a bar down its left edge, a key whose switch the board
hasn't confirmed yet pulses white, and the switch itself fades out of white when the pane comes round. All four are a
function of the state, the page and one clock, so the tests read them too.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ctxswitch.common.deck_settings import FILE as SETTINGS_FILE, load_settings

COLS = 5
ROWS = 3
KEY_COUNT = COLS * ROWS
KEY_W = 72                         # an MK.2 key, in pixels: what a label is fitted to
KEY_H = 72                         # …and how tall it is, which is what an animation moves along
ROW2 = 5                           # the middle row's first key: keys 5…9 are the board's panels and its exits
ROW2_CMDS = ("gpu", "pool", "spend", "exchange", "detach")     # what each of those keys is, and what it sends
HOLD_MS = 600                      # ▲/▼ held this long opens the highlighted row on release
RECONNECT_S = 2.0                  # how often to retry the board while it is unreachable
HEARTBEAT_S = 20.0                 # `ctx deck` runs for hours: one line of proof it's still awake, every 20 s
FPS = 20                           # the animation clock: measured on the device, a full board repaint fits in one
SETTINGS_TICKS = FPS               # …and the settings file is looked at once a second, which is every 20th of them
BREATHE_S = 1.6                    # a waiting session breathes: dark to red and back, once over this long
SWEEP_S = 1.2                      # a busy session sweeps: the bar takes this long down, and the same back up
WIPE_S = 0.25                      # the key a switch just made visible fades out of white over this long
PENDING_S = 6.0                    # how long to keep waiting for a switch this deck asked the board to make
PULSE_S = 0.8                      # a key waiting on that switch pulses on this cycle while it thinks
PULSE_DEPTH = 0.15                 # …barely: white, with the key's own colour let back in this far
SWEEP_H = 16                       # how tall the sweep bar is, in pixels, on a 72 px key
SWEEP_W = 2                        # its rightmost pixel column: 3 px along the key's left edge, clear of the text
SWEEP_TOP = 4                      # where the bar's top edge starts, and stops at the far end: h - SWEEP_BOTTOM
SWEEP_BOTTOM = 20                  # …so a 72 px key gives it 48 px of travel, top bar to bottom bar

SLOT_BG = ("#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd")   # fixed per slot key
WAIT_BG = "#ff3b30"                # a waiting slot at the top of its breath (and the border it always wears)
DOT = {"busy": "#ffd700", "waiting": WAIT_BG, "idle": "#34c759",
       "shell": "#8e8e93", "unknown": "#8e8e93", "ended": "#3a3a3c"}
DIM_GREY = "#3a3a3c"               # the text of a key the board says nothing about
ARROW_BG = "#444444"
ACTION_BG = "#333333"
LIT_BG = "#1f4f7a"                 # an action key whose thing is on: the panel is up, the screen is showing
END_BG = "#5a1a1a"
CONFIRM_BG = "#d62728"             # the KILL ✓ key, while a confirm is up
OPTION_BG = "#2b2b2b"
TYPE_HINT = "type on\nthe board"        # the three bottom-row keys while the board's text box has focus
BLACK = "#000000"
WHITE = "#ffffff"

FONT_BOLD = "DejaVuSans-Bold.ttf"
FONT_REG = "DejaVuSans.ttf"


def sock_path(explicit: str | None = None) -> Path:
    """The board's control socket: ``--sock``, else ``$CTXSWITCH_BOARD_SOCK``, else the usual place."""
    return Path(explicit or os.environ.get("CTXSWITCH_BOARD_SOCK")
                or Path.home() / ".local" / "state" / "ctxswitch" / "board.sock")


def _hex(c: str) -> tuple[int, int, int]:
    s = str(c).lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def _rgb(t: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % t


def blend(c1: str, c2: str, a: float) -> str:
    """Two colours, mixed per channel: all of ``c1`` at 0, all of ``c2`` at 1, and straight between.

    The animation's one colour routine, so it is the one place the ends have to be exact: a face is compared by
    value to decide whether a key needs repainting, and a mix that drifted at a=0 or a=1 would repaint a still key."""
    a = min(1.0, max(0.0, a))
    one, two = _hex(c1), _hex(c2)
    return _rgb(tuple(round(x + (y - x) * a) for x, y in zip(one, two)))


def breathe(t: float) -> float:
    """How red a waiting session is right now: 0 at the bottom of a breath, 1 at the top, back to 0, over
    ``BREATHE_S`` seconds. Smooth at both ends — a cosine, so the turn around costs nothing to the eye.

    Quantized to 1/32 steps: at 20 fps several frames in a row land on the same shade, and two frames that look the
    same must compare equal or the key goes back on the wire for nothing."""
    return round((0.5 - 0.5 * math.cos(2 * math.pi * (t % BREATHE_S) / BREATHE_S)) * 32) / 32


def sweep_y(t: float, h: int) -> int:
    """The top edge of a busy key's sweep bar: down the key's left edge over ``SWEEP_S`` seconds and back up over
    the next, as a triangle wave, in whole pixels — which is as fine as a frame can move anyway.

    It travels between ``SWEEP_TOP`` and ``h - SWEEP_BOTTOM`` so the bar stays inside the key's own bars."""
    lo, hi = SWEEP_TOP, max(SWEEP_TOP, h - SWEEP_BOTTOM)
    phase = (t % (2 * SWEEP_S)) / SWEEP_S
    return round(lo + (hi - lo) * (phase if phase <= 1.0 else 2.0 - phase))


@dataclass
class KeyFace:
    """Everything needed to paint one key — and everything a test wants to know about it."""
    bg: str = BLACK
    lines: tuple[str, ...] = ()
    top_bar: bool = False
    bottom_bar: bool = False
    dot: str | None = None
    border: str | None = None
    dim: bool = False
    ink: str | None = None                    # text in this colour instead of white: a white key's own palette colour
    edge: tuple[int, int, str] | None = None   # a sweep bar on the key's left edge: (y0, y1, colour)
    small: bool = False                        # every line in the 11 px board text size (the input box's prompt and text)
    sub: str | None = None                     # one more line under `lines`, always 11 px: a label that keeps its own
                                               # size and says something small underneath it


def pulse(t: float) -> float:
    """How far a key waiting on the board has let its colour back in: 0 at white, 1 at the most tinted it goes, on a
    ``PULSE_S`` cycle. The same cosine as the breath — so the turn round costs nothing to the eye — scaled down to
    ``PULSE_DEPTH``, because this key is white first and only thinks in shades."""
    return PULSE_DEPTH * round((0.5 - 0.5 * math.cos(2 * math.pi * (t % PULSE_S) / PULSE_S)) * 32) / 32


def pages(n_rows: int) -> list[list[int | str]]:
    """How ``n_rows`` rows lie out over the top row's five keys, page by page: five entries to a page, each one a
    1-based row number, ``"prev"`` / ``"next"`` for an arrow, or ``None`` for a key with nothing on it.

    Up to five rows fit as they are. Past that a page gives up its last key to `▶` — and every page after the first
    gives up its first to `◀` — so four rows, three, then whatever is left land on the keys: 6 rows is
    ``[1 2 3 4 ▶]`` and ``[◀ 5 6 · ·]``, 9 is three pages, 12 is four."""
    n = max(0, int(n_rows))
    if n <= 5:
        return [[i + 1 if i < n else None for i in range(5)]]
    out: list[list[int | str]] = [[i + 1 for i in range(4)] + ["next"]]
    shown = 4
    while shown < n:
        rest = n - shown
        if rest <= 4:                                       # what is left fits one page, arrows and all: that's the
            out.append(["prev"] + [shown + i + 1 for i in range(rest)] + [None] * (4 - rest))   # last one
            break
        else:
            out.append(["prev"] + [shown + i + 1 for i in range(3)] + ["next"])
            shown += 3
    return out


def scroll_step(offset: int, delta: int) -> int:
    """Where one arrow press takes the scroll offset. At the far left the window is rows 1-4 on keys 0-3; one step
    right would put rows 2-4 on keys 1-3 - the same keys they were on - so the only visible change would be row 1
    going away. The first step right goes two instead, to rows 3-5, and the step back from there returns to 0."""
    if delta > 0 and offset == 0:
        return 2
    new = offset + delta
    return 0 if (delta < 0 and new <= 1) else new


def scroll_window(n_rows: int, offset: int) -> tuple[list[int | str | None], int]:
    """How ``n_rows`` rows lie under the top row's five keys when the row *scrolls* instead of paging: the five
    entries for the window that starts at 0-based ``offset``, and that offset as it actually stands.

    Five rows or fewer fit as they are, with no arrows at all. Past that one arrow press moves the window along by
    exactly one row, and which keys the arrows cost depends on where the window is: rows ``1–4`` and `▶` at the left
    end, `◀` plus rows ``o+1…o+3`` plus `▶` in the middle, and `◀` plus the last four rows at the far right — so one
    row comes on at one end as one goes off at the other, until the far right, where the `▶` is the key that stops
    being needed and the fifth row comes out. An offset past either end is pulled back to the nearest one that means
    something, and the window handed back is the one that offset draws, so a caller can keep its own number and still
    get a legal top row."""
    n = max(0, int(n_rows))
    o = min(max(0, int(offset)), max(0, n - 4))              # the first row a window can start at
    if n <= 5:
        return [i + 1 if i < n else None for i in range(5)], o
    if o == 0:
        return [1, 2, 3, 4, "next"], o
    if o + 4 < n:                                            # rows behind *and* ahead: an arrow at each end
        return ["prev"] + [o + 1 + i for i in range(3)] + ["next"], o
    return ["prev"] + [n - 3 + i for i in range(4)], o       # …and the far right is exactly the last four rows


def scroll_to(n_rows: int, n: int, offset: int) -> int:
    """The offset nearest ``offset`` whose scroll window shows row ``n``: the least the window can move to follow the
    highlight, rather than the page it would need if the row paged. A row no window shows at all leaves it at 0."""
    best = None
    for o in range(max(0, int(n_rows)) - 3):
        if n in scroll_window(n_rows, o)[0]:
            if best is None or abs(o - offset) < abs(best - offset):
                best = o
    return 0 if best is None else best


# --------------------------------------------------------------------------------------------- the layout table


class Layout:
    """What the keys look like for a board state at a moment in time, and what a press sends. Pure functions of the
    state, the clock, the page on show, the wipes still running and the switches waiting to be confirmed."""

    def plan(self, state: dict | None, t: float = 0.0, wipes: dict[int, float] | None = None, page: int = 0,
             pending: dict[str, float] | None = None, mode: str = "pages", offset: int = 0) -> dict[int, KeyFace]:
        faces = self._base(state, t, page, pending, mode, offset)
        for key, t0 in (wipes or {}).items():         # the key a switch just made visible: white, fading back
            face = faces.get(key)
            if face is not None:
                face.bg = blend(WHITE, face.bg, min(1.0, (t - t0) / WIPE_S))   # its text stays white: it re-emerges
        return faces

    def _base(self, state: dict | None, t: float, page: int = 0, pending: dict[str, float] | None = None,
              mode: str = "pages", offset: int = 0) -> dict[int, KeyFace]:
        """The faces as they would stand still, with the clock only deciding the breathing and the sweep."""
        if state is None:
            faces = {i: KeyFace() for i in range(KEY_COUNT)}
            faces[7] = KeyFace(lines=("no board",), dim=True)      # grey on black, in the centre
            return faces
        modal = state.get("modal")
        if modal == "picker":
            return self._picker(state)                             # a picker's five keys are its options: no paging
        if modal == "start":
            return self._start(state)                              # …and so are the Start screen's, with four keys more
        if modal == "confirm":
            return self._confirm()
        if modal == "input":
            return self._typing(state, t, page, pending, mode, offset)
        if modal == "spend":
            return self._spend(state)
        return self._normal(state, t, page, pending, mode, offset)

    def animating(self, state: dict | None, wipes: dict[int, float] | None = None, page: int = 0,
                  pending: dict[str, float] | None = None, mode: str = "pages", offset: int = 0,
                  animating: bool = True) -> bool:
        """Whether the next frame can differ from this one: a session on show waiting (it breathes) or busy (it
        sweeps), a switch on it waiting to be confirmed (it pulses), or a wipe still running. A board that is all
        idle and settling paints nothing until it next changes, and a row that isn't on show costs nothing. With the
        animations switched off in the settings file nothing ever moves on the clock, which is what `animating=False`
        says — the deck asks rather than assuming, so nothing on the tick's path still thinks it is moving."""
        if not animating:
            return False
        if wipes:
            return True
        here = set(self._page_keys(state or {}, page, mode, offset).values())   # the row positions on show
        rows = [r for i, r in enumerate((state or {}).get("rows") or []) if r and r.get("n", i + 1) in here]
        if any(r.get("key") in (pending or {}) for r in rows):
            return True
        return any(r.get("state") in ("waiting", "busy") for r in rows)

    def _row_of(self, state: dict, n: int) -> dict | None:
        """The row with position ``n``, as the state lists them."""
        return next((r for i, r in enumerate(state.get("rows") or []) if (r or {}).get("n", i + 1) == n), None)

    def _entries(self, state: dict | None, page: int, mode: str = "pages", offset: int = 0) -> list:
        """The five entries the top row shows: the whole page ``page`` of the rows, or the four-row window that
        starts at ``offset`` when the top row scrolls."""
        if mode == "scroll":
            return scroll_window(len((state or {}).get("rows") or []), offset)[0]
        all_pages = pages(len((state or {}).get("rows") or []))
        return all_pages[min(max(0, int(page)), len(all_pages) - 1)]

    def _entry(self, state: dict, page: int, key: int, mode: str = "pages", offset: int = 0):
        """What key ``key`` of the top row stands for: a row number, "prev", "next", or None."""
        return self._entries(state, page, mode, offset)[key]

    def _page_keys(self, state: dict, page: int, mode: str = "pages", offset: int = 0) -> dict[int, int]:
        """Which key shows which row: key index → 1-based row position, for the rows the state has — the page on
        show, or the scroll window on show. A row the state doesn't carry isn't on a key, and a key showing an arrow
        or nothing isn't either."""
        entries = self._entries(state, page, mode, offset)
        return {i: n for i, n in enumerate(entries)
                if isinstance(n, int) and self._row_of(state, n) is not None}

    def _normal(self, state: dict, t: float, page: int = 0, pending: dict[str, float] | None = None,
                mode: str = "pages", offset: int = 0) -> dict[int, KeyFace]:
        faces: dict[int, KeyFace] = {}
        entries = self._entries(state, page, mode, offset)
        n_rows = len(state.get("rows") or [])
        total = len(pages(n_rows))
        only = total == 1                                # one page: its empty keys are the slots that are free
        for i in range(5):
            entry = entries[i]
            if entry in ("next", "prev"):
                faces[i] = self._arrow("▶" if entry == "next" else "◀", mode, entries, offset, n_rows,
                                       min(max(0, int(page)) + 1, total), total)
                continue
            r = self._row_of(state, entry) if isinstance(entry, int) else None
            if r is None:
                # nothing on this key: on a single page, the number it would have been, in dark grey only; once the
                # rows are paged the keys belong to the rows and a spare one is plain black
                faces[i] = KeyFace(lines=(str(i + 1),), dim=True) if only else KeyFace()
                continue
            row_state = r.get("state")
            waiting = row_state == "waiting"
            dot = DOT.get(row_state, DOT["unknown"])
            own = blend(SLOT_BG[i % len(SLOT_BG)], WAIT_BG, breathe(t)) if waiting else SLOT_BG[i % len(SLOT_BG)]
            t0 = (pending or {}).get(r.get("key"))           # when the press went: the pulse starts there, at white
            faces[i] = KeyFace(bg=blend(WHITE, own, pulse(t - t0)) if t0 is not None else own,
                               lines=(str(entry), _title(r.get("title"))),
                               # white on white says nothing: while it thinks, the key's own colour does the text
                               ink=SLOT_BG[i % len(SLOT_BG)] if t0 is not None else None,
                               top_bar=bool(r.get("visible")), bottom_bar=bool(r.get("selected")),
                               dot=dot, border=WAIT_BG if waiting else None,
                               edge=(sweep_y(t, KEY_H), sweep_y(t, KEY_H) + SWEEP_H - 1, dot)
                               if row_state == "busy" else None)
        for i, face in enumerate(self._row2(state)):
            faces[ROW2 + i] = face
        faces[10] = KeyFace(bg=ARROW_BG, lines=("▲",))
        faces[11] = KeyFace(bg=ARROW_BG, lines=("▼",))
        faces[12] = KeyFace(bg=ACTION_BG, lines=("restart",))
        faces[13] = KeyFace(bg=ACTION_BG, lines=("new",))
        faces[14] = KeyFace(bg=END_BG, lines=("kill",))
        return faces

    def _row2(self, state: dict) -> list[KeyFace]:
        """The middle row: two panels that stay lit while they are up, the spend screen (lit while it is showing),
        where to send a session elsewhere, and detach. The lit face is the same key with a bar under it, so the row
        reads the same at a glance whichever way each thing is set."""
        panels = state.get("panels") or {}
        lit = [bool(panels.get("gpu")), bool(panels.get("pool")), state.get("modal") == "spend", False, False]
        return [KeyFace(bg=LIT_BG if on else (END_BG if cmd == "detach" else ACTION_BG), lines=(cmd,),
                        bottom_bar=bool(on))
                for cmd, on in zip(ROW2_CMDS, lit)]

    def _cancel_row2(self, faces: dict[int, KeyFace]) -> dict[int, KeyFace]:
        """A modal is up: the middle row's own commands are nobody's to run, so every one of its keys is a cancel."""
        for i in range(ROW2, ROW2 + COLS):
            faces[i] = KeyFace(bg=ACTION_BG, lines=("cancel",))
        return faces

    def _spend(self, state: dict) -> dict[int, KeyFace]:
        """The spend screen is up: it takes the whole pane and its own Esc closes it, so the only thing a key can do
        apart from closing it is the $ that opened it — key 7, still lit, and it goes back the way it came."""
        faces = {i: KeyFace() for i in range(ROW2)}
        self._cancel_row2(faces)
        faces[ROW2 + 2] = self._row2(state)[2]
        for i in range(10, KEY_COUNT):
            faces[i] = KeyFace(bg=ACTION_BG, lines=("cancel",))
        return faces

    def _picker(self, state: dict) -> dict[int, KeyFace]:
        faces: dict[int, KeyFace] = {}
        options = state.get("options") or []
        for i in range(5):
            opt = next((o for o in options if o.get("n") == i + 1), None)
            faces[i] = KeyFace(bg=OPTION_BG, lines=(str(i + 1), _title(opt.get("label")))) \
                if opt else KeyFace()
        self._cancel_row2(faces)
        for i in (10, 11, 12, 13, 14):
            faces[i] = KeyFace(bg=ACTION_BG, lines=("cancel",))
        return faces

    def _start(self, state: dict) -> dict[int, KeyFace]:
        """The Start screen (`s`): its profile keys along the top, the board's own commands blank in the middle —
        none of them is the board's to run under this screen — and the screen's own five along the bottom: ▲▼ over
        the sessions to resume, `select` to mark the one the cursor is on, `cancel`, and `enter` to resume them.

        The bottom row's two say what the screen would do: `select` carries the cursor row's title and wears the lit
        colour while that row is marked, and `enter` says how many it would bring back — or `nothing`, dim, when
        nothing has ended. A board whose state has no `start` field — an older one — leaves both saying only what
        they are, and presses still send the same commands."""
        faces: dict[int, KeyFace] = {}
        options = state.get("options") or []
        for i in range(5):
            opt = next((o for o in options if o.get("n") == i + 1), None)
            faces[i] = KeyFace(bg=OPTION_BG, lines=(str(i + 1), _title(opt.get("label")))) \
                if opt else KeyFace()
        for i in range(ROW2, 10):
            faces[i] = KeyFace()                       # nothing of the board's runs under this screen: no cancel
        start = state.get("start") if isinstance(state.get("start"), dict) else None
        recents = (start or {}).get("recents") or []
        cursor = (start or {}).get("cursor")
        row = recents[cursor] if isinstance(cursor, int) and 0 <= cursor < len(recents) else {}
        # lit by the cursor row's own mark — the row this key would mark — not by how many others are marked
        marked = bool(row.get("marked"))
        n_marked = (start or {}).get("marked") or sum(1 for r in recents if r.get("marked"))
        under = None if start is None else (f"resume {n_marked or 1}" if (n_marked or recents) else "nothing")
        faces[10] = KeyFace(bg=ARROW_BG, lines=("▲",))
        faces[11] = KeyFace(bg=ARROW_BG, lines=("▼",))
        faces[12] = KeyFace(bg=LIT_BG if marked else ACTION_BG, lines=("select",),
                            sub=(_title(row.get("title")) or None) if start else None)
        faces[13] = KeyFace(bg=ACTION_BG, lines=("cancel",))
        faces[14] = KeyFace(bg=ACTION_BG, lines=("enter",), sub=under,
                            dim=bool(start) and not recents)   # nothing to bring back: the key says so, in grey
        return faces

    def _confirm(self) -> dict[int, KeyFace]:
        faces = {i: KeyFace() for i in range(ROW2)}
        self._cancel_row2(faces)
        for i in (10, 11, 12, 13):
            faces[i] = KeyFace(bg=ACTION_BG, lines=("cancel",))
        faces[14] = KeyFace(bg=CONFIRM_BG, lines=("KILL", "✓"))
        return faces

    def _typing(self, state: dict, t: float, page: int = 0, pending: dict[str, float] | None = None,
                mode: str = "pages", offset: int = 0) -> dict[int, KeyFace]:
        """The board's text Input has focus (`/`, `m`, `a`, a directory prompt). The rows underneath are still doing
        what they do, so they keep moving; the bottom row becomes the box's own two keys and a reminder.

        `enter` accepts the box with whatever is in it and `cancel` drops it — the two keys the box answers to, where
        the text goes in is on the board, not here. The three keys between them say so, and the middle one of those
        carries the box itself when the state carries it: the prompt it's asking and the text typed so far, in the
        small size the slot titles use. A board that says nothing about either keeps the plain reminder instead."""
        faces = self._normal(state, t, page, pending, mode, offset)
        for i in range(ROW2, KEY_COUNT):
            faces[i] = KeyFace(bg=ACTION_BG, lines=(TYPE_HINT,))
        faces[10] = KeyFace(bg=ACTION_BG, lines=("cancel",))
        faces[14] = KeyFace(bg=ACTION_BG, lines=("enter",))
        prompt, text = state.get("input_prompt"), state.get("input_text")
        if isinstance(prompt, str) or isinstance(text, str):
            faces[12] = KeyFace(bg=ACTION_BG, lines=(_title(prompt), _title(text)), small=True)
        return faces

    def _arrow(self, glyph: str, mode: str, entries: list, offset: int, n_rows: int,
               page: int, total: int) -> KeyFace:
        """An arrow key: the arrow big, and under it where in the book this is. Paging says which page of how many —
        `▶` says where it goes, `◀` where it came from, which is the page the operator is looking at either way.
        Scrolling has no pages to count, so it says which rows are under the keys: `5–7/9`."""
        if mode == "scroll":
            shown = [e for e in entries if isinstance(e, int)]
            where = f"{shown[0]}–{shown[-1]}/{n_rows}" if shown else f"–/{n_rows}"
        else:
            where = f"{page}/{total}"
        return KeyFace(bg=ARROW_BG, lines=(glyph, where))

    def on_press(self, state: dict | None, key: int, page: int = 0, mode: str = "pages",
                 offset: int = 0) -> list[dict]:
        """The requests to send the moment key ``key`` goes down. Nothing at all for a blank key, or while the
        board is unreachable: with no state there is nothing to act on and no one to send it to.

        A top-row key asks for the row it is showing, which past the first page is not the key's own number. An arrow
        is the deck's own business: it answers a `deck` request with no `cmd` — `page` in pages mode, `scroll` in
        scroll mode — which `DeckApp._send` turns a window over without the board ever hearing about it.

        While the board's text box has focus only its two keys answer: `enter` accepts the box (`submit`, which the
        board takes as Enter — it accepts the Input with the text it holds) and `cancel` drops it."""
        if key not in range(KEY_COUNT) or state is None:
            return []
        modal = state.get("modal")
        if modal == "picker":
            if key < 5:
                return [{"cmd": "pick", "n": key + 1}]
            return [{"cmd": "cancel"}] if key >= ROW2 else []
        if modal == "start":
            # the screen's own four keys, and nothing else: the middle row's commands are the board's, and the board
            # is not asking them under this screen
            if key < 5:
                return [{"cmd": "pick", "n": key + 1}]
            if key in (10, 11):
                return [{"cmd": "move", "delta": -1 if key == 10 else 1}]
            return [{"cmd": {12: "mark", 13: "cancel", 14: "submit"}[key]}] if key >= 12 else []
        if modal == "confirm":
            if key == 14:
                return [{"cmd": "confirm", "yes": True}]
            return [{"cmd": "cancel"}] if key >= ROW2 else []
        if modal == "input":
            # the text goes in on the board; these are the box's own two keys, and nothing else is anyone's to run
            if key == 14:
                return [{"cmd": "submit"}]
            if key == 10:
                return [{"cmd": "cancel"}]
            return []
        if modal == "spend":
            # the spend screen has the whole pane and its own Esc: key 7 is the $ that opened it, and everything
            # else on the deck is that Esc
            if key == ROW2 + 2:
                return [{"cmd": "spend"}]
            return [] if key < ROW2 else [{"cmd": "cancel"}]
        if key < 5:
            entry = self._entry(state, page, key, mode, offset)
            if entry == "next":
                return [{"deck": "scroll" if mode == "scroll" else "page", "delta": 1}]
            if entry == "prev":
                return [{"deck": "scroll" if mode == "scroll" else "page", "delta": -1}]
            return [{"cmd": "slot", "n": entry}] if isinstance(entry, int) else []
        if ROW2 <= key < ROW2 + COLS:
            return [{"cmd": ROW2_CMDS[key - ROW2]}]
        if key == 10:
            return [{"cmd": "move", "delta": -1}]
        if key == 11:
            return [{"cmd": "move", "delta": 1}]
        if key == 12:
            return [{"cmd": "start"}]
        if key == 13:
            return [{"cmd": "new"}]
        if key == 14:
            return [{"cmd": "end"}]
        return []

    def on_release(self, state: dict | None, key: int, held_ms: float) -> list[dict]:
        """The requests to send when key ``key`` comes up after ``held_ms`` milliseconds. Only a ▲▼ held past
        HOLD_MS does anything: it opens the row the highlight is on now — the press already moved it, so this is
        where the press left it. Read off the state as it stands at release, which may be later than the press."""
        if key not in (10, 11) or state is None or held_ms < HOLD_MS:
            return []
        if state.get("modal"):
            return []
        for i, r in enumerate(state.get("rows") or []):
            if r.get("selected"):
                return [{"cmd": "slot", "n": r.get("n", i + 1)}]     # the row's own position, whatever page shows it
        return []


def _title(text) -> str:
    """One board title as key text: on one line, shortened with an ellipsis when it won't fit."""
    s = " ".join(str(text or "").split())
    return s if len(s) <= 24 else s[:23] + "…"


# ------------------------------------------------------------------------------------------------------ drawing


def render(face: KeyFace, deck) -> bytes:
    """A KeyFace as the bytes one key wants — JPEG for an MK.2, after the device's own flip."""
    if getattr(deck, "no_render", False):                       # --no-device: the face in words, not in pixels
        return describe(face).encode()
    from StreamDeck.ImageHelpers import PILHelper
    from PIL import ImageDraw
    fmt = deck.key_image_format()
    w, h = fmt["size"]
    img = PILHelper.create_image(deck, background=_hex(face.bg))
    d = ImageDraw.Draw(img)
    if face.border:
        d.rectangle((1, 1, w - 2, h - 2), outline=_hex(face.border), width=2)
    if face.edge:                                   # the busy sweep, along the key's left edge, under the text.
        y0, y1, colour = face.edge                  # the top and bottom bars may cross its ends: that's fine
        d.rectangle((0, y0, SWEEP_W, y1), fill=_hex(colour))
    if face.top_bar:
        d.rectangle((0, 0, w - 1, 3), fill=_hex(WHITE))
    if face.bottom_bar:
        d.rectangle((0, h - 4, w - 1, h - 1), fill=_hex(WHITE))
    if face.dot:
        # upper right, level with the number, ringed in white so it reads on every palette colour
        d.ellipse((w - 19, 7, w - 9, 17), fill=_hex(face.dot), outline=_hex(WHITE), width=1)   # 9 px in: clear of the key bevel
    ink = _hex(face.ink or (DIM_GREY if face.dim else WHITE))
    y = 6
    text = list(face.lines) + ([face.sub] if face.sub is not None else [])
    n_lines = len(face.lines)
    for i, line in enumerate(text):
        # past the key's own lines there's only the `sub`, and that is board text at whatever it hangs under
        size = 11 if (face.small or i >= n_lines) else label_size(line, i, w)
        d.multiline_text((5, y), "\n".join(_wrap(line, w, size)), fill=ink, font=_font(size, bold=size == 26),
                         spacing=2)
        y += size + 7
    return PILHelper.to_native_format(deck, img)


def describe(face: KeyFace) -> str:
    """A face in words — what ``ctx deck --no-device`` prints instead of painting. The background carries the
    animation for anyone watching by eye; the edge says where a sweep bar is."""
    marks = "".join("T" if face.top_bar else "") + ("B" if face.bottom_bar else "")
    return (f"{face.bg} {'|'.join(' '.join(line.split()) for line in face.lines)!r}"
            + (f" sub={face.sub!r}" if face.sub else "")
            + (f" edge={face.edge}" if face.edge else "")
            + (f" dot={face.dot}" if face.dot else "") + (f" border={face.border}" if face.border else "")
            + (f" ink={face.ink}" if face.ink else "")
            + (f" bars={marks}" if marks else "") + (" dim" if face.dim else ""))


def label_size(line: str, index: int, width: int = KEY_W) -> int:
    """How big one line of a key's text is drawn: 26 px for a slot number or an arrow (a row number past nine is two
    digits, which fits at that size), 15 px for the key's own label, 11 px for board text under it — which is also
    what the page count under an arrow gets. A label too long for the key shrinks rather than wraps ("restart",
    "exchange"), down to 10 px at the least, so a whole word always comes out whole — which is also what a test can
    ask without a device: the size it picks is the biggest one whose measured text still fits the key."""
    if index == 0 and len(line.strip()) <= 2:
        return 26
    if index or not line.strip():
        # …and a line with nothing on it: it can only move the next one down, so let it move it as little as possible
        return 11
    first = line.split("\n")[0]                        # what `_wrap` draws as its own line; Pillow can't measure two
    size = 15
    while size > 10 and _font(size).getlength(first) > width - 10:
        size -= 1
    return size


def _font(size: int, bold: bool = False):
    """DejaVu where it's installed, Pillow's own bitmap font where it isn't."""
    from PIL import ImageFont
    name = FONT_BOLD if bold else FONT_REG
    got = _FONTS.get((name, size))
    if got is not None:
        return got
    try:
        f = ImageFont.truetype(name, size)
    except OSError:
        try:
            f = ImageFont.truetype(f"/usr/share/fonts/truetype/dejavu/{name}", size)
        except OSError:
            f = ImageFont.load_default()
    _FONTS[(name, size)] = f
    return f


_FONTS: dict[tuple[str, int], object] = {}


def _wrap(line: str, width: int, size: int) -> list[str]:
    """A key's text in at most two lines: wrapped at whole words, the second one ended with an ellipsis."""
    if "\n" in line:
        return [p.strip() for p in line.split("\n") if p.strip()][:2]
    limit = max(4, int((width - 10) / (0.55 * size)))     # DejaVu averages ~0.55 em per glyph
    if len(line) <= limit:
        return [line]
    out, cur = [], ""
    for word in line.split():
        cand = f"{cur} {word}".strip()
        if len(cand) > limit and cur:
            out.append(cur)
            cur = word
        else:
            cur = cand
        if len(out) == 2:
            break
    if cur and len(out) < 2:
        out.append(cur)
    out = out[:2]
    if out and len(out[-1]) > limit:
        out[-1] = out[-1][:limit - 1] + "…"
    return out


# ------------------------------------------------------------------------------------------------ board client


class BoardClient:
    """NDJSON over the board's unix socket: requests answered in order, state pushed to a callback, and a reconnect
    loop of its own. One connection at a time; ``request`` waits for the reply carrying its own id."""

    def __init__(self, path, on_state=None, loop=None, log=None):
        self.path = str(path)
        self.on_state = on_state
        self.log = log or (lambda msg: None)
        self.connected = False
        self.state: dict | None = None
        self._loop = loop
        self._reader = None
        self._writer = None
        self._run: asyncio.Task | None = None
        self._next_id = 0
        self._pending: dict[str, asyncio.Future] = {}
        self._lock: asyncio.Lock | None = None
        self._last_fail: str | None = None

    def connect(self, loop=None) -> None:
        """Start (or restart) the connection: keep it up, and feed ``on_state`` from it."""
        self._loop = loop or self._loop or asyncio.get_running_loop()
        self._lock = asyncio.Lock()
        if self._run is None or self._run.done():
            self._run = self._loop.create_task(self._keep_up())

    async def stop(self) -> None:
        task, self._run = self._run, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:                      # pragma: no cover - that's the expected way out
                pass
        self._close()

    def request(self, cmd: str, **fields) -> dict:
        """Send one command from any thread and wait for its reply. ``ok: false`` is a reply, not an exception."""
        return asyncio.run_coroutine_threadsafe(self.arequest(cmd, **fields), self._loop).result(timeout=10)

    async def arequest(self, cmd: str, **fields) -> dict:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._writer is None:
                return {"ok": False, "error": "not connected"}
            self._next_id += 1
            rid = str(self._next_id)
            fut = (self._loop or asyncio.get_running_loop()).create_future()
            self._pending[rid] = fut
            try:
                self._writer.write((json.dumps({"cmd": cmd, "id": rid, **fields}) + "\n").encode())
                await self._writer.drain()
            except asyncio.CancelledError:
                self._pending.pop(rid, None)
                raise
            except Exception as e:                              # noqa: BLE001 - a dead socket is a failed request
                self._pending.pop(rid, None)
                self.log(f"send {cmd}: {e}")
                return {"ok": False, "error": "send failed"}
            try:
                return await fut
            except asyncio.CancelledError:
                self._pending.pop(rid, None)
                raise
            except Exception as e:                              # noqa: BLE001 - report it, stay connected
                return {"ok": False, "error": str(e)}

    def subscribe(self, callback, loop=None) -> None:
        """Call ``callback(state)`` for every state the board pushes (and for the one ``hello`` answers with)."""
        self.on_state = callback
        if loop is not None:
            self._loop = loop

    async def _keep_up(self) -> None:
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(self.path)
            except asyncio.CancelledError:
                raise
            except OSError as e:
                self._fail(f"no board at {self.path}: {e.strerror or e} (retrying every {RECONNECT_S:.0f} s)")
                await asyncio.sleep(RECONNECT_S)
                continue
            self._reader, self._writer = reader, writer
            self.connected = True
            self._last_fail = None                              # a later failure is news again
            self.log(f"connected to the board at {self.path}")
            reading = self._loop.create_task(self._pump(reader))   # replies arrive only while this runs
            try:
                hello = await self.arequest("hello")               # the table as it stands, and the proto on the wire
                if isinstance(hello.get("state"), dict):
                    self._apply(hello["state"])
                if hello.get("proto") not in (None, 1):
                    self.log(f"the board speaks proto {hello['proto']}, this deck speaks 1")
                await self.arequest("subscribe")                   # from here the board pushes what changes
                await reading
                self._fail("the board closed the connection")
            except asyncio.CancelledError:
                reading.cancel()
                self._fail("stopped")
                raise
            except Exception as e:                              # noqa: BLE001 - any failure of the link is a disconnect
                self._fail(f"the board closed the connection: {e}")
            finally:
                if not reading.done():
                    reading.cancel()
                self._close()
            await asyncio.sleep(RECONNECT_S)                    # and don't go looking again before then

    async def _pump(self, reader) -> None:
        while True:
            line = await reader.readline()
            if not line:
                return                                          # the board closed the connection
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            rid = msg.get("id")
            if rid is not None and str(rid) in self._pending:
                fut = self._pending.pop(str(rid))
                if not fut.done():
                    fut.set_result(msg)
            if msg.get("event") == "state" and isinstance(msg.get("state"), dict):
                self._apply(msg["state"])

    def _apply(self, state: dict | None) -> None:
        self.state = state
        if self.on_state is None:
            return
        try:
            self.on_state(state)
        except Exception as e:                                  # noqa: BLE001 - a bad callback must not kill the link
            self.log(f"state callback: {e}")

    def _fail(self, why: str) -> None:
        was, self.connected = self.connected, False
        if why != self._last_fail:                              # the same reason every 2 s isn't worth printing twice
            self._last_fail = why
            self.log(why)
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result({"ok": False, "error": "not connected"})
        self._pending.clear()
        if was:
            self._apply(None)                                   # with no board there is no table to show

    def _close(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        self.connected = False
        if writer is not None:
            try:
                writer.close()
            except Exception:                                   # noqa: BLE001 - it is already gone, which is the point
                pass


# ------------------------------------------------------------------------------------------------------- app


class FakeDeck:
    """A deck that isn't one, for ``ctx deck --no-device`` and for tests: remembers what it was told to draw."""

    def __init__(self, count: int = KEY_COUNT, layout: tuple[int, int] = (ROWS, COLS), log=None):
        self._count, self._layout = count, layout
        self.no_render = False                                  # True prints faces instead of drawing them
        self.images: dict[int, bytes] = {}
        self.paints = 0
        self.brightness: int | None = None
        self.reset_called = False
        self.opened = self.closed = False
        self.cb = None
        self.deck_type_str = "Stream Deck Original (fake)"
        self.log = log or (lambda line: None)

    def key_count(self) -> int:
        return self._count

    def key_layout(self) -> tuple[int, int]:
        return self._layout

    def key_image_format(self) -> dict:
        return {"size": (72, 72), "format": "JPEG", "flip": (True, True), "rotation": 0}

    def set_key_image(self, key: int, image: bytes) -> None:
        self.images[key] = image
        self.paints += 1
        if self.no_render:
            self.log(f"key {key}: {image.decode(errors='replace')}")

    def set_key_callback(self, callback) -> None:
        self.cb = callback

    def set_brightness(self, percent: int) -> None:
        self.brightness = percent

    def reset(self) -> None:
        self.reset_called = True
        self.images.clear()

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def deck_type(self) -> str:
        return self.deck_type_str


class DeckApp:
    """The device, the board, and the redraw that keeps one showing the other."""

    def __init__(self, deck, board, layout: Layout | None = None, log=print, fps: float = FPS,
                 heartbeat_s: float = 0.0, echo=print, clock=time.monotonic, settings_path=None,
                 brightness_override: int | None = None):
        self.deck = deck
        self.board = board
        self.layout = layout or Layout()
        self.log = log
        self.echo = echo                                        # the running commentary, not the problems
        self.fps = fps
        self.heartbeat_s = heartbeat_s
        self.state: dict | None = None
        self.faces: dict[int, KeyFace] = {}
        self.page = 0                               # which page of the rows the top row is showing
        self.mode = "pages"                         # …or the other way that row can move: "pages" / "scroll"
        self.offset = 0                             # …scroll's: 0-based index of the first row the window shows
        # (both come from the settings file a few lines below: which one the top row does is the file's business)
        self.settings_path = Path(settings_path) if settings_path else SETTINGS_FILE
        self.brightness_override = brightness_override          # an explicit --brightness beats the file, for this run
        self.settings = load_settings(self.settings_path)
        self.mode = self.settings["top_row"]                  # the file's, before the first state has been painted
        self.anim_on = bool(self.settings["animations"])        # False: static faces, the state still updating
        self._checked_tick = 0                        # the animation tick counts itself, so the file is read 1 s in 20
        self._last_mtime: float | None = None
        self._lit: bool | None = None         # what the device was last told for its lights: None, never yet
        self._settings_error: str | None = None       # the same unreadable file every second isn't worth printing twice
        self.wipes: dict[str, float] = {}           # session key → when its fade started; dropped when it's done
        self.pending: dict[str, float] = {}         # …and the switch the board hasn't confirmed yet, by key
        self.loop: asyncio.AbstractEventLoop | None = None
        self._timers: list[asyncio.Task] = []
        self._last_paint = 0.0
        self._down: dict[int, float] = {}       # when each key went down, so a release can be timed
        self.clock = clock                                      # the animation's clock, and how a key's hold
        #                                                       is timed: a test may hand its own
        self._last_visible: str | None = None   # the visible_key of the state before this one: a switch is off it
        self._had_state = False                 # …and whether there was one at all: the first state is not a switch
        self._paint_times: list[float] = []     # when each repaint went out, so the heartbeat can count the last 20 s

    def start(self, loop=None) -> None:
        """Wire the device up and paint what the board says now."""
        self.loop = loop or asyncio.get_running_loop()
        try:
            self.deck.set_key_callback(self._key)
        except Exception as e:                                  # noqa: BLE001 - keys that don't report are the worst case
            self.log(f"key callback: {e}")
        self.board.subscribe(self._state, loop=self.loop)
        if self.fps:                                            # one tick drives every animation
            self._timers.append(self.loop.create_task(self._animate()))
        if self.heartbeat_s:                                    # at least one line every 30 s, however quiet it is
            self._timers.append(self.loop.create_task(self._heartbeat()))
        self.refresh(self.board.state)
        self._apply_settings(self.settings, first=True)         # the file's word on how this deck behaves

    async def _heartbeat(self) -> None:
        """One line every ``heartbeat_s`` saying the deck is alive — and a redraw in passing, in case a push was
        ever missed. The face cache makes that redraw cost nothing when nothing changed."""
        since = time.monotonic()
        while True:
            await asyncio.sleep(self.heartbeat_s)
            painted = self.refresh(self.state)
            board = "the board" if getattr(self.board, "connected", True) else "no board yet"
            now = time.monotonic()
            self._paint_times = [t for t in self._paint_times if now - t <= HEARTBEAT_S]
            self.echo(f"[deck] {now - since:.0f}s up: {board}, {len(self.faces)} keys, {painted} repainted "
                      f"({self._last_paint:.0f} ms), {len(self._paint_times)} in the last {HEARTBEAT_S:.0f} s, "
                      f"{self._anim_count()} keys animating")

    async def stop(self) -> None:
        timers, self._timers = self._timers, []
        for task in timers:
            task.cancel()
        for task in timers:
            try:
                await task
            except asyncio.CancelledError:                      # pragma: no cover - the expected way out
                pass
            except Exception as e:                              # noqa: BLE001 - closing down: say it, carry on
                self.log(f"timer: {e}")
        try:
            self.deck.reset()
        except Exception as e:                                  # noqa: BLE001 - nothing left to do about the deck
            self.log(f"reset: {e}")
        try:
            self.deck.close()
        except Exception as e:                                  # noqa: BLE001 - ditto
            self.log(f"close: {e}")

    def refresh(self, state: dict | None) -> int:
        """Take a state the board sent: follow the highlight if it moved, start the fade a confirmed switch calls
        for, then paint every key whose face changed. Returns how many were painted."""
        previous = self.state
        self.state = state
        self._follow(state, previous)
        self._wipes(state)
        return self._paint(self.clock())

    def _pages(self) -> int:
        """How many pages the rows on show need."""
        return len(pages(len((self.state or {}).get("rows") or [])))

    def _follow(self, state: dict | None, previous: dict | None) -> None:
        """Keep the top row somewhere that still exists, and follow the highlight: when the board has moved it to a
        row this window doesn't show, go and show that one — which is what carries ▲▼ walking off the edge along.
        Only a state that *moved* it counts: a window the operator moved by hand stays put until it does. How far it
        has to move to cover the row is the minimum: the page the row is on, or the least the scroll window can
        shift, which is the one that puts the row on key 3."""
        n_rows = len((state or {}).get("rows") or [])
        if self.mode == "scroll":
            self.offset = min(self.offset, max(0, n_rows - 4))
        else:
            self.page = min(self.page, self._pages() - 1)
        if not state or previous is None or state.get("selected_key") == previous.get("selected_key"):
            return
        n = next((r.get("n", i + 1) for i, r in enumerate(state.get("rows") or [])
                  if r and r.get("key") == state.get("selected_key")), None)
        if n is None:
            return
        if self.mode == "scroll":
            entries, self.offset = scroll_window(n_rows, self.offset)
            if n not in entries:
                self.offset = scroll_to(n_rows, n, self.offset)   # the least that brings row n into the window
            return
        for i, entries in enumerate(pages(n_rows)):
            if n in entries:
                self.page = i
                return

    def _wipes(self, state: dict | None) -> None:
        """A state whose `visible_key` isn't the one before it is a switch, and the row that came out fades in. A
        switch this deck asked for is confirmed by that same state: its key stops pulsing and starts fading, so the
        pulse lasts exactly as long as the board took. A switch that didn't come from this deck gets the plain fade
        with no pulse before it. The first state says nothing moved — there is nothing before it to have moved
        from."""
        visible = (state or {}).get("visible_key")
        switched = self._had_state and visible is not None and visible != self._last_visible
        if visible is not None and visible in self.pending:
            self.pending.pop(visible)
            self.wipes[visible] = self.clock()
        elif switched:
            self.wipes[visible] = self.clock()
        self._last_visible = visible
        self._had_state = True

    def _paint(self, t: float) -> int:
        """Plan the board as it stands at ``t`` and send the keys whose face differs from what they show. With the
        animations switched off in the settings file the clock is held at 0 and nothing is wiped or pulsed, so every
        face is the still one — a switch is still waited on and still tracked, it just isn't drawn as white."""
        started = time.monotonic()
        if self.anim_on:
            self.wipes = {key: t0 for key, t0 in self.wipes.items() if (t - t0) / WIPE_S < 1.0}
        # a switch the board never answered — refused, or lost: the key goes back the way it came, quietly
        self.pending = {key: t0 for key, t0 in self.pending.items() if t - t0 < PENDING_S}
        plan = self.layout.plan(self.state, t if self.anim_on else 0.0, self._key_wipes(), self.page,
                                self.pending if self.anim_on else None, self.mode, self.offset)
        drawn = 0
        for key in sorted(plan):
            face = plan[key]
            if self.faces.get(key) == face:
                continue
            try:
                self.deck.set_key_image(key, render(face, self.deck))
            except Exception as e:                              # noqa: BLE001 - one key that fails must not stop the rest
                self.log(f"key {key}: {e}")
                continue
            drawn += 1
            self.faces[key] = face
        now = time.monotonic()
        self._paint_times.extend([now] * drawn)
        self._last_paint = (now - started) * 1000
        return drawn

    def _page_rows(self) -> list[dict]:
        """The rows the top row on show actually draws, in key order."""
        here = set(self.layout._page_keys(self.state or {}, self.page, self.mode, self.offset).values())
        return [r for i, r in enumerate((self.state or {}).get("rows") or []) if r and r.get("n", i + 1) in here]

    def _key_wipes(self) -> dict[int, float]:
        """The fades as `plan` wants them: by the key index that shows the row right now. A row the top row isn't
        showing has no key here to fade, which is the point — moving the window must never put a fade on the wrong
        key. Nothing fades at all while the animations are off."""
        if not self.anim_on:
            return {}
        here = self.layout._page_keys(self.state or {}, self.page, self.mode, self.offset)  # key index → row
        at = {n: key for key, n in here.items()}
        position = {(r or {}).get("key"): (r or {}).get("n", i + 1)
                    for i, r in enumerate((self.state or {}).get("rows") or [])}
        return {at[position[key]]: t0 for key, t0 in self.wipes.items() if position.get(key) in at}

    def _anim_count(self) -> int:
        """How many keys the animations are moving right now, on the window on show: the rows that breathe or sweep
        or are waiting on the board, plus any fading in."""
        if not self.anim_on:
            return 0
        live = sum(1 for r in self._page_rows()
                   if r.get("state") in ("waiting", "busy") or r.get("key") in self.pending)
        return live + len(self._key_wipes())

    async def _animate(self) -> None:
        """The animation's own clock. Nothing else moves on it: when no session on the window is waiting or busy or
        thinking, and nothing is fading, the tick looks, finds nothing to do, and costs nothing."""
        while True:
            await asyncio.sleep(1.0 / self.fps)
            self._tick()

    def _tick(self) -> int:
        """One frame, if there is one to be had. Returns how many keys it sent. Every ``SETTINGS_TICKS`` ticks —
        once a second on the 20 fps clock — it also looks at the settings file, which is how a change made on the
        board's `D` screen reaches a deck that is already running."""
        self._checked_tick += 1
        if self._checked_tick % SETTINGS_TICKS == 0:
            self._check_settings()
        t = self.clock()
        if not self.layout.animating(self.state, self._key_wipes(), self.page, self.pending, self.mode, self.offset,
                                     self.anim_on):
            return 0                                            # the tick still runs, and still costs nothing
        return self._paint(t)

    def _state(self, state: dict) -> None:
        self.refresh(state)

    def _key(self, deck, key: int, pressed: bool) -> None:
        """The device's own callback, on its own thread: ask the layout, send what it says. A press goes at once; a
        release only matters for a key this remembers going down, and only for as long as it was held."""
        now = self.clock()
        state = self.board.state
        if pressed:
            self._down[key] = now
            reqs = self.layout.on_press(state, key, self.page, self.mode, self.offset)
            self._armed(key, state, reqs, now)
        else:
            t0 = self._down.pop(key, None)
            if t0 is None:
                return                                          # a release with no press behind it: nothing was held
            reqs = self.layout.on_release(state, key, (now - t0) * 1000)
        self._send(key, reqs)

    def _armed(self, key: int, state: dict | None, reqs: list[dict], now: float) -> None:
        """A key that asked the board to switch to one of its rows says so now, before the board has had a turn: the
        key goes white and pulses until the state arrives with that row in the pane (or until it stops being worth
        waiting for). Painting here, rather than waiting on the reply, is what makes the key answer the finger."""
        for req in reqs:
            if req.get("cmd") != "slot":
                continue
            r = self.layout._row_of(state or {}, req.get("n"))
            # a row already in the pane has nothing to confirm: it would pulse until the timeout gave up on it
            if r and r.get("key") and r["key"] != (state or {}).get("visible_key"):
                self.pending[r["key"]] = now
                self._paint(now)

    def _send(self, key: int, reqs: list[dict]) -> None:
        """Get the layout's requests to the board, from whichever thread the device callback arrived on. A request
        with no `cmd` is the deck's own — the top row moving over the rows — which the board is never told about."""
        for req in reqs:
            if "cmd" not in req:
                self._local(req)
                continue
            cmd, fields = req["cmd"], {k: v for k, v in req.items() if k != "cmd"}
            try:
                reply = _request(self.board, cmd, fields, self.loop, self.log)
            except Exception as e:                              # noqa: BLE001 - a key press is not worth a traceback
                reply = {"ok": False, "error": str(e)}
            if reply is not None and not reply.get("ok"):
                self.log(f"key {key}: {reply.get('error') or 'the board said no'}")
                if cmd == "slot" and self._refused(key, req):
                    self._paint(self.clock())

    def _refused(self, key: int, req: dict) -> bool:
        """A `slot` the board said no to: stop that row pulsing, since it is never going to come."""
        r = self.layout._row_of(self.state or {}, req.get("n"))
        if r and r.get("key") in self.pending:
            self.pending.pop(r["key"], None)
            return True
        return False

    def _local(self, req: dict) -> None:
        """A request the deck answers itself. `page` turns the top row over to the next or previous page of rows;
        `scroll` slides the window along by that one row. Either way it repaints and the board sees nothing, because
        there is nothing for it to have done."""
        kind = req.get("deck")
        if kind not in ("page", "scroll"):
            return
        delta = int(req.get("delta") or 0)
        n_rows = len((self.state or {}).get("rows") or [])
        if kind == "scroll" or self.mode == "scroll":
            self.offset = scroll_window(n_rows, scroll_step(self.offset, delta))[1]
        else:
            self.page = min(max(0, self.page + delta), self._pages() - 1)
        self._paint(self.clock())

    def _check_settings(self) -> None:
        """One look at the settings file: if its mtime has moved, read it and apply what changed. A file that won't
        read keeps the deck on what it already had, and says so once rather than every second."""
        try:
            mtime = self.settings_path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime is not None and mtime == self._last_mtime:
            return                                              # 20 ticks a second, and this is 1 in 20 of them
        self._last_mtime = mtime
        try:
            got = load_settings(self.settings_path)
        except Exception as e:                                  # noqa: BLE001 - the deck carries on with what it had
            if str(e) != self._settings_error:
                self._settings_error = str(e)
                self.log(f"deck settings: {e}")
            return
        self._settings_error = None
        self._apply_settings(got)

    def _apply_settings(self, got: dict, first: bool = False) -> None:
        """One look at the settings file, against what this deck is doing: which way the top row moves, how bright
        the keys are, and whether the faces animate. One line each, so the log says what the deck did about it. The
        first look is the deck starting up rather than anything changing, so it says nothing and repaints nothing —
        `__init__` read the file before the first key was laid out."""
        was_mode, was_anim = self.mode, self.anim_on
        self.settings = got
        self.anim_on = bool(got["animations"])
        if got["top_row"] != was_mode:
            self.mode = got["top_row"]
            self._show_highlighted()
            self.log(f"deck settings: the top row {'scrolls' if self.mode == 'scroll' else 'pages'}")
        want = self.brightness_override if self.brightness_override is not None else got["brightness"]
        if self._lit != want:                                 # once at start, then whenever the number moves
            self._set_brightness(want, say=not first)
        if self.anim_on != was_anim:
            self.log(f"deck settings: animations {'on' if self.anim_on else 'off'}")
        if not first:
            self._paint(self.clock())

    def _show_highlighted(self) -> None:
        """Where the top row starts after the mode changes: the window that shows the highlighted row, which is the
        one the operator is looking at. With no state, or no highlight, the start of the rows."""
        n_rows = len((self.state or {}).get("rows") or [])
        n = next((r.get("n", i + 1) for i, r in enumerate((self.state or {}).get("rows") or [])
                  if r and r.get("key") == self.state.get("selected_key")), None) if self.state else None
        if self.mode == "scroll":
            self.offset = 0 if n is None else scroll_to(n_rows, n, self.offset)
            return
        book = pages(n_rows)
        self.page = 0 if n is None else next((i for i, entries in enumerate(book) if n in entries), 0)

    def _set_brightness(self, percent: int, say: bool = True) -> None:
        """The key lights, and a line saying so when it was a change rather than the deck lighting up in the first
        place. The number is remembered even if the device refused it, so the deck doesn't go back to the device
        every second with the same answer."""
        try:
            self.deck.set_brightness(percent)
        except Exception as e:                                  # noqa: BLE001 - a deck without controllable lights
            self.log(f"brightness: {e}")
        self._lit = percent
        if say:
            self.log(f"deck settings: brightness {percent}")

    def press(self, key: int) -> None:
        """A key going down, for a hand-driven fake deck."""
        self._key(self.deck, key, True)

    def release(self, key: int) -> None:
        """The same key coming back up, for a hand-driven fake deck."""
        self._key(self.deck, key, False)


def _request(board, cmd: str | None, fields: dict, loop, log=print) -> dict | None:
    """One request to the board, from whatever thread the device callback arrived on.

    The library calls that callback on its own USB thread, so the usual way is to hand the coroutine to the loop and
    wait for it. A callback that arrives *on* the loop (a fake deck, or a library that changes hands) must not wait
    for an answer only this thread could deliver: that one is fired off, and a failure it finds is logged when it
    lands. Returns the reply, or None when it went off unwaited."""
    areq = getattr(board, "arequest", None)
    if areq is None:
        return board.request(cmd, **fields)
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if loop is None or running is loop:
        task = (running or loop).create_task(areq(cmd, **fields))

        def _said(fut: asyncio.Task) -> None:
            if not fut.cancelled() and fut.exception():
                log(f"{cmd}: {fut.exception()}")

        task.add_done_callback(_said)
        return None
    return asyncio.run_coroutine_threadsafe(areq(cmd, **fields), loop).result(timeout=10)


def open_deck(log=print):
    """The first Stream Deck on the USB bus, opened."""
    from StreamDeck.DeviceManager import DeviceManager
    decks = DeviceManager().enumerate()
    if not decks:
        raise RuntimeError("no Stream Deck found")
    deck = decks[0]
    deck.open()
    log(f"{deck.deck_type()} · {deck.key_count()} keys {'×'.join(str(d) for d in deck.key_layout())}")
    return deck


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ctx deck", description="A Stream Deck as buttons for the board.")
    ap.add_argument("--brightness", type=int, default=None,
                    help="key light brightness, 0-100 — said here, it wins over the settings file for this run "
                         "(otherwise the file's, which is 60 until the board's D screen says otherwise)")
    ap.add_argument("--sock", default=None, help="the board's control socket "
                                                 "(default ~/.local/state/ctxswitch/board.sock)")
    ap.add_argument("--no-device", action="store_true", help="no deck attached: print the faces instead of drawing them")
    a = ap.parse_args(argv)
    if not a.no_device:
        try:
            import StreamDeck                                   # noqa: F401 - the whole point of the extra
            import PIL                                          # noqa: F401
        except ImportError:
            print("the Stream Deck library isn't installed: install with: uv pip install -e '.[deck]'")
            return 2
    path = sock_path(a.sock)
    try:
        deck = FakeDeck(log=print) if a.no_device else open_deck()
    except RuntimeError as e:
        print(f"{e}"); return 1
    if a.no_device:
        deck.no_render = True                                   # the faces in words, since there's nothing to draw them on
    # the lights are the settings file's unless they were said on the command line, which wins for this run; the
    # deck's own first look at the file is what puts them on the keys, so it says nothing about it
    board = BoardClient(path, log=lambda m: print(m, file=sys.stderr))
    app = DeckApp(deck, board, log=lambda m: print(m, file=sys.stderr), heartbeat_s=HEARTBEAT_S,
                  echo=lambda m: print(m, file=sys.stderr, flush=True), brightness_override=a.brightness)
    loop = asyncio.new_event_loop()
    closing: list[asyncio.Task] = []

    def _stop() -> None:
        if closing:
            return
        closing.append(loop.create_task(_shutdown(app)))        # the last thing it does is stop the loop

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
            continue
        except (NotImplementedError, RuntimeError, ValueError):     # Windows, or not the main thread
            pass
        try:
            signal.signal(sig, lambda *_f: loop.call_soon_threadsafe(_stop))
        except (ValueError, OSError):                               # pragma: no cover - no signals for this process
            print("can't catch Ctrl-C here: close this terminal to stop it", file=sys.stderr)
    try:
        loop.run_until_complete(_start(app, loop))
        loop.run_forever()
    finally:
        if not closing:
            loop.run_until_complete(_shutdown(app, stop_loop=False))
        loop.run_until_complete(_drain(closing, loop))
        loop.close()
    return 0


async def _drain(closing: list, loop) -> None:
    """The shutdown the signal started, given its turn — and the loop stopped if it somehow didn't get that far."""
    if closing:
        try:
            await asyncio.wait_for(closing[0], timeout=10)
        except (TimeoutError, asyncio.CancelledError):              # it didn't finish: leave anyway
            print("shutdown: the board didn't answer within 10 s", file=sys.stderr)
        except Exception as e:                                    # noqa: BLE001 - it already failed; say so and leave
            print(f"shutdown: {e}", file=sys.stderr)
    if loop.is_running():                                         # pragma: no cover - only if _shutdown never ran
        loop.stop()


async def _start(app: DeckApp, loop) -> None:
    app.board.connect(loop=loop)
    app.start(loop=loop)


async def _shutdown(app: DeckApp, stop_loop: bool = True) -> None:
    await app.board.stop()
    await app.stop()
    if stop_loop:
        asyncio.get_running_loop().stop()


if __name__ == "__main__":                                      # pragma: no cover
    sys.exit(main(sys.argv[1:]))
