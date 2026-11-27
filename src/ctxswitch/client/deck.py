"""``ctx deck`` — an Elgato Stream Deck as a button box for the board.

The board (`ctx tui`) listens on a unix socket (PROTOCOL.md: NDJSON, one object per line). This talks that protocol
and paints the 15 keys of a Stream Deck MK.2 with what the board is showing: five session slots; the middle row's
`gpu` `pool` `spend` `exchange` `detach`, the two panels and the spend screen lit while they are up; and ▲▼ to move
the highlight (hold one and it opens the row it landed on), `restart`, `new` and `kill` — the last of which makes the
board ask, so the key becomes KILL ✓ / cancel. While a modal is up the middle row is cancel with the bottom one, the
one exception being `spend` while the spend screen is showing, which closes it.

Everything that decides *what a key looks like* or *what a press sends* lives in `Layout`, which needs neither
Pillow, nor a device, nor a board — so that is the part the tests read. `render` and `DeckApp` only carry it out.

The faces move: a waiting session breathes, a busy one sweeps a bar down its left edge, and the key a switch just
made visible fades out of white. All three are a function of the state and one clock, so the tests read them too.
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
BREATHE_S = 1.6                    # a waiting session breathes: dark to red and back, once over this long
SWEEP_S = 1.2                      # a busy session sweeps: the bar takes this long down, and the same back up
WIPE_S = 0.25                      # the key a switch just made visible fades out of white over this long
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
    edge: tuple[int, int, str] | None = None   # a sweep bar on the key's left edge: (y0, y1, colour)


# --------------------------------------------------------------------------------------------- the layout table


class Layout:
    """What the keys look like for a board state at a moment in time, and what a press sends. Pure functions of the
    state, the clock, and the wipes still running."""

    def plan(self, state: dict | None, t: float = 0.0, wipes: dict[int, float] | None = None) -> dict[int, KeyFace]:
        faces = self._base(state, t)
        for key, t0 in (wipes or {}).items():         # the key a switch just made visible: white, fading back
            face = faces.get(key)
            if face is not None:
                face.bg = blend(WHITE, face.bg, min(1.0, (t - t0) / WIPE_S))   # its text stays white: it re-emerges
        return faces

    def _base(self, state: dict | None, t: float) -> dict[int, KeyFace]:
        """The faces as they would stand still, with the clock only deciding the breathing and the sweep."""
        if state is None:
            faces = {i: KeyFace() for i in range(KEY_COUNT)}
            faces[7] = KeyFace(lines=("no board",), dim=True)      # grey on black, in the centre
            return faces
        modal = state.get("modal")
        if modal in ("picker", "start"):
            return self._picker(state)
        if modal == "confirm":
            return self._confirm()
        if modal == "input":
            return self._typing(state, t)
        if modal == "spend":
            return self._spend(state)
        return self._normal(state, t)

    def animating(self, state: dict | None, wipes: dict[int, float] | None = None) -> bool:
        """Whether the next frame can differ from this one: a session waiting (it breathes), one busy (it sweeps),
        or a wipe still running. A board that is all idle and settling paints nothing until it next changes."""
        if wipes:
            return True
        return any((r or {}).get("state") in ("waiting", "busy") for r in (state or {}).get("rows") or [])

    def _rows(self, state: dict) -> dict[int, dict]:
        """Board rows by key index: ``n`` is the number on the board, so slot ``n`` is key ``n-1``."""
        return {r.get("n", i + 1) - 1: r for i, r in enumerate(state.get("rows") or [])}

    def _normal(self, state: dict, t: float) -> dict[int, KeyFace]:
        faces: dict[int, KeyFace] = {}
        rows = self._rows(state)
        for i in range(5):
            r = rows.get(i)
            if r is None:
                faces[i] = KeyFace(lines=(str(i + 1),), dim=True)   # black, the number in dark grey only
                continue
            row_state = r.get("state")
            waiting = row_state == "waiting"
            dot = DOT.get(row_state, DOT["unknown"])
            faces[i] = KeyFace(bg=blend(SLOT_BG[i % len(SLOT_BG)], WAIT_BG, breathe(t)) if waiting else
                               SLOT_BG[i % len(SLOT_BG)],
                               lines=(str(i + 1), _title(r.get("title"))),
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

    def _confirm(self) -> dict[int, KeyFace]:
        faces = {i: KeyFace() for i in range(ROW2)}
        self._cancel_row2(faces)
        for i in (10, 11, 12, 13):
            faces[i] = KeyFace(bg=ACTION_BG, lines=("cancel",))
        faces[14] = KeyFace(bg=CONFIRM_BG, lines=("KILL", "✓"))
        return faces

    def _typing(self, state: dict, t: float) -> dict[int, KeyFace]:
        """The board's text Input has focus (`/`, `m`, `a`, a directory prompt): no key here can help, so say so.
        The rows underneath are still doing what they do, so they keep moving."""
        faces = self._normal(state, t)
        for i in range(ROW2, KEY_COUNT):
            faces[i] = KeyFace(bg=ACTION_BG, lines=("type on\nthe board",))
        return faces

    def on_press(self, state: dict | None, key: int) -> list[dict]:
        """The requests to send the moment key ``key`` goes down. Nothing at all for a blank key, or while the
        board is unreachable: with no state there is nothing to act on and no one to send it to."""
        if key not in range(KEY_COUNT) or state is None:
            return []
        modal = state.get("modal")
        if modal in ("picker", "start"):
            if key < 5:
                return [{"cmd": "pick", "n": key + 1}]
            return [{"cmd": "cancel"}] if key >= ROW2 else []
        if modal == "confirm":
            if key == 14:
                return [{"cmd": "confirm", "yes": True}]
            return [{"cmd": "cancel"}] if key >= ROW2 else []
        if modal == "input":
            return []
        if modal == "spend":
            # the spend screen has the whole pane and its own Esc: key 7 is the $ that opened it, and everything
            # else on the deck is that Esc
            if key == ROW2 + 2:
                return [{"cmd": "spend"}]
            return [] if key < ROW2 else [{"cmd": "cancel"}]
        if key < 5:
            return [{"cmd": "slot", "n": key + 1}]
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
                return [{"cmd": "slot", "n": r.get("n", i + 1)}]     # the same row → key numbering as `_rows`
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
        d.ellipse((w - 11, h - 11, w - 1, h - 1), fill=_hex(face.dot))
    ink = _hex(DIM_GREY) if face.dim else _hex(WHITE)
    y = 6
    for i, line in enumerate(face.lines):
        size = label_size(line, i, w)
        d.multiline_text((5, y), "\n".join(_wrap(line, w, size)), fill=ink, font=_font(size, bold=size == 26),
                         spacing=2)
        y += size + 7
    return PILHelper.to_native_format(deck, img)


def describe(face: KeyFace) -> str:
    """A face in words — what ``ctx deck --no-device`` prints instead of painting. The background carries the
    animation for anyone watching by eye; the edge says where a sweep bar is."""
    marks = "".join("T" if face.top_bar else "") + ("B" if face.bottom_bar else "")
    return (f"{face.bg} {'|'.join(' '.join(line.split()) for line in face.lines)!r}"
            + (f" edge={face.edge}" if face.edge else "")
            + (f" dot={face.dot}" if face.dot else "") + (f" border={face.border}" if face.border else "")
            + (f" bars={marks}" if marks else "") + (" dim" if face.dim else ""))


def label_size(line: str, index: int, width: int = KEY_W) -> int:
    """How big one line of a key's text is drawn: 26 px for a slot number or an arrow, 15 px for the key's own label,
    11 px for board text under it. A label too long for the key shrinks rather than wraps ("restart", "exchange"),
    down to 10 px at the least, so a whole word always comes out whole — which is also what a test can ask without a
    device: the size it picks is the biggest one whose measured text still fits the key."""
    if index == 0 and len(line.strip()) <= 2:
        return 26
    if index:
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
                 heartbeat_s: float = 0.0, echo=print, clock=time.monotonic):
        self.deck = deck
        self.board = board
        self.layout = layout or Layout()
        self.log = log
        self.echo = echo                                        # the running commentary, not the problems
        self.fps = fps
        self.heartbeat_s = heartbeat_s
        self.state: dict | None = None
        self.faces: dict[int, KeyFace] = {}
        self.wipes: dict[int, float] = {}       # key → when its wipe started; dropped when the fade is done
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
        """Take a state the board sent: start any wipe it calls for, then paint every key whose face changed.
        Returns how many were painted."""
        self.state = state
        self._wipes(state)
        return self._paint(self.clock())

    def _wipes(self, state: dict | None) -> None:
        """A state whose `visible_key` isn't the one before it is a switch, and the key that came out is wiped. The
        first state says nothing moved — there is nothing before it to have moved from."""
        visible = (state or {}).get("visible_key")
        if self._had_state and visible is not None and visible != self._last_visible:
            index = next((r.get("n", i + 1) - 1 for i, r in enumerate((state or {}).get("rows") or [])
                          if r.get("key") == visible), None)
            if index in range(KEY_COUNT):                       # a row off the top of the deck has no key to wipe
                self.wipes[index] = self.clock()
        self._last_visible = visible
        self._had_state = True

    def _paint(self, t: float) -> int:
        """Plan the board as it stands at ``t`` and send the keys whose face differs from what they show."""
        started = time.monotonic()
        self.wipes = {key: t0 for key, t0 in self.wipes.items() if (t - t0) / WIPE_S < 1.0}
        plan = self.layout.plan(self.state, t, self.wipes)
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

    def _anim_count(self) -> int:
        """How many keys the animations are moving right now: the rows that breathe or sweep, plus any wiping."""
        rows = (self.state or {}).get("rows") or []
        live = sum(1 for r in rows if (r or {}).get("state") in ("waiting", "busy"))
        return live + len(self.wipes)

    async def _animate(self) -> None:
        """The animation's own clock. Nothing else moves on it: when no session is waiting or busy and no wipe is
        running, the tick looks, finds nothing to do, and costs nothing."""
        while True:
            await asyncio.sleep(1.0 / self.fps)
            self._tick()

    def _tick(self) -> int:
        """One frame, if there is one to be had. Returns how many keys it sent."""
        t = self.clock()
        if not self.layout.animating(self.state, self.wipes):
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
            reqs = self.layout.on_press(state, key)
        else:
            t0 = self._down.pop(key, None)
            if t0 is None:
                return                                          # a release with no press behind it: nothing was held
            reqs = self.layout.on_release(state, key, (now - t0) * 1000)
        self._send(key, reqs)

    def _send(self, key: int, reqs: list[dict]) -> None:
        """Get the layout's requests to the board, from whichever thread the device callback arrived on."""
        for req in reqs:
            cmd, fields = req.get("cmd"), {k: v for k, v in req.items() if k != "cmd"}
            try:
                reply = _request(self.board, cmd, fields, self.loop, self.log)
            except Exception as e:                              # noqa: BLE001 - a key press is not worth a traceback
                reply = {"ok": False, "error": str(e)}
            if reply is not None and not reply.get("ok"):
                self.log(f"key {key}: {reply.get('error') or 'the board said no'}")

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
    ap.add_argument("--brightness", type=int, default=60, help="key light brightness, 0-100 (default 60)")
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
    try:
        deck.set_brightness(a.brightness)
    except Exception as e:                                      # noqa: BLE001 - the keys work without the lights dimmed
        print(f"brightness: {e}")
    board = BoardClient(path, log=lambda m: print(m, file=sys.stderr))
    app = DeckApp(deck, board, log=lambda m: print(m, file=sys.stderr), heartbeat_s=HEARTBEAT_S,
                  echo=lambda m: print(m, file=sys.stderr, flush=True))
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
