"""`ctx deck`: the board's table as fifteen keys.

The layout is tested as pure data (`Layout.plan` / `Layout.on_press` — no Pillow, no device, no board), the drawing
as bytes off a fake deck, and the wire against a fake board: a real `asyncio.start_unix_server` that speaks
PROTOCOL.md, so the client is tested on the contract the board's own job implements.
"""
from __future__ import annotations

import asyncio
import atexit
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from conftest import unix_sockets_ok
from ctxswitch.client import deck
from ctxswitch.client.deck import BoardClient, DeckApp, FakeDeck, KeyFace, Layout, render
from ctxswitch.common.deck_settings import save_settings

try:                                                       # the deck extra is optional here, as it is on any host
    from StreamDeck.Transport import TransportError
except ImportError:                                        # `deck` recognises a dead handle by its name, so this
    class TransportError(Exception):                       # behaves the same as the library's own
        pass

pytestmark = pytest.mark.timeout(10)
needs_unix = pytest.mark.skipif(not unix_sockets_ok(), reason="this kernel won't let a test bind a unix socket")

SLOT_BG = deck.SLOT_BG


@pytest.fixture(autouse=True)
def _settings_file(tmp_path_factory, monkeypatch):
    """The settings file a deck reads when a test doesn't say otherwise: one that isn't there, so every deck in this
    file starts on the defaults however its operator's own board is set up. A test that wants a file writes its own
    and hands `DeckApp` the path."""
    monkeypatch.setattr(deck, "SETTINGS_FILE", tmp_path_factory.mktemp("deck-settings") / "deck-settings")


def row(n: int, **kw) -> dict:
    r = {"n": n, "key": f"laptop/claude/{n}", "title": f"session {n}", "host": "laptop", "harness": "claude",
         "state": "busy", "visible": False, "selected": False, "pool_jobs": 0}
    return {**r, **kw}


def board_state(rows=None, modal=None, options=None, panels=None, visible_key=None, selected_key=None,
                start=...) -> dict:
    """A `state` object as PROTOCOL.md describes it: three live rows unless told otherwise, both panels off.
    `visible_key` is the row in the main pane — the one the top bar is on, unless a test says otherwise. `start` is
    the Start screen's own state; left alone it is null, and `start=None` is an older board that has no such field."""
    rows = rows if rows is not None else [row(1), row(2), row(3)]
    if visible_key is None:
        visible_key = next((r["key"] for r in rows if r.get("visible")), rows[0]["key"])
    if selected_key is None:
        selected_key = rows[-1]["key"]
    st = {"proto": 1, "rows": rows, "slots": [r["key"] for r in rows], "visible_key": visible_key,
          "selected_key": selected_key, "modal": modal, "options": options or [],
          "panels": {"gpu": False, "pool": False, **(panels or {})}}
    if start is not ...:
        st["start"] = start
    return st


def recent(title: str, marked: bool = False) -> dict:
    """One of the Start screen's rows to resume, as its `start` state lists them in screen order."""
    return {"key": f"laptop/claude/{title.replace(' ', '')}", "title": title, "marked": marked}


def start_state(cursor: int = 0, marked: int = 0, recents=None) -> dict:
    """The Start screen's state: where its cursor is, how many rows it has marked, and its rows themselves. With the
    rows left to default, `marked=1` marks the cursor's own — the state a board would really answer — and
    `marked=2` marks it and the next. Recents a test hands over are the state exactly as it says."""
    if recents is None:
        recents = [recent("ctx switch"), recent("board control"), recent("the deck")]
        for i in range(min(marked, len(recents) - cursor)):
            recents[cursor + i] = {**recents[cursor + i], "marked": True}
    return {"cursor": cursor, "marked": marked, "recents": list(recents)}


async def until(what, cond, timeout=5.0):
    """Wait for something the event loop has to do, and say what it was if it doesn't. Never longer than `timeout`."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            pytest.fail(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.02)


class FakeBoard:
    """A board that answers the protocol: its own socket, the state it reports, and every request it was sent."""

    def __init__(self, state=None):
        self.state = state if state is not None else board_state()
        self.seen: list[dict] = []
        self.conns: list[asyncio.StreamWriter] = []
        self.server: asyncio.Server | None = None

    async def serve(self, path) -> None:
        self.server = await asyncio.start_unix_server(self._conn, path=str(path))

    async def _conn(self, reader, writer) -> None:
        self.conns.append(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                try:
                    req = json.loads(line)
                except ValueError:
                    writer.write(b'{"ok": false, "error": "bad request"}\n')
                    await writer.drain()
                    continue
                self.seen.append(req)
                writer.write((json.dumps(self._reply(req)) + "\n").encode())
                await writer.drain()
        except (ConnectionResetError, asyncio.CancelledError):   # noqa: BLE001 - the client went away
            pass
        finally:
            if writer in self.conns:
                self.conns.remove(writer)

    def _reply(self, req: dict) -> dict:
        reply = {"ok": True, "id": req.get("id")}
        cmd = req.get("cmd")
        if cmd in ("hello", "state", "subscribe"):
            reply["proto"] = 1
            reply["state"] = self.state
        elif cmd == "slot":
            if not 1 <= int(req.get("n") or 0) <= len(self.state["slots"]):
                return {"ok": False, "id": req.get("id"), "error": "no such slot"}
        elif cmd == "confirm":
            if self.state.get("modal") != "confirm":
                return {"ok": False, "id": req.get("id"), "error": "no confirm open"}
        elif cmd not in ("move", "new", "start", "end", "select", "pick", "cancel"):
            return {"ok": False, "id": req.get("id"), "error": "unknown cmd"}
        return reply

    async def push(self, state: dict) -> None:
        self.state = state
        for w in list(self.conns):
            w.write((json.dumps({"event": "state", "state": state}) + "\n").encode())
            await w.drain()

    async def drop_clients(self) -> None:
        for w in list(self.conns):
            w.close()
        await asyncio.sleep(0.05)

    async def stop(self) -> None:
        for w in list(self.conns):
            w.close()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None


class FakeBoardSync:
    """The board as DeckApp sees it when no socket is involved: a state, and the requests it was sent."""

    def __init__(self, state=None):
        self.state = state if state is not None else board_state()
        self.seen: list[dict] = []
        self.cb = None

    def subscribe(self, callback, loop=None) -> None:
        self.cb = callback

    def request(self, cmd, **fields) -> dict:
        self.seen.append({"cmd": cmd, **fields})
        return {"ok": True}


# ----------------------------------------------------------------------------------------------- the layout table


def test_three_sessions_colour_three_keys_and_leave_two_empty():
    faces = Layout().plan(board_state())
    for i in range(3):
        assert faces[i].bg == SLOT_BG[i], f"slot {i + 1} isn't its own colour"
        assert faces[i].lines[0] == str(i + 1), "the slot number isn't the first thing on the key"
        assert faces[i].lines[1] == f"session {i + 1}"
    for i in (3, 4):
        assert faces[i].bg == deck.BLACK and faces[i].dim and faces[i].lines == (str(i + 1),)
        assert faces[i].dot is None, "an empty slot has nothing to put a dot for"


def test_visible_gets_the_top_bar_and_selected_the_bottom_one():
    faces = Layout().plan(board_state(rows=[row(1, visible=True), row(2, selected=True)]))
    assert faces[0].top_bar and not faces[0].bottom_bar
    assert faces[1].bottom_bar and not faces[1].top_bar


@pytest.mark.parametrize("session_state,colour", [("busy", "#ffd700"), ("waiting", "#ff3b30"), ("idle", "#34c759"),
                                                  ("shell", "#8e8e93"), ("unknown", "#8e8e93"), ("ended", "#3a3a3c")])
def test_each_session_state_has_its_dot(session_state, colour):
    faces = Layout().plan(board_state(rows=[row(1, state=session_state)]))
    assert faces[0].dot == colour
    assert faces[0].border == (deck.WAIT_BG if session_state == "waiting" else None)


def test_a_waiting_session_breathes_between_its_colour_and_red():
    """No flash: the key eases from its own colour to the waiting red and back, so it catches the eye without
    strobing. The border and the dot are the steady half — they are on whatever the background is doing."""
    waiting = board_state(rows=[row(1, state="waiting"), row(2, state="idle")])
    off = Layout().plan(waiting, 0.0)
    on = Layout().plan(waiting, deck.BREATHE_S / 2)
    assert off[0].bg == SLOT_BG[0] and on[0].bg == deck.WAIT_BG
    assert off[0].border == on[0].border == deck.WAIT_BG, "the border is on for the whole breath"
    assert off[0].dot == on[0].dot == deck.WAIT_BG
    assert off[0].lines == on[0].lines, "the breath changes the colour, not what the key says"
    assert off[1].bg == on[1].bg == SLOT_BG[1], "a session that isn't waiting doesn't breathe"
    assert off[1].edge is None


def test_the_breath_is_smooth_and_holds_still_when_it_looks_still():
    """20 frames a second over a 1.6 s cycle is 32 frames, so a shade has to last more than one frame — otherwise
    every tick puts a key back on the wire for a difference nobody can see."""
    waiting = board_state(rows=[row(1, state="waiting")])
    L = Layout()
    cycle = [L.plan(waiting, i / deck.FPS)[0].bg for i in range(int(deck.BREATHE_S * deck.FPS))]
    assert len(set(cycle)) > 1, "the breath never moves"
    assert len(set(cycle)) < len(cycle), "every frame is its own shade, so every frame is a repaint"
    assert cycle[0] == cycle[-1] == SLOT_BG[0], "a cycle of frames has to end where it started"
    assert L.plan(waiting, 0.799)[0].bg == L.plan(waiting, 0.8)[0].bg, "two frames 50 ms apart look the same here"


def test_a_busy_session_sweeps_down_its_left_edge():
    busy = board_state(rows=[row(1, state="busy"), row(2, state="idle")])
    L = Layout()
    at_zero, later = L.plan(busy, 0.0), L.plan(busy, 0.3)
    assert at_zero[0].edge == (deck.SWEEP_TOP, deck.SWEEP_TOP + deck.SWEEP_H - 1, deck.DOT["busy"])
    assert later[0].edge[0] > at_zero[0].edge[0], "the bar didn't move down the key"
    assert later[0].edge[2] == deck.DOT["busy"]
    assert all(deck.SWEEP_TOP <= y <= deck.KEY_H - deck.SWEEP_BOTTOM
               for y in (at_zero[0].edge[0], later[0].edge[0])), "the bar left the key"
    assert at_zero[0].lines == later[0].lines and at_zero[0].dot == later[0].dot, "nothing else on the face moves"
    assert at_zero[1].edge is None, "a session that isn't busy has nothing sweeping"


@pytest.mark.parametrize("t", [0.0, 0.3, 0.6, 1.2, 1.8, 2.4, 3.7])
def test_the_sweep_bar_is_only_ever_where_a_key_can_show_it(t):
    y = deck.sweep_y(t, 72)
    assert deck.SWEEP_TOP <= y <= 72 - deck.SWEEP_BOTTOM, f"at t={t} the bar sits at y={y}"
    assert 0 <= y + deck.SWEEP_H - 1 <= 72, f"at t={t} the bar runs off the key"


def test_a_long_title_is_wrapped_or_shortened():
    faces = Layout().plan(board_state(rows=[row(1, title="one " * 30)]))
    assert len(faces[0].lines) == 2
    face = Layout().plan(board_state(rows=[row(1, title="a" * 200)]))[0]
    assert face.lines[1].endswith("…") and len(face.lines[1]) <= 26


def test_picker_keys_are_the_options_and_the_rest_cancel():
    faces = Layout().plan(board_state(modal="picker", options=[{"n": 1, "label": "claude (max)"},
                                                               {"n": 2, "label": "pi"}]))
    assert faces[0].bg == deck.OPTION_BG and faces[0].lines == ("1", "claude (max)")
    assert faces[1].lines == ("2", "pi")
    for i in (2, 3, 4):
        assert faces[i] == KeyFace(), "an option key with no option stays black"
    for i in range(deck.ROW2, deck.KEY_COUNT):
        assert faces[i].lines == ("cancel",), f"key {i} should offer cancel"
        assert faces[i].bg == deck.ACTION_BG, f"key {i}'s cancel is an ordinary action face"
    assert faces[10].bg == faces[11].bg == faces[12].bg == faces[13].bg


def test_the_spend_screen_leaves_only_its_own_key_lit():
    """The spend screen takes the whole pane and answers its own Esc, so every key is a way out — except key 7,
    which is still the $ that opened it, lit, and closes it again."""
    faces = Layout().plan(board_state(modal="spend"))
    assert faces[7].lines == ("spend",) and faces[7].bg == deck.LIT_BG and faces[7].bottom_bar
    for i in (0, 1, 2, 3, 4):
        assert faces[i] == KeyFace(), "the rows are behind the spend screen, so they do nothing"
    for i in (5, 6, 8, 9, 10, 11, 12, 13, 14):
        assert faces[i].lines == ("cancel",) and faces[i].bg == deck.ACTION_BG, f"key {i} should offer cancel"


def start_mode_state(cursor: int = 0, marked: int = 0, recents=None, start=...) -> dict:
    """A board whose Start screen is up, with three profile options and the `start` state its screen would report.
    ``start=None`` is an older board, whose state has no such field."""
    st = board_state(modal="start", options=[{"n": 1, "label": "claude · max"}, {"n": 2, "label": "pi"},
                                             {"n": 3, "label": "shell"}], start=start_state(cursor, marked, recents))
    if start is not ...:
        st["start"] = start
    return st


def test_the_start_screen_keeps_the_picker_s_option_keys():
    """Its top row is the picker's, unchanged: one key per profile the screen's digits start."""
    faces = Layout().plan(start_mode_state())
    picker = Layout().plan(board_state(modal="picker", options=[{"n": 1, "label": "claude · max"},
                                                               {"n": 2, "label": "pi"}, {"n": 3, "label": "shell"}]))
    for i in range(5):
        assert faces[i] == picker[i], f"key {i} is not the option face the picker would put there"


def test_the_start_screen_blacks_the_board_s_own_middle_row():
    """The board's own commands are nobody's to run under this screen, so the middle row is blank — not the
    picker's nine ways out. What the screen does answer to is the bottom row, tested on its own."""
    faces = Layout().plan(start_mode_state())
    for i in range(deck.ROW2, 10):
        assert faces[i] == KeyFace(), f"key {i} would run one of the board's commands under the start screen"


def test_the_start_screen_keys_walk_mark_and_resume():
    """Keys 10–14: ▲▼ the screen's cursor, `select` its Space, `cancel` its Esc and `enter` its Enter — the four
    things the screen answers to, and nothing of the board's."""
    faces = Layout().plan(start_mode_state())
    assert faces[10].lines == ("▲",) and faces[10].bg == deck.ARROW_BG
    assert faces[11].lines == ("▼",) and faces[11].bg == deck.ARROW_BG
    assert faces[12].lines == ("select",) and faces[12].bg == deck.ACTION_BG and faces[12].sub is None
    assert faces[13].lines == ("cancel",) and faces[13].bg == deck.ACTION_BG
    assert faces[14].lines == ("enter",) and faces[14].bg == deck.ACTION_BG and faces[14].sub is None
    assert not faces[14].dim, "there are rows to bring back, so the key isn't dim"


def test_the_start_screen_s_select_key_lights_for_a_marked_row_and_shows_no_title():
    """`select` says which row its Space would mark, in the small line under the label, and goes to the lit colour
    while that row is marked."""
    L = Layout()
    face = L.plan(start_mode_state(cursor=1))[12]
    assert face.lines == ("select",) and face.sub is None and face.bg == deck.ACTION_BG
    marked = L.plan(start_mode_state(cursor=1, marked=1))[12]
    assert marked.bg == deck.LIT_BG and marked.lines == ("select",) and marked.sub is None
    # the cursor moved off the marked row: the key says which row Space would mark now, and it is not lit
    away = L.plan(start_mode_state(cursor=2, marked=1, recents=[recent("ctx switch"), recent("board control", True),
                                                                recent("the deck")]))[12]
    assert away.bg == deck.ACTION_BG and away.sub is None


def test_the_start_screen_s_enter_key_shows_no_count_and_dims_when_empty():
    """`enter` resumes what is marked, or the cursor row when nothing is: the key says how many, and says `nothing`
    and goes dim when the screen has no rows at all."""
    L = Layout()
    assert L.plan(start_mode_state(marked=2))[14].sub is None
    assert L.plan(start_mode_state(cursor=2))[14].sub is None, "no count on the key"
    empty = L.plan(start_mode_state(recents=[]))[14]
    assert empty.lines == ("enter",) and empty.sub is None and empty.dim


def test_an_older_board_s_start_screen_says_only_what_the_two_keys_are(monkeypatch):
    """A board whose state has no `start`: the two keys that could say more say only what they are, and go on doing
    the same when pressed — the screen underneath answers them either way."""
    older = start_mode_state(start=None)
    del older["start"]                                   # a board that never heard of the field at all
    for st in (start_mode_state(start=None), older):
        assert Layout().plan(st) == Layout().plan(older), "a null `start` and no `start` are the same board"
    faces = Layout().plan(older)
    assert faces[12].lines == ("select",) and faces[12].sub is None and faces[12].bg == deck.ACTION_BG
    assert faces[14].lines == ("enter",) and faces[14].sub is None and not faces[14].dim
    for i in (10, 11, 13):
        assert faces[i].lines == Layout().plan(start_mode_state())[i].lines, f"key {i} says the same either way"
    for i in range(deck.ROW2, deck.KEY_COUNT):
        assert faces[i].bg != deck.BLACK or i in range(deck.ROW2, 10), f"key {i} went black for want of a field"


def test_a_key_face_can_carry_one_small_line_under_its_label():
    """`sub` is one 11 px line under the key's own lines, which keep their sizes — `small` makes every line 11 px,
    which is not what a label with a note under it wants."""
    face = KeyFace(lines=("enter",), sub="resume 2")
    assert deck.label_size(face.lines[0], 0) == 15, "the label keeps the size a key's own word gets"
    assert deck.label_size(face.sub, 1) == 11
    img = render(face, FakeDeck())
    assert img[:2] == b"\xff\xd8" and deck.describe(face) == "#000000 'enter' sub='resume 2'"
    both = render(KeyFace(lines=("select",), sub="a session title far too long for one key"), FakeDeck())
    assert both[:2] == b"\xff\xd8", "a sub too long for the key still draws"


def test_confirm_puts_kill_on_the_last_key():
    faces = Layout().plan(board_state(modal="confirm"))
    assert faces[14].bg != faces[14].dot and faces[14].bg.startswith("#d") and faces[14].lines == ("KILL", "✓")
    for i in (10, 11, 12, 13):
        assert faces[i].lines == ("cancel",)
    for i in range(5):
        assert faces[i] == KeyFace(), "a confirm answers on one key; the slots do nothing"


def test_no_board_is_said_on_the_centre_key_only():
    faces = Layout().plan(None)
    assert len(faces) == deck.KEY_COUNT
    assert faces[7].lines == ("no board",) and faces[7].dim
    for i, face in faces.items():
        if i != 7:
            assert face == KeyFace(), f"key {i} should be black with nothing on it"


def test_the_buttons_normal_mode_gives():
    faces = Layout().plan(board_state())
    assert faces[10].lines == ("▲",) and faces[11].lines == ("▼",)
    assert faces[10].bg == faces[11].bg == deck.ARROW_BG
    assert faces[12].lines == ("restart",) and faces[13].lines == ("new",) and faces[14].lines == ("kill",)
    assert faces[12].bg == faces[13].bg == deck.ACTION_BG and faces[14].bg == deck.END_BG


def test_the_middle_row_names_the_five_board_commands():
    faces = Layout().plan(board_state())
    for i, cmd in enumerate(deck.ROW2_CMDS):
        face = faces[deck.ROW2 + i]
        assert face.lines == (cmd,), f"key {deck.ROW2 + i} should be the {cmd} key"
        assert face.bg == (deck.END_BG if cmd == "detach" else deck.ACTION_BG)
        assert not face.bottom_bar and face.dot is None, "a toggle that is off isn't lit"


@pytest.mark.parametrize("key,panels,modal", [(5, {"gpu": True}, None), (6, {"pool": True}, None),
                                              (7, None, "spend")])
def test_a_toggle_key_is_lit_while_its_thing_is_up(key, panels, modal):
    faces = Layout().plan(board_state(modal=modal, panels=panels))
    assert faces[key].bg == deck.LIT_BG and faces[key].bottom_bar, f"key {key} should read as on"
    assert faces[key].lines == Layout().plan(board_state())[key].lines, "lit or not, the key says the same thing"
    others = [i for i in range(deck.ROW2, deck.ROW2 + 4) if i != key]
    for i in others:
        assert not faces[i].bottom_bar and faces[i].bg != deck.LIT_BG, f"key {i} is off, so it stays dark"


def test_a_state_with_no_panels_field_is_still_a_normal_board():
    """An older board's state has no `panels`: the row is there, nothing lit, rather than anything missing."""
    st = board_state()
    del st["panels"]
    faces = Layout().plan(st)
    assert [faces[i].lines for i in range(deck.ROW2, deck.ROW2 + 5)] == [(c,) for c in deck.ROW2_CMDS]
    assert all(faces[i].bg != deck.LIT_BG for i in range(deck.ROW2, deck.ROW2 + 5))


def test_typing_mode_leaves_row_2_alone():
    """The Input has focus: nothing on the middle row is the operator's to run, and one of them would take the board
    out of the box it is typing in, so all five keys only say where the text goes."""
    faces = Layout().plan(board_state(modal="input"))
    for i in range(deck.ROW2, deck.ROW2 + deck.COLS):
        assert faces[i].lines == (deck.TYPE_HINT,) and faces[i].bg == deck.ACTION_BG
    assert Layout().on_press(board_state(modal="input"), 5) == []


def test_typing_mode_gives_the_text_box_its_own_two_keys():
    """The bottom row while the box has focus: `enter` on the last key and `cancel` on the first — the two keys the
    box answers to on the board — with the three between them still only saying where the text goes."""
    faces = Layout().plan(board_state(modal="input"))
    assert faces[10].lines == ("cancel",) and faces[14].lines == ("enter",)
    for i in (11, 12, 13):
        assert faces[i].lines == (deck.TYPE_HINT,), f"key {i} should still only say where to type"
    for i in range(deck.ROW2, deck.KEY_COUNT):
        assert faces[i].bg == deck.ACTION_BG and faces[i].ink is None, f"key {i} should read white on dark"
    L = Layout()
    st = board_state(modal="input")
    assert L.on_press(st, 14) == [{"cmd": "submit"}], "enter accepts the Input with the text it holds"
    assert L.on_press(st, 10) == [{"cmd": "cancel"}], "cancel drops it, as Esc does"
    for key in (0, 5, 11, 12, 13):
        assert L.on_press(st, key) == [], f"key {key} must not take the board out of the box mid-type"


@pytest.mark.parametrize("prompt,text,want", [
    ("dir for the new session: ", "~/repos/ctx", ("dir for the new session:", "~/repos/ctx")),
    ("what should it do? ", "", ("what should it do?", "")),
    (None, "half a message", (None, "half a message")),
    ("a prompt", None, ("a prompt", None)),
    (None, None, None),
])
def test_the_box_shows_its_prompt_and_what_is_typed_on_the_middle_key(prompt, text, want):
    """A board that says what its Input is asking and holds shows both on key 12, prompt first, in the small size the
    slot titles use. One that says nothing — an older board — leaves the plain reminder there instead."""
    st = board_state(modal="input")
    if prompt is not None or text is not None:
        st = {**st, "input_prompt": prompt, "input_text": text}
    face = Layout().plan(st)[12]
    if want is None:
        assert face.lines == (deck.TYPE_HINT,), "with nothing to show, the key says where the text goes"
    else:
        assert face.lines == tuple("" if line is None else line for line in want)
        assert face.small, "both lines small, as a slot title is"
    assert face.bg == deck.ACTION_BG


# --------------------------------------------------------------------------------------------------- animations
#
# All three are pure functions of (state, t, wipes), so they are read here rather than watched on the device:
# `blend` mixes a colour, `breathe` says how red a waiting session is, `sweep_y` where the busy bar has got to,
# and a wipe is `plan`'s third argument.


def test_blend_mixes_straight_between_two_colours_and_is_exact_at_both_ends():
    assert deck.blend("#000000", "#ffffff", 0.5) == "#808080"
    assert deck.blend("#1f77b4", "#ff3b30", 0.0) == "#1f77b4", "at 0 the first colour, unchanged"
    assert deck.blend("#1f77b4", "#ff3b30", 1.0) == "#ff3b30", "at 1 the second, unchanged"
    assert deck.blend("#ff3b30", "#1f77b4", 1.0) == "#1f77b4"
    assert deck.blend("#000000", "#ffffff", 0.25) == "#404040"
    assert deck.blend("#1f77b4", "#ff3b30", -0.5) == "#1f77b4", "a mix outside 0…1 is clamped, not wrapped"
    assert deck.blend("#1f77b4", "#ff3b30", 1.5) == "#ff3b30"
    assert deck.blend("#ffffff", "#ffffff", 0.37) == "#ffffff"


def test_breathe_goes_from_zero_to_one_and_back_over_one_cycle():
    assert deck.breathe(0.0) == 0.0
    assert abs(deck.breathe(deck.BREATHE_S / 2) - 1.0) < 1e-6
    assert abs(deck.breathe(deck.BREATHE_S) - 0.0) < 1e-6
    assert abs(deck.breathe(3 * deck.BREATHE_S + deck.BREATHE_S / 2) - 1.0) < 1e-6, "the cycle keeps repeating"
    assert all(0.0 <= deck.breathe(t / deck.FPS) <= 1.0 for t in range(400)), "it never leaves 0…1"


def test_breathe_holds_a_shade_long_enough_that_a_still_frame_is_not_a_repaint():
    """Quantized to 1/32: two frames that mix to the same colour compare equal, and the face cache keeps the key
    off the wire. Unquantized, every one of the 32 frames in a cycle would be a shade of its own."""
    assert deck.breathe(0.799) == deck.breathe(0.8)
    assert deck.breathe(0.0) == deck.breathe(0.015)
    assert deck.breathe(0.8) == deck.breathe(0.81)


def test_sweep_y_runs_down_the_key_and_back_as_a_triangle():
    assert deck.sweep_y(0.0, 72) == 4
    assert deck.sweep_y(deck.SWEEP_S, 72) == 72 - deck.SWEEP_BOTTOM
    assert deck.sweep_y(2 * deck.SWEEP_S, 72) == 4
    assert deck.sweep_y(0.6, 72) == 28, "half way down at half the time, both ways"
    assert deck.sweep_y(1.8, 72) == 28, "…and half way back up"
    assert deck.sweep_y(0.3, 72) == 16 and deck.sweep_y(0.9, 72) == 40, "down at a steady pace"
    assert deck.sweep_y(2.4, 72) == deck.sweep_y(0.0, 72) == deck.sweep_y(4.8, 72), "the cycle repeats"


def test_a_wipe_fades_the_key_that_just_came_out_of_white():
    """`visible_key` moved, so the key that gained it says so: white at once, its own colour after WIPE_S, and
    something between in between. The text is white the whole while, so it re-emerges as the background comes down."""
    st = board_state(rows=[row(1, state="idle"), row(2, state="idle")])
    t0 = 10.0
    normal = Layout().plan(st, t0)[1]
    assert Layout().plan(st, t0, {1: t0})[1].bg == deck.WHITE
    mid = Layout().plan(st, t0 + deck.WIPE_S / 2, {1: t0})[1]
    assert mid.bg == deck.blend(deck.WHITE, normal.bg, 0.5) != normal.bg != deck.WHITE
    done = Layout().plan(st, t0 + deck.WIPE_S, {1: t0})[1]
    assert done == normal, "at the end of the wipe the face is the face"
    assert done.bg == normal.bg and done.lines == normal.lines and done.dot == normal.dot, "bars and dot as normal"
    assert Layout().plan(st, t0, {1: t0 - deck.WIPE_S})[1] == normal, "a wipe that finished changes nothing"
    assert Layout().plan(st, t0, {})[1] == normal, "no wipes: the plan is the plain one"


def test_a_wipe_does_not_disturb_the_key_it_is_not_on():
    st = board_state(rows=[row(1, state="waiting"), row(2, state="idle")])
    t = 0.4
    wiped = Layout().plan(st, t, {1: t})
    plain = Layout().plan(st, t)
    assert wiped[0] == plain[0], "the breathing key breathes whether another one is wiping or not"


def test_animating_is_true_only_while_something_is_moving():
    L = Layout()
    assert L.animating(board_state(rows=[row(1, state="idle"), row(2, state="idle")])) is False
    assert L.animating(board_state(rows=[row(1, state="busy")])) is True
    assert L.animating(board_state(rows=[row(1, state="waiting")])) is True
    assert L.animating(board_state(rows=[row(1, state="shell"), row(2, state="ended")])) is False
    assert L.animating(board_state(rows=[row(1, state="idle")]), {3: 1.0}) is True, "a wipe is movement of its own"
    assert L.animating(board_state()) is True, "the default state's rows are busy"
    assert L.animating(None) is False, "with no board nothing moves"
    assert L.animating(board_state(rows=[row(1, state="idle")], modal="confirm")) is False


def test_the_rows_behind_a_modal_are_still_rows():
    """A modal covers the pane, but the state still carries its rows and what they are doing — which is what lets
    the keys behind it keep breathing and sweeping while the middle row and the bottom one say `cancel`."""
    for modal in ("picker", "confirm", "spend", "input"):
        st = board_state(modal=modal, rows=[row(1, state="waiting", visible=True), row(2, state="busy")])
        assert st["visible_key"] == st["rows"][0]["key"], f"a {modal} state still says which row is in the pane"
        assert Layout().animating(st) is True, f"a {modal} shouldn't stop the top row moving"


# ------------------------------------------------------------------------------------------ what a press sends


def test_presses_in_normal_mode():
    st = board_state()
    L = Layout()
    assert L.on_press(st, 2) == [{"cmd": "slot", "n": 3}]
    assert L.on_press(st, 0) == [{"cmd": "slot", "n": 1}]
    assert L.on_press(st, 10) == [{"cmd": "move", "delta": -1}]
    assert L.on_press(st, 11) == [{"cmd": "move", "delta": 1}]
    assert L.on_press(st, 12) == [{"cmd": "start"}]
    assert L.on_press(st, 13) == [{"cmd": "new"}]
    assert L.on_press(st, 14) == [{"cmd": "end"}]


@pytest.mark.parametrize("key,cmd", list(zip(range(deck.ROW2, deck.ROW2 + deck.COLS), deck.ROW2_CMDS)))
def test_the_middle_row_sends_its_own_command(key, cmd):
    assert Layout().on_press(board_state(), key) == [{"cmd": cmd}]


def test_a_press_with_no_board_sends_nothing():
    assert Layout().on_press(None, 0) == []


def test_the_modal_keys():
    L = Layout()
    picker = board_state(modal="picker", options=[{"n": 3, "label": "pi"}])
    assert L.on_press(picker, 2) == [{"cmd": "pick", "n": 3}]
    for key in range(deck.ROW2, deck.KEY_COUNT):
        assert L.on_press(picker, key) == [{"cmd": "cancel"}], f"key {key} should cancel a picker"
    confirm = board_state(modal="confirm")
    assert L.on_press(confirm, 14) == [{"cmd": "confirm", "yes": True}]
    for key in range(deck.ROW2, 14):
        assert L.on_press(confirm, key) == [{"cmd": "cancel"}], f"key {key} should answer the confirm no"
    assert L.on_press(confirm, 0) == [], "the slot keys do nothing while a confirm is up"
    assert L.on_press(board_state(modal="input"), 0) == []


def test_the_start_screen_keys_send_the_screen_s_own_commands():
    """What each of those five keys asks for: the screen's profile digits, its cursor, its mark, its Esc and its
    Enter. The middle row sends nothing at all — under this screen the board's commands are not the deck's to run."""
    L = Layout()
    st = start_mode_state()
    for key in range(5):                                 # every option key, as the picker has them: the board says
        assert L.on_press(st, key) == [{"cmd": "pick", "n": key + 1}], f"key {key} starts profile {key + 1}"
    for key in range(deck.ROW2, 10):
        assert L.on_press(st, key) == [], f"key {key} must not run {deck.ROW2_CMDS[key - deck.ROW2]} here"
    assert L.on_press(st, 10) == [{"cmd": "move", "delta": -1}]
    assert L.on_press(st, 11) == [{"cmd": "move", "delta": 1}]
    assert L.on_press(st, 12) == [{"cmd": "mark"}]
    assert L.on_press(st, 13) == [{"cmd": "cancel"}]
    assert L.on_press(st, 14) == [{"cmd": "submit"}]


def test_the_start_screen_keys_send_the_same_with_no_start_state():
    """An older board's state says nothing about the cursor or the marks; the keys still ask for the same five
    things, because it is the screen that answers them, not the field."""
    L = Layout()
    for key, want in [(0, [{"cmd": "pick", "n": 1}]), (10, [{"cmd": "move", "delta": -1}]),
                      (11, [{"cmd": "move", "delta": 1}]), (12, [{"cmd": "mark"}]),
                      (13, [{"cmd": "cancel"}]), (14, [{"cmd": "submit"}])]:
        assert L.on_press(start_mode_state(start=None), key) == want, f"key {key}"
    for key in range(deck.ROW2, 10):
        assert L.on_press(start_mode_state(start=None), key) == [], f"key {key}"


def test_the_spend_key_toggles_and_the_others_cancel():
    """On the spend screen the middle row is nine ways out and one of them is the key that is already lit: key 7
    sends `spend` again, which is what closes it."""
    L = Layout()
    spend = board_state(modal="spend", panels={"gpu": True, "pool": True})
    assert L.on_press(spend, 7) == [{"cmd": "spend"}]
    for key in (5, 6, 8, 9, 10, 11, 12, 13, 14):
        assert L.on_press(spend, key) == [{"cmd": "cancel"}], f"key {key} should close the spend screen"
    assert L.on_press(spend, 0) == [], "a slot key does nothing behind the spend screen"


def test_a_key_outside_the_deck_sends_nothing():
    assert Layout().on_press(board_state(), 15) == [] and Layout().on_press(board_state(), -1) == []


# --------------------------------------------------------------------------------------------------- drawing


def test_every_kind_of_face_draws_a_jpeg_the_deck_can_take():
    d = FakeDeck()
    faces = [Layout().plan(board_state())[i] for i in range(deck.KEY_COUNT)]
    faces += [Layout().plan(None)[7], Layout().plan(board_state(modal="confirm"))[14],
              Layout().plan(board_state(modal="picker", options=[{"n": 1, "label": "x"}]))[0],
              Layout().plan(board_state(rows=[row(1, state="waiting", visible=True, selected=True)]))[0],
              Layout().plan(start_mode_state(cursor=1, marked=1))[12],      # a label with its small line under it
              Layout().plan(start_mode_state(recents=[]))[14],
              KeyFace()]
    from PIL import Image
    for face in faces:
        img = render(face, d)
        assert img[:2] == b"\xff\xd8", f"{face.lines} didn't come out as JPEG"
        im = Image.open(io.BytesIO(img))
        assert im.size == (72, 72) and im.format == "JPEG"


def test_the_longest_label_on_the_board_is_not_cut_off():
    """"exchange" is the middle row's longest word. It must come out whole on a 72 px key: the size the drawing
    picks has to be one the font fits inside the key's margins, and the pixels must say the same."""
    face = Layout().plan(board_state())[deck.ROW2 + 3]
    assert face.lines == ("exchange",)
    size = deck.label_size(face.lines[0], 0)
    assert size <= 15, "a key's own label never starts bigger than 15 px"
    from ctxswitch.client.deck import _font
    assert _font(size).getlength("exchange") <= deck.KEY_W - 10, f"{size} px doesn't fit a key, so it is truncated"
    assert _font(size + 1).getlength("exchange") > deck.KEY_W - 10, "it should stop at the biggest size that fits"
    d = FakeDeck()
    from PIL import Image
    im = Image.open(io.BytesIO(render(face, d))).convert("RGB")     # the deck's own flip puts the text low
    rows = [[x for x in range(im.size[0]) if sum(im.getpixel((x, y))) > 450] for y in range(im.size[1])]
    ink = max(rows, key=lambda r: (r[-1] - r[0]) if r else 0, default=[])
    assert ink, "nothing was drawn on the key at all"
    assert min(ink) >= 4 and max(ink) < im.size[0] - 4, f"the word ran to the key's edge: cols {min(ink)}…{max(ink)}"


def test_a_face_that_is_sweeping_still_draws_a_jpeg():
    """The one thing an animation must not do to the wire: the deck takes JPEGs and nothing else, however many of
    them a second one key asks for."""
    face = Layout().plan(board_state(rows=[row(1, state="busy")]), 0.6)[0]
    assert face.edge is not None, "the face should have a bar to draw"
    img = render(face, FakeDeck())
    assert img[:2] == b"\xff\xd8", "an animating key didn't come out as JPEG"
    from PIL import Image
    opened = Image.open(io.BytesIO(img))
    assert opened.size == (72, 72) and opened.format == "JPEG"
    im = opened.convert("RGB")
    y0, y1, colour = face.edge
    want = deck._hex(colour)
    lo, hi = 71 - y1, 71 - y0            # the deck's own flip is already in these bytes: left is right, y is upside down
    on_edge = [im.getpixel((x, y)) for x in (71, 70, 69) for y in range(lo + 4, hi - 3)]
    assert any(sum(abs(a - b) for a, b in zip(px, want)) < 90 for px in on_edge), \
        "the sweep bar isn't down the left edge of the key the deck shows"


def test_a_page_key_and_a_thinking_key_draw():
    """Two faces the paging and the press-to-confirm added: an arrow with its page count under it, and a white key
    whose text is a colour rather than white."""
    nine = board_state(rows=[row(n) for n in range(1, 10)])
    faces = [Layout().plan(nine, 0.0, {}, 1)[0],
             Layout().plan(nine, 0.0, {}, 0, {row(1)["key"]: 0.0})[0]]
    for face in faces:
        img = render(face, FakeDeck())
        assert img[:2] == b"\xff\xd8", f"{face.lines} didn't come out as JPEG"
    assert deck.describe(faces[1]).endswith("ink=#1f77b4"), deck.describe(faces[1])


def test_the_image_comes_back_flipped_the_way_the_deck_wants():
    d = FakeDeck()
    from PIL import Image
    im = Image.open(io.BytesIO(render(KeyFace(bg="#1f77b4", top_bar=True), d)))
    assert im.getpixel((36, 70))[0] > 200, "the top bar drew where the deck's flip puts the bottom"
    assert im.getpixel((36, 1))[2] > 120, "the key's own colour didn't survive the flip"


# --------------------------------------------------------------------- the wire, against a board that answers it


@needs_unix
async def test_hello_round_trip(tmp_path):
    board = FakeBoard()
    await board.serve(tmp_path / "board.sock")
    client = BoardClient(tmp_path / "board.sock")
    client.connect()
    try:
        await until("the board to see a hello", lambda: any(r["cmd"] == "hello" for r in board.seen))
        assert client.connected, "the client didn't notice it was connected"
        assert client.state == board.state, "hello's state didn't reach the client"
        got = await client.arequest("state")
        assert got["ok"] and got["state"] == board.state
        assert all(str(r["id"]).isdigit() for r in board.seen), "requests need an id so replies can be matched"
    finally:
        await client.stop()
        await board.stop()


@needs_unix
async def test_a_pushed_state_reaches_the_subscriber(tmp_path):
    board = FakeBoard()
    await board.serve(tmp_path / "board.sock")
    client = BoardClient(tmp_path / "board.sock")
    seen: list[dict] = []
    client.subscribe(seen.append)
    client.connect()
    try:
        await until("the hello to land", lambda: client.state is not None)
        new = board_state(rows=[row(1, state="idle")])
        await board.push(new)
        await until("the pushed state", lambda: seen and seen[-1] == new)
        assert client.state == new
    finally:
        await client.stop()
        await board.stop()


@needs_unix
async def test_the_board_going_and_coming_back(tmp_path, monkeypatch):
    monkeypatch.setattr(deck, "RECONNECT_S", 0.15)             # the real retry is every 2 s; this test needn't wait
    path = tmp_path / "board.sock"
    board = FakeBoard()
    await board.serve(path)
    client = BoardClient(path)
    client.connect()
    try:
        await until("the first connection", lambda: client.connected)
        await board.drop_clients()
        await until("the client to notice it was dropped", lambda: not client.connected)
        got = await client.arequest("state")
        assert got["ok"] is False and "not connected" in got["error"]
        await board.serve(path)
        await until("the client to reconnect", lambda: client.connected, timeout=deck.RECONNECT_S + 4)
        await until("the second hello", lambda: sum(1 for r in board.seen if r["cmd"] == "hello") >= 2)
        assert client.state == board.state, "a reconnect re-sends hello, so the state comes with it"
    finally:
        await client.stop()
        await board.stop()


@needs_unix
async def test_an_error_reply_is_a_reply(tmp_path):
    board = FakeBoard()
    await board.serve(tmp_path / "board.sock")
    client = BoardClient(tmp_path / "board.sock")
    client.connect()
    try:
        await until("the first connection", lambda: client.connected)
        got = await client.arequest("slot", n=9)
        assert got["ok"] is False and got["error"] == "no such slot"
        assert client.connected, "the board saying no isn't a disconnect"
    finally:
        await client.stop()
        await board.stop()


class _Pipe:
    """The write end of a connection, for testing what the client does with the bytes a board sends."""

    def __init__(self):
        self.sent: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.sent.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Feeder:
    """One end of an in-memory connection: whatever is written arrives on the other side's StreamReader."""

    def __init__(self, into: asyncio.StreamReader):
        self.into = into

    def write(self, data: bytes) -> None:
        self.into.feed_data(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.into.feed_eof()


class TwinServer:
    """A board a client can connect to with no socket at all, answering like `FakeBoard` over `asyncio.start_unix_server`.
    This sandbox's kernel won't make a unix socket, so the reconnect loop — the part that can't be checked any other
    way — gets tested against this instead; on a normal host the socket tests above cover it too."""

    def __init__(self, board: FakeBoard):
        self.board = board
        self.up = False
        self.attempts = 0
        self._tasks: list[asyncio.Task] = []

    async def open_unix_connection(self, path):
        self.attempts += 1
        if not self.up:
            raise ConnectionRefusedError(f"no one is listening at {path}")
        to_client, from_client = asyncio.StreamReader(), asyncio.StreamReader()
        self._tasks.append(asyncio.create_task(self.board._conn(from_client, _Feeder(to_client))))
        return to_client, _Feeder(from_client)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()


async def test_the_client_keeps_up_with_a_board_that_comes_and_goes(monkeypatch):
    """hello, subscribe, a push, the board hanging up, and the client finding its way back — no socket needed."""
    monkeypatch.setattr(deck, "RECONNECT_S", 0.05)
    board = FakeBoard()
    twin = TwinServer(board)
    monkeypatch.setattr(deck.asyncio, "open_unix_connection", twin.open_unix_connection)
    client = BoardClient("/in/memory/board.sock")
    seen: list[dict] = []
    client.subscribe(seen.append)
    client.connect()
    try:
        twin.up = False
        await until("a retry while the board is down", lambda: twin.attempts >= 2, timeout=3)
        assert not client.connected
        twin.up = True
        await until("the board to be found", lambda: client.connected, timeout=3)
        await until("hello and subscribe", lambda: [r["cmd"] for r in board.seen] == ["hello", "subscribe"], timeout=3)
        assert client.state == board.state, "hello brings the table with it"
        new = board_state(rows=[row(1, state="idle")])
        await board.push(new)
        await until("the pushed state", lambda: seen and seen[-1] == new, timeout=3)
        got = await client.arequest("move", delta=1)
        sent = [{k: v for k, v in r.items() if k != "id"} for r in board.seen]
        assert got["ok"] is True and {"cmd": "move", "delta": 1} in sent, sent
        for w in list(board.conns):
            w.close()
        await until("the client to notice it was dropped", lambda: not client.connected, timeout=3)
        assert (await client.arequest("state"))["ok"] is False, "a dropped connection answers with the reason"
        await until("the client to reconnect", lambda: client.connected, timeout=3)
        assert client.state == board.state, "reconnecting re-sends hello, so the table comes with it"
    finally:
        await client.stop()
        await twin.stop()


async def test_replies_are_matched_and_pushes_are_not_missed():
    """The same contract without a socket — this sandbox's kernel won't make one, so the lines a board would send
    are handed to the reader by hand: a reply with an id answers its request, an event is a state, EOF is a drop."""
    client = BoardClient("/no/such/socket")
    client._loop = asyncio.get_running_loop()
    client._lock = asyncio.Lock()
    seen: list[dict] = []
    client.subscribe(seen.append)
    reader, client._writer, client.connected = asyncio.StreamReader(), _Pipe(), True
    pump = asyncio.create_task(client._pump(reader))
    try:
        asking = asyncio.create_task(client.arequest("slot", n=2))
        await until("the request to be written", lambda: client._writer.sent)
        req = json.loads(client._writer.sent[0])
        assert req["cmd"] == "slot" and req["n"] == 2 and req["id"]
        reader.feed_data(b"not json at all\n")                       # a board that talks nonsense is ignored
        reader.feed_data((json.dumps({"ok": True, "id": "999", "state": {"rows": []}}) + "\n").encode())
        reader.feed_data((json.dumps({"ok": True, "id": req["id"], "proto": 1}) + "\n").encode())
        assert (await asking)["ok"] is True, "the reply with the request's id didn't answer it"
        reader.feed_data((json.dumps({"event": "state", "state": board_state()}) + "\n").encode())
        await until("the pushed state", lambda: seen)
        assert client.state == board_state()
        reader.feed_data((json.dumps({"event": "state", "state": "not a table"}) + "\n").encode())
        await asyncio.sleep(0.05)
        assert len(seen) == 1, "only a state object is a new state"
        reader.feed_eof()
        await asyncio.wait_for(pump, timeout=5)                      # EOF is the board hanging up
    finally:
        pump.cancel()
        client._close()
    assert not client.connected


# ------------------------------------------------------------------- the app: a deck and a board, keeping in step


async def _app(state=None, fps: float = 0.0, clock=None, **kw) -> tuple[DeckApp, FakeDeck, FakeBoardSync]:
    d, b = FakeDeck(), FakeBoardSync(state if state is not None else board_state())
    kw = {**kw, "clock": clock} if clock is not None else kw
    app = DeckApp(d, b, fps=fps, log=lambda m: None, **kw)
    app.start()
    return app, d, b


async def test_the_first_draw_paints_every_key():
    app, d, _b = await _app()
    try:
        assert len(d.images) == 15 and d.paints == 15, "the first plan should paint the whole deck"
        assert d.cb is not None, "the deck was never told who to ask about a press"
    finally:
        await app.stop()


async def test_a_state_that_changes_one_row_repaints_only_that_row():
    """The face cache, on its own: the clock is held still, so the only thing that can move a key is the state."""
    app, d, b = await _app(clock=lambda: 30.0)
    try:
        assert d.paints == 15
        first = dict(d.images)
        b.cb(board_state(rows=[row(1), row(2, title="renamed"), row(3)]))
        assert d.paints == 16, f"one row changed but {d.paints - 15} keys were painted"
        assert d.images[1] != first[1] and d.images[0] == first[0] and d.images[14] == first[14]
        b.cb(board_state(rows=[row(1), row(2, title="renamed"), row(3)]))
        assert d.paints == 16, "the same state painted again"
    finally:
        await app.stop()


async def test_a_press_becomes_a_request():
    app, d, b = await _app()
    try:
        d.cb(d, 2, True)
        assert b.seen == [{"cmd": "slot", "n": 3}]
        d.cb(d, 11, True)
        d.cb(d, 13, False)                                    # a key coming back up sends nothing at all
        assert b.seen == [{"cmd": "slot", "n": 3}, {"cmd": "move", "delta": 1}]
    finally:
        await app.stop()


async def test_a_press_on_the_loop_thread_does_not_wait_for_itself():
    """The library calls the key callback on its own USB thread, but a press may arrive on the loop's own thread
    (a fake deck, a library that changes hands). Waiting there for an answer only this thread could deliver is a
    deadlock, so that one is sent without waiting."""
    class AsyncBoard(FakeBoardSync):
        async def arequest(self, cmd, **fields) -> dict:
            await asyncio.sleep(0)                              # a board that answers on a later turn
            self.seen.append({"cmd": cmd, **fields})
            return {"ok": True}

    d, b = FakeDeck(), AsyncBoard()
    app = DeckApp(d, b, fps=0.0, log=lambda m: None)
    app.start()
    try:
        await asyncio.wait_for(asyncio.to_thread(d.cb, d, 0, True), timeout=5)
        await until("the request to reach the board", lambda: b.seen == [{"cmd": "slot", "n": 1}], timeout=3)
        d.cb(d, 14, True)                                       # and here, on the loop thread itself
        await until("the second request", lambda: {"cmd": "end"} in b.seen, timeout=3)
    finally:
        await app.stop()


async def test_a_board_that_says_no_is_said_out_loud():
    class Rude(FakeBoardSync):
        def request(self, cmd, **fields):
            self.seen.append({"cmd": cmd, **fields})
            return {"ok": False, "error": "no such slot"}

    d, b = FakeDeck(), Rude()
    said: list[str] = []
    app = DeckApp(d, b, fps=0.0, log=said.append)
    app.start()
    try:
        d.cb(d, 0, True)                                        # a row the deck is showing: the board says no to it
        assert any("no such slot" in line for line in said), said
    finally:
        await app.stop()


async def test_a_tick_repaints_only_the_key_that_moves():
    """The animation clock, turned by the test instead of by `asyncio.sleep`: one busy key is repainted frame after
    frame and nothing else on the deck is — and a board that is all idle is not repainted at all, however many
    ticks go by. The face cache is what makes the second half cost nothing."""
    idle = [row(1, state="idle"), row(2, state="idle"), row(3, state="idle")]
    clock = {"t": 500.0}
    app, d, _b = await _app(state=board_state(rows=[row(1, state="busy"), *idle[1:]]), clock=lambda: clock["t"])
    try:
        assert d.paints == deck.KEY_COUNT and app._anim_count() == 1
        moved: set[int] = set()
        for _ in range(10):
            clock["t"] += 1 / deck.FPS
            before = dict(d.images)
            assert app._tick() == 1, "a busy session is one repainted key per frame"
            moved |= {k for k, v in d.images.items() if before.get(k) != v}
        assert moved == {0}, f"the ticks repainted keys {sorted(moved)} as well as the busy one"
    finally:
        await app.stop()

    app, d, _b = await _app(state=board_state(rows=idle), clock=lambda: clock["t"])
    try:
        for _ in range(10):
            clock["t"] += 1 / deck.FPS
            assert app._tick() == 0, "nothing is waiting or busy, so a tick has nothing to send"
        assert d.paints == deck.KEY_COUNT, f"an idle board was repainted {d.paints - deck.KEY_COUNT} more times"
    finally:
        await app.stop()


async def test_a_breathing_key_is_not_repainted_every_frame():
    """The breath is quantized, so some frames in a cycle are the shade the key already shows — which is the whole
    reason to quantize it: those ticks cost nothing."""
    clock = {"t": 12.0}
    app, d, _b = await _app(state=board_state(rows=[row(1, state="waiting"), row(2, state="idle")]),
                            clock=lambda: clock["t"])
    try:
        frames = int(deck.BREATHE_S * deck.FPS)
        painted = 0
        for _ in range(frames):
            clock["t"] += 1 / deck.FPS
            painted += app._tick()
        assert 0 < painted < frames, f"a {frames}-frame cycle repainted {painted} times: no quantization"
        assert app.faces[0].bg in (SLOT_BG[0], deck.WAIT_BG) or deck.SLOT_BG[0] < app.faces[0].bg, "still a colour"
    finally:
        await app.stop()


async def test_the_animation_timer_ticks_by_itself():
    """The one timer the animations have: `fps` frames a second, and it needs nothing else to make it move."""
    app, d, _b = await _app(state=board_state(rows=[row(1, state="busy")]), fps=20.0)
    try:
        painted = d.paints
        await until("the animation timer to repaint the busy key", lambda: d.paints > painted, timeout=1.0)
    finally:
        await app.stop()


async def test_a_switch_wipes_the_row_that_came_out():
    """`refresh` is what notices a switch: the state whose `visible_key` isn't the one before it puts a wipe on the
    row that gained it, and the tick drops it once the fade is done. It is keyed by session key, not by key index, so
    the page on show decides which key draws it — see `test_a_wipe_on_another_page_waits_for_its_page`."""
    clock = {"t": 100.0}
    rows = [row(1, visible=True), row(2), row(3)]
    app, d, b = await _app(state=board_state(rows=rows), clock=lambda: clock["t"])
    try:
        assert app.wipes == {}, "the first state the deck sees says nothing has moved"
        b.cb(board_state(rows=[row(1), row(2, title="renamed"), row(3)], visible_key=rows[0]["key"]))
        assert app.wipes == {}, "a row that was renamed isn't a switch"
        b.cb(board_state(rows=[row(1), row(2, visible=True), row(3)], visible_key=row(2)["key"]))
        assert list(app.wipes) == [row(2)["key"]], f"row 2 came out, so row 2 wipes: {app.wipes}"
        assert app.faces[1].bg == deck.WHITE, "at the start of a wipe the key is white"
        clock["t"] += deck.WIPE_S / 2
        app._tick()
        assert app.faces[1].bg != deck.WHITE and row(2)["key"] in app.wipes, "half way through it is neither"
        clock["t"] += deck.WIPE_S
        app._tick()
        assert app.wipes == {}, "a wipe that has run its WIPE_S is dropped, not left to fade forever"
        assert app.faces[1].bg != deck.WHITE
    finally:
        await app.stop()


# -------------------------------------------------------------------------------------------------- page layout
#
# `pages(n)` is the whole paging scheme in one pure function: which row, or which arrow, or nothing, sits on each of
# the top row's five keys, page by page. `Layout.plan` and `on_press` read it, and `DeckApp.page` is the only state
# it has, so everything below is read off those three.


def test_pages_lays_the_rows_out_over_the_top_row():
    """Five rows fit as they are. Past that a page pays an arrow for the ones it can't show: four rows and `▶`
    first, `◀` and three in the middle, `◀` and the remainder last."""
    assert deck.pages(0) == [[None, None, None, None, None]]
    assert deck.pages(4) == [[1, 2, 3, 4, None]]
    assert deck.pages(5) == [[1, 2, 3, 4, 5]]
    assert deck.pages(6) == [[1, 2, 3, 4, "next"], ["prev", 5, 6, None, None]]
    assert deck.pages(9) == [[1, 2, 3, 4, "next"], ["prev", 5, 6, 7, "next"], ["prev", 8, 9, None, None]]
    assert deck.pages(12) == [[1, 2, 3, 4, "next"], ["prev", 5, 6, 7, "next"], ["prev", 8, 9, 10, "next"],
                              ["prev", 11, 12, None, None]]
    assert deck.pages(13) == [[1, 2, 3, 4, "next"], ["prev", 5, 6, 7, "next"], ["prev", 8, 9, 10, "next"],
                              ["prev", 11, 12, 13, None]]


@pytest.mark.parametrize("n", list(range(14)))
def test_every_row_count_is_paged_without_losing_or_repeating_a_row(n):
    """Every row on exactly one page, every page five keys, and the arrows agreeing with themselves: `▶` on every
    page but the last, `◀` on every page after the first, and the pages running 4, then 3 at a time."""
    book = deck.pages(n)
    assert all(len(page) == 5 for page in book), f"{n} rows gave a page that isn't five keys: {book}"
    shown = [e for page in book for e in page if isinstance(e, int)]
    assert shown == list(range(1, n + 1)), f"{n} rows: the rows shown are {shown}"
    for i, page in enumerate(book):
        if i:
            assert page[0] == "prev", f"page {i} has no ◀: {page}"
        if i < len(book) - 1:
            assert page[4] == "next", f"page {i} has no ▶: {page}"
        assert "prev" not in page[1:] and "next" not in page[:4], f"page {i} has an arrow in the middle: {page}"
        if len(book) > 1 and i == 0:
            assert page[:4] == [1, 2, 3, 4], f"the first page of {n} rows is {page}"


def test_pages_of_five_or_fewer_rows_need_no_arrows():
    for n in range(6):
        page = deck.pages(n)[0]
        assert all(e is None or isinstance(e, int) for e in page), f"{n} rows shouldn't page: {deck.pages(n)}"


def test_a_board_of_six_pages_once_each_way():
    """Six rows: the first page is rows 1–4 and `▶` saying there is another, the second is `◀`, rows 5 and 6, and
    two keys with nothing on them."""
    six = board_state(rows=[row(n) for n in range(1, 7)])
    L = Layout()
    first = L.plan(six, 0.0, {}, 0)
    assert [first[i].lines[0] for i in range(5)] == ["1", "2", "3", "4", "▶"]
    assert first[4].bg == deck.ARROW_BG, "a page key wears the arrow keys' grey"
    assert first[4].lines == ("▶", "1/2"), f"▶ says nothing about where it goes: {first[4]}"
    assert [first[i].bg for i in range(4)] == list(SLOT_BG[:4])
    second = L.plan(six, 0.0, {}, 1)
    assert second[0].lines == ("◀", "2/2") and second[0].bg == deck.ARROW_BG
    assert [second[i].lines[0] for i in (1, 2)] == ["5", "6"]
    assert [second[i].bg for i in (1, 2)] == [SLOT_BG[1], SLOT_BG[2]], "a key keeps its colour wherever it lands"
    for i in (3, 4):
        assert second[i] == KeyFace(), f"key {i} on the last page should be a plain black face, not a spare number"


def test_page_one_of_nine_shows_three_rows_between_two_arrows():
    nine = board_state(rows=[row(n) for n in range(1, 10)])
    faces = Layout().plan(nine, 0.0, {}, 1)
    assert [faces[i].lines[0] for i in range(5)] == ["◀", "5", "6", "7", "▶"]
    assert faces[0].lines == ("◀", "2/3") and faces[4].lines == ("▶", "2/3"), "both arrows say which page this is"
    assert Layout().plan(nine, 0.0, {}, 2)[0].lines == ("◀", "3/3")


def test_a_paged_key_names_the_row_it_shows():
    """The number on the key is the row's — `slot n` is the same `n` the board numbers its rows with, and past nine
    it is a number the board itself draws nowhere. The bars, the dot and the breath come with the row."""
    nine = board_state(rows=[row(n, visible=(n == 6), selected=(n == 6), state="waiting" if n == 6 else "idle")
                              for n in range(1, 10)])
    shown = Layout().plan(nine, 0.0, {}, 1)[2]
    assert shown.lines == ("6", "session 6")
    assert shown.top_bar and shown.bottom_bar and shown.border == deck.WAIT_BG


def test_the_page_on_show_clamps_to_the_rows_there_are():
    """Ask for page 7 of 3 and you get the last one, not a crash or a blank top row."""
    nine = board_state(rows=[row(n) for n in range(1, 10)])
    assert Layout().plan(nine, 0.0, {}, 7)[1].lines[0] == "8"
    assert Layout().on_press(nine, 1, 7) == [{"cmd": "slot", "n": 8}], "and a press reads the page it clamped to"


def test_a_picker_never_pages():
    """Options 1–5 on keys 0–4, whatever the rows are doing underneath: paging an option list would be a list where
    two of the keys mean the same thing twice."""
    nine = board_state(rows=[row(n) for n in range(1, 10)], modal="picker",
                       options=[{"n": 1, "label": "one"}, {"n": 2, "label": "two"}])
    for page in (0, 1):
        faces = Layout().plan(nine, 0.0, {}, page)
        assert faces[0].lines == ("1", "one") and faces[1].lines == ("2", "two")
        assert faces[4] == KeyFace(), f"page {page} put something where option 5 goes"
        assert Layout().on_press(nine, 4, page) == [{"cmd": "pick", "n": 5}]


# ----------------------------------------------------------------------------------------------- what a press sends


def test_a_paged_key_asks_for_the_row_it_shows():
    nine = board_state(rows=[row(n) for n in range(1, 10)])
    L = Layout()
    assert L.on_press(nine, 1, 1) == [{"cmd": "slot", "n": 5}], "key 1 of page 1 shows row 5"
    assert L.on_press(nine, 3, 1) == [{"cmd": "slot", "n": 7}]
    assert L.on_press(nine, 1, 2) == [{"cmd": "slot", "n": 8}]
    assert L.on_press(nine, 4, 0) == [{"deck": "page", "delta": 1}], "▶ is the deck's own: no cmd, so no board"
    assert L.on_press(nine, 0, 1) == [{"deck": "page", "delta": -1}]
    assert L.on_press(nine, 3, 2) == [], "an empty key on the last page does nothing"
    five = board_state(rows=[row(n) for n in range(1, 6)])
    assert L.on_press(five, 4, 0) == [{"cmd": "slot", "n": 5}], "five rows or fewer page as they always did"
    assert L.on_press(board_state(), 4, 0) == [], "and a free slot key on a short board still does nothing"


# ---------------------------------------------------------------------------------------------------- the app


async def _six() -> tuple[DeckApp, FakeDeck, FakeBoardSync]:
    """A deck on a board of six rows: two pages, and nothing moving on either."""
    return await _app(board_state(rows=[row(n, state="idle") for n in range(1, 7)]), clock=lambda: 40.0)


async def test_the_page_key_flips_the_page_and_sends_nothing():
    """`▶` and `◀` are the deck's own business: the board is never asked, and the top row comes round."""
    app, d, b = await _six()
    try:
        assert app.page == 0
        assert d.images[4] == render(Layout().plan(app.state, 40.0, {}, 0)[4], d)
        app.press(4)
        assert b.seen == [], f"the board was told about a page turn: {b.seen}"
        assert app.page == 1 and d.images[0] == render(Layout().plan(app.state, 40.0, {}, 1)[0], d)
        app.press(4)
        assert app.page == 1, "▶ on the last page has nowhere to go"
        app.press(0)
        assert app.page == 0 and b.seen == []
        app.press(0)
        assert app.page == 0, "◀ on the first page is a no-op"
    finally:
        await app.stop()


async def test_a_paged_key_opens_the_row_it_shows():
    app, d, b = await _six()
    try:
        app.press(4)                                              # ▶ onto page 1
        b.seen.clear()
        app.press(1)                                              # the key showing row 5
        assert b.seen == [{"cmd": "slot", "n": 5}], b.seen
    finally:
        await app.stop()


async def test_the_page_follows_the_highlight_and_only_the_highlight():
    """▲▼ walking the highlight off the page brings the page along; a page the operator turned by hand stays put
    until the highlight actually moves."""
    rows = [row(n, state="idle") for n in range(1, 10)]
    app, d, b = await _app(board_state(rows=rows, selected_key=rows[0]["key"]), clock=lambda: 60.0)
    try:
        assert app.page == 0
        b.cb(board_state(rows=rows, selected_key=rows[6]["key"]))   # the highlight is on row 7 now: page 1 shows it
        assert app.page == 1, "the highlight walked off the page and the page stayed"
        app.press(4)                                                # ▶ by hand, onto page 2
        assert app.page == 2
        b.cb(board_state(rows=rows, selected_key=rows[6]["key"], modal=None))
        assert app.page == 2, "a state that didn't move the highlight shouldn't move the page back"
        b.cb(board_state(rows=rows, selected_key=rows[0]["key"]))
        assert app.page == 0, "and one that moved it back does"
    finally:
        await app.stop()


async def test_the_page_clamps_when_the_rows_shrink():
    rows = [row(n, state="idle") for n in range(1, 10)]
    app, d, b = await _app(board_state(rows=rows, selected_key=rows[0]["key"]), clock=lambda: 61.0)
    try:
        app.press(4)
        app.press(4)
        assert app.page == 2
        b.cb(board_state(rows=rows[:4], selected_key=rows[0]["key"]))
        assert app.page == 0, "four rows are one page: the deck was showing a page that isn't there"
        assert d.images[4] == render(Layout().plan(app.state, 61.0, {}, 0)[4], d)
    finally:
        await app.stop()


async def test_a_wipe_on_another_page_waits_for_its_page():
    """A fade belongs to the row, not to the key index it happened to be on: page 2 draws row 8's fade, and page 1
    draws no fade at all rather than one on the wrong key."""
    rows = [row(n, state="idle") for n in range(1, 10)]
    app, d, b = await _app(board_state(rows=rows, selected_key=rows[0]["key"], visible_key=rows[0]["key"]),
                           clock=lambda: 70.0)
    try:
        app.press(4)
        app.press(4)                                                # page 3, whose first row is row 8
        assert app.page == 2, app.page
        page = app.page
        b.cb(board_state(rows=rows, selected_key=rows[0]["key"], visible_key=rows[7]["key"]))
        assert list(app.wipes) == [rows[7]["key"]], f"keyed by row, not by key: {app.wipes}"
        faces = Layout().plan(app.state, 70.0, app._key_wipes(), page)
        assert faces[1].bg == deck.WHITE, f"row 8's fade isn't on the key showing it: {faces[1]}"
        app.page = 1 - page                                          # the same moment, the other page
        other = Layout().plan(app.state, 70.0, app._key_wipes(), app.page)
        assert all(other[i].bg == SLOT_BG[i] for i in range(5) if other[i].bg != deck.ARROW_BG), \
            f"a fade from another page leaked onto this one: {[other[i].bg for i in range(5)]}"
    finally:
        await app.stop()


async def test_only_the_page_on_show_is_worth_animating():
    """A busy row on a page nobody is looking at costs nothing: the tick looks, and there is no frame to be had."""
    rows = [row(n, state="idle") for n in range(1, 10)]
    rows[6] = row(7, state="busy")                                   # row 7, on page 1
    clock = {"t": 80.0}
    app, d, b = await _app(board_state(rows=rows, selected_key=rows[0]["key"]), clock=lambda: clock["t"])
    try:
        assert app.page == 0 and Layout().animating(app.state, {}, 0) is False
        assert Layout().animating(app.state, {}, 1) is True
        for _ in range(5):
            clock["t"] += 1 / deck.FPS
            assert app._tick() == 0, "a row on another page is putting keys on the wire"
        assert d.paints == deck.KEY_COUNT
        assert app._anim_count() == 0
        app.page = 1
        app._tick()                                                 # the page itself is new, so it paints
        assert app._anim_count() == 1, f"the busy row isn't the one moving: {app._anim_count()}"
        frames = 0
        for _ in range(8):
            clock["t"] += 1 / deck.FPS
            frames += app._tick()
        assert frames >= 1, "the same row, on the page that shows it, is a frame"
    finally:
        await app.stop()


async def test_a_press_pulses_until_the_board_confirms_the_switch():
    """The key answers the finger rather than the state: white at once with its own colour as text, still thinking
    two seconds on, and only when the pane actually comes round does it fade to normal."""
    rows = [row(n, state="idle") for n in range(1, 7)]
    clock = {"t": 90.0}
    app, d, b = await _app(board_state(rows=rows, selected_key=rows[0]["key"], visible_key=rows[0]["key"]),
                           clock=lambda: clock["t"])
    try:
        app.press(4)                                                 # onto page 1, which shows row 5 on key 1
        b.seen.clear()
        app.press(1)
        assert list(app.pending) == [rows[4]["key"]], app.pending
        assert app.faces[1].bg == deck.WHITE, f"the key didn't answer: {app.faces[1]}"
        assert app.faces[1].ink == SLOT_BG[1], "white on white says nothing: the text wears the key's own colour"
        assert app.faces[1].lines == ("5", "session 5")
        assert Layout().animating(app.state, {}, 1, app.pending) is True
        clock["t"] += 2.0                                            # still thinking: the board hasn't swapped it in
        app._tick()
        assert list(app.pending) == [rows[4]["key"]], app.pending
        assert app.faces[1].bg != SLOT_BG[1]
        b.cb(board_state(rows=[rows[0], rows[1], rows[2], rows[3],
                               {**rows[4], "visible": True}, rows[5]],
                         selected_key=rows[0]["key"], visible_key=rows[4]["key"]))
        assert app.pending == {}, "the pane came round, so there is nothing left to wait on"
        assert list(app.wipes) == [rows[4]["key"]], f"and now it fades: {app.wipes}"
        assert app.faces[1].bg == deck.WHITE, "at the top of the fade it is still white"
        clock["t"] += deck.WIPE_S
        app._tick()
        assert app.wipes == {} and app.faces[1].bg == SLOT_BG[1], "the fade ends on the face the key would show"
    finally:
        await app.stop()


async def test_a_switch_the_board_never_answers_gives_up():
    """A pulse can't be left running on the off chance: after PENDING_S the key goes back the way it came, and a
    `slot` the board says no to goes back at once."""
    rows = [row(n, state="idle") for n in range(1, 4)]
    clock = {"t": 100.0}
    app, d, b = await _app(board_state(rows=rows), clock=lambda: clock["t"])
    try:
        app.press(1)
        assert list(app.pending) == [rows[1]["key"]]
        clock["t"] += deck.PENDING_S
        app._tick()
        assert app.pending == {}, "a switch the board never answered is still pulsing"
        assert app.faces[1].bg == SLOT_BG[1], "the key went back to its own colour"
        app._send(2, [{"cmd": "slot", "n": 3}])                       # and one it says no to is over at once
        assert app.pending == {}
    finally:
        await app.stop()


async def test_a_refused_switch_stops_pulsing_at_once():
    """A `slot` the board says no to is a switch that isn't coming: the key was white when the answer came back and
    is its own colour again by the time that answer has been read."""
    class Rude(FakeBoardSync):
        def __init__(self, state):
            super().__init__(state)
            self.at_answer: KeyFace | None = None
            self.app = None

        def request(self, cmd, **fields):
            self.seen.append({"cmd": cmd, **fields})
            self.at_answer = self.app.faces.get(1)                # what the key showed as the board answered
            return {"ok": False, "error": "no such slot"}

    rows = [row(n, state="idle") for n in range(1, 4)]
    said: list[str] = []
    board = Rude(board_state(rows=rows))
    app = DeckApp(FakeDeck(), board, fps=0.0, log=said.append, clock=lambda: 110.0)
    board.app = app
    app.start()
    try:
        app.press(1)
        assert any("no such slot" in line for line in said), said
        assert board.at_answer is not None and board.at_answer.bg == deck.WHITE, "the press hadn't armed the key"
        assert app.pending == {}, "the board said no, so there is nothing left to wait on"
        assert app.faces[1].bg == SLOT_BG[1] and app.faces[1].ink is None
    finally:
        await app.stop()


async def test_a_switch_the_deck_didnt_ask_for_still_fades():
    """A state that puts another row in the pane with nothing pending — the operator used the keyboard — gets the
    plain fade it always got, with no pulse in front of it."""
    rows = [row(n, state="idle") for n in range(1, 4)]
    clock = {"t": 120.0}
    app, d, b = await _app(board_state(rows=rows, visible_key=rows[0]["key"]), clock=lambda: clock["t"])
    try:
        b.cb(board_state(rows=rows, visible_key=rows[2]["key"]))
        assert app.pending == {}, "nothing was pressed, so there is nothing to wait on"
        assert list(app.wipes) == [rows[2]["key"]], app.wipes
        assert app.faces[2].bg == deck.WHITE
        clock["t"] += deck.WIPE_S
        app._tick()
        assert app.faces[2].bg == SLOT_BG[2] and app.wipes == {}
    finally:
        await app.stop()


async def test_a_state_showing_another_row_leaves_the_key_thinking():
    """A state whose pane isn't the row this key asked for hasn't answered the press: the key keeps pulsing, and the
    row that did come out fades the way a switch that came from the board does."""
    rows = [row(n, state="idle") for n in range(1, 4)]
    clock = {"t": 130.0}
    app, d, b = await _app(board_state(rows=rows, visible_key=rows[0]["key"]), clock=lambda: clock["t"])
    try:
        app.press(1)                                              # ask for row 2
        assert list(app.pending) == [rows[1]["key"]]
        b.cb(board_state(rows=rows, visible_key=rows[2]["key"]))   # the board went to row 3 instead
        assert list(app.pending) == [rows[1]["key"]], f"row 2 wasn't the pane, so it is still waiting: {app.pending}"
        assert list(app.wipes) == [rows[2]["key"]], app.wipes
        assert app.faces[1].bg != SLOT_BG[1], "the key that asked stopped saying so"
    finally:
        await app.stop()


async def test_stopping_resets_the_deck_and_closes_it():
    app, d, _b = await _app()
    await app.stop()
    assert d.reset_called and d.closed


async def test_the_heartbeat_says_the_deck_is_alive():
    """`ctx deck` runs for hours, so it prints a line of proof now and then — and repaints in passing, which the
    face cache makes cost nothing when nothing changed. The line also counts what the animations have been doing,
    so a deck that has gone quiet can be told apart from one that has stopped."""
    d, b = FakeDeck(), FakeBoardSync(board_state(rows=[row(1, state="idle"), row(2, state="idle")]))
    said: list[str] = []
    app = DeckApp(d, b, fps=0.0, log=lambda m: None, heartbeat_s=0.05, echo=said.append)
    app.start()
    try:
        await until("a heartbeat line", lambda: any("keys" in line for line in said), timeout=2)
        assert d.paints == deck.KEY_COUNT, "the heartbeat's redraw found nothing changed, so it painted nothing extra"
    finally:
        await app.stop()
    line = " ".join(said)
    assert "the board" in line
    assert "in the last" in line and "animating" in line, f"the heartbeat says nothing about the animations: {line}"
    assert "0 keys animating" in line, f"an idle board is not animating: {line}"


def test_no_deck_attached_waits_instead_of_exiting(monkeypatch, capfd):
    """An unattached deck is the ordinary state of a machine before anyone plugs in — `usbipd attach` comes later, by
    hand. So it is waited in, said once, and the process stays up with its board connection running."""
    started: list = []

    def nope(*_a, **_kw):
        raise RuntimeError("no Stream Deck found")

    real_start = deck._start

    async def _start_once(app, loop) -> None:
        await real_start(app, loop)                         # the app is what finds there is nothing to paint on
        started.append(app)
        loop.call_later(0.05, loop.stop)                    # main() runs until Ctrl-C; this test stops there

    monkeypatch.setattr(deck, "open_deck", nope)
    monkeypatch.setattr(deck, "_start", _start_once)
    assert deck.main(["--sock", str(Path(tempfile.mkdtemp()) / "board.sock")]) == 0, \
        "no deck attached must not be an exit"
    err = capfd.readouterr().err
    assert err.count("no Stream Deck found: waiting for one (attach it with usbipd)") == 1, err
    assert started and started[0].deck is None, "the app should be holding nothing, waiting for the supervisor"
    assert started[0].open_deck is nope, "and the supervisor should be the one that looks"


def test_no_extra_installed_says_how_to_get_it(tmp_path, capfd, monkeypatch):
    monkeypatch.setitem(sys.modules, "StreamDeck", None)       # `import StreamDeck` now raises ImportError
    assert deck.main(["--sock", str(tmp_path / "nope.sock")]) == 2
    assert "install with: uv pip install -e '.[deck]'" in capfd.readouterr().out


# ------------------------------------------------------------------------------------------------- scroll layout
#
# The other way the top row can carry more rows than it has keys for: `scroll_window(n, o)` is the whole scheme in
# one pure function — a window of consecutive rows that one arrow moves along by exactly one row — and `Layout.plan`,
# `on_press` and `DeckApp.offset` read it the way they read `pages()`.


def test_four_rows_scroll_as_they_stand_with_no_arrows():
    four = board_state(rows=[row(n, state="idle") for n in range(1, 5)])
    assert deck.scroll_window(4, 0) == ([1, 2, 3, 4, None], 0), "five rows or fewer need no arrows at all"
    faces = Layout().plan(four, 0.0, {}, 0, None, "scroll", 0)
    assert [faces[i].lines[0] for i in range(4)] == ["1", "2", "3", "4"]
    assert faces[4].dim and faces[4].lines == ("5",), "a board that fits is the same top row it always had"
    assert Layout().on_press(four, 3, 0, "scroll", 0) == [{"cmd": "slot", "n": 4}]
    assert Layout().on_press(four, 4, 0, "scroll", 0) == [], "a free slot key says nothing, scrolled or paged"


@pytest.mark.parametrize("offset,want", [(0, [1, 2, 3, 4, "next"]), (1, ["prev", 2, 3, 4, "next"]),
                                         (2, ["prev", 3, 4, 5, 6])])
def test_six_rows_scroll_one_row_to_the_far_right(offset, want):
    assert deck.scroll_window(6, offset) == (want, min(offset, 2))


def test_nine_rows_scroll_one_at_a_time_and_end_on_the_last_four():
    assert deck.scroll_window(9, 0) == ([1, 2, 3, 4, "next"], 0)
    assert deck.scroll_window(9, 3) == (["prev", 4, 5, 6, "next"], 3)
    assert deck.scroll_window(9, 5) == (["prev", 6, 7, 8, 9], 5), "the far right drops ▶ and shows the last four"


def test_a_scroll_offset_past_either_end_is_pulled_back_to_the_nearest_window():
    assert deck.scroll_window(9, 99) == (["prev", 6, 7, 8, 9], 5), "the last window is the one ending on row 9"
    assert deck.scroll_window(9, -3) == ([1, 2, 3, 4, "next"], 0)
    assert deck.scroll_window(0, 4) == ([None] * 5, 0), "and a board with no rows has no window to move"


@pytest.mark.parametrize("n", list(range(14)))
def test_every_offset_shows_a_window_of_consecutive_rows_that_one_arrow_moves_by_one(n):
    """The scheme, rather than the table: five keys, no repeats, the rows on show one after another, and every press
    that has somewhere to go landing exactly one row along. Nothing is ever shown that isn't a row there is."""
    last = max(0, n - 4)
    for o in range(-1, last + 2):
        entries, at = deck.scroll_window(n, o)
        assert len(entries) == 5 and at == max(0, min(o, last)), f"{n} rows at {o}: {entries} at {at}"
        assert all(e is None or isinstance(e, int) or e in ("prev", "next") for e in entries)
        assert sum(1 for e in entries if e == "prev") <= 1 and sum(1 for e in entries if e == "next") <= 1
        rows = [e for e in entries if isinstance(e, int)]
        if rows:
            assert rows == list(range(rows[0], rows[0] + len(rows))), f"{n} at {o}: {entries}"
        assert all(1 <= r <= n for r in rows), f"{n} at {o} shows a row that isn't there: {entries}"
        if n > 5:
            assert entries[0] == "prev" if at else entries[4] == "next", f"{n} at {at} has no arrow: {entries}"
        for delta in (-1, 1):
            nxt, moved = deck.scroll_window(n, at + delta)
            assert moved == max(0, min(at + delta, last)), f"{n} at {at} {'◀' if delta < 0 else '▶'}: {nxt}"


def test_a_scrolled_window_says_which_rows_are_under_the_keys():
    """A scroll has no page numbers, so the line under an arrow counts rows instead: the ones on screen, of how many.
    Both arrows on one window say the same thing, because they are looking at the same window."""
    nine = board_state(rows=[row(n, state="idle") for n in range(1, 10)])
    L = Layout()
    middle = L.plan(nine, 0.0, {}, 0, None, "scroll", 3)
    assert [middle[i].lines[0] for i in range(5)] == ["◀", "4", "5", "6", "▶"]
    assert middle[0].lines == ("◀", "4–6/9") and middle[4].lines == ("▶", "4–6/9")
    assert [middle[i].bg for i in (1, 2, 3)] == list(SLOT_BG[1:4]), "a key keeps its colour wherever it lands"
    assert L.plan(nine, 0.0, {}, 0, None, "scroll", 0)[4].lines == ("▶", "1–4/9")
    assert L.plan(nine, 0.0, {}, 0, None, "scroll", 5)[0].lines == ("◀", "6–9/9")
    assert L.plan(nine, 0.0, {}, 0, None, "scroll", 7)[0].lines == ("◀", "6–9/9"), "and it says so clamped too"


def test_a_scrolled_key_names_the_row_it_shows():
    nine = board_state(rows=[row(n, visible=(n == 5), selected=(n == 5), state="waiting" if n == 5 else "idle")
                             for n in range(1, 10)])
    shown = Layout().plan(nine, 0.0, {}, 0, None, "scroll", 3)[2]
    assert shown.lines == ("5", "session 5")
    assert shown.top_bar and shown.bottom_bar and shown.border == deck.WAIT_BG
    assert shown.bg == SLOT_BG[2], "the breath is on the key's own colour, wherever the window has put the row"


def test_only_the_window_on_show_is_worth_animating_when_scrolled():
    """The same rule the pages have, on the other axis: a busy row outside the window costs nothing, and the window
    that shows it is a frame."""
    rows = [row(n, state="idle") for n in range(1, 10)]
    rows[6] = row(7, state="busy")
    assert Layout().animating(board_state(rows=rows), {}, 0, None, "scroll", 0) is False
    assert Layout().animating(board_state(rows=rows), {}, 0, None, "scroll", 4) is True


def test_a_scrolled_press_asks_for_its_row_or_moves_the_window():
    nine = board_state(rows=[row(n, state="idle") for n in range(1, 10)])
    L = Layout()
    assert [L.on_press(nine, k, 0, "scroll", 3) for k in (1, 2, 3)] == \
        [[{"cmd": "slot", "n": 4}], [{"cmd": "slot", "n": 5}], [{"cmd": "slot", "n": 6}]]
    assert L.on_press(nine, 4, 0, "scroll", 3) == [{"deck": "scroll", "delta": 1}], "▶ is the deck's own: no cmd"
    assert L.on_press(nine, 0, 0, "scroll", 3) == [{"deck": "scroll", "delta": -1}]
    assert L.on_press(nine, 4, 0, "scroll", 5) == [{"cmd": "slot", "n": 9}], \
        "at the far right the key that used to be ▶ is row 9, so it opens it"
    assert L.on_press(nine, 0, 0, "scroll", 0) == [{"cmd": "slot", "n": 1}], "at the left end ◀ isn't a key yet"
    assert L.on_press(nine, 11, 0, "scroll", 3) == [{"cmd": "move", "delta": 1}], "and the rest is unchanged"


async def test_the_scroll_key_moves_the_window_and_sends_nothing():
    """`▶` and `◀` scrolled: one row along per press, and the board never hears about it."""
    rows = [row(n, state="idle") for n in range(1, 10)]
    app, d, b = await _app(board_state(rows=rows, selected_key=rows[0]["key"]), clock=lambda: 62.0,
                           **_settings({"top_row": "scroll"}))
    try:
        assert (app.mode, app.offset) == ("scroll", 0)
        assert d.images[4] == render(Layout().plan(app.state, 62.0, {}, 0, None, "scroll", 0)[4], d)
        app.press(4)
        assert b.seen == [], f"the board was told about a scroll: {b.seen}"
        assert app.offset == 2 and [app.faces[i].lines[0] for i in range(5)] == ["◀", "3", "4", "5", "▶"]
        app.press(0)
        assert app.offset == 0 and b.seen == []
        b.seen.clear()
        app.press(0)
        assert app.offset == 0, "at the left end there is no ◀ key: key 0 is row 1, and opening it is all it does"
        assert b.seen == [{"cmd": "slot", "n": 1}], b.seen
        b.seen.clear()
        for _ in range(4):                                        # two rows the first press, then one a press, to the far right
            app.press(4)
        assert app.offset == 5, f"the window ran past the far right: {app.offset}"
        assert [app.faces[i].lines[0] for i in range(5)] == ["◀", "6", "7", "8", "9"]
        assert b.seen == []
        b.seen.clear()
        app.press(4)                                              # the key that was ▶ is row 9 now: it opens it
        assert b.seen == [{"cmd": "slot", "n": 9}] and app.offset == 5, b.seen
    finally:
        await app.stop()


async def test_a_scrolled_key_opens_the_row_it_shows():
    rows = [row(n, state="idle") for n in range(1, 10)]
    app, d, b = await _app(board_state(rows=rows, selected_key=rows[0]["key"]), clock=lambda: 63.0,
                           **_settings({"top_row": "scroll"}))
    try:
        app.press(4)                                              # ▶: the first step goes two, to rows 3–5 and ▶
        b.seen.clear()
        app.press(1)                                              # the key showing row 3
        assert b.seen == [{"cmd": "slot", "n": 3}], b.seen
    finally:
        await app.stop()


async def test_the_scroll_follows_the_highlight_by_the_least_it_can():
    """▲▼ walking the highlight out of the window brings the window along — by the least it can move, which is the
    window that puts the row on the last key it has for rows, and not one row further than it has to."""
    rows = [row(n, state="idle") for n in range(1, 10)]
    app, d, b = await _app(board_state(rows=rows, selected_key=rows[0]["key"]), clock=lambda: 64.0,
                           **_settings({"top_row": "scroll"}))
    try:
        assert app.offset == 0
        b.cb(board_state(rows=rows, selected_key=rows[6]["key"]))  # row 7: o=4 shows 5 6 7, so no further
        assert app.offset == 4, f"row 7 needed o=4: {[app.faces[i].lines[0] for i in range(5)]}"
        assert [app.faces[i].lines[0] for i in range(5)] == ["◀", "5", "6", "7", "▶"]
        b.cb(board_state(rows=rows, selected_key=rows[5]["key"]))  # row 6, already on screen: the window stays
        assert app.offset == 4, "a highlight the window already shows shouldn't move it"
        b.cb(board_state(rows=rows, selected_key=rows[1]["key"]))  # and back to the left, again only as far as needed
        assert app.offset == 1, "o=1 shows rows 2–4: o=0 would be one further than the row needed"
        b.cb(board_state(rows=rows, selected_key=rows[8]["key"]))  # row 9: only the far right shows it
        assert app.offset == 5
    finally:
        await app.stop()


async def test_the_scroll_clamps_when_the_rows_shrink():
    rows = [row(n, state="idle") for n in range(1, 10)]
    app, d, b = await _app(board_state(rows=rows, selected_key=rows[0]["key"]), clock=lambda: 65.0,
                           **_settings({"top_row": "scroll"}))
    try:
        app.offset = 5
        b.cb(board_state(rows=rows[:4], selected_key=rows[0]["key"]))
        assert app.offset == 0, "four rows are no window at all: the deck was showing rows that aren't there"
        assert [app.faces[i].lines[0] for i in range(4)] == ["1", "2", "3", "4"]
    finally:
        await app.stop()


# --------------------------------------------------------------------------------------- the settings file, live
#
# One JSON file the board's `D` screen writes (`common/deck_settings.py`, docs/deck-settings.md) and the deck reads
# at start and once a second after that. A test hands the app a path and a clock, so "once a second" is 20 `_tick()`s
# and a rewrite of the file is the only thing that can change how the deck behaves.


def _settings(values: dict) -> dict:
    """A settings file in a temporary directory of its own — every call its own file, so one test's `top_row` can
    never be read back by the next one — and the two `DeckApp` arguments that point a deck at it: the path, and no
    `--brightness`, which is what makes the file the deck's only word on the lights."""
    where = Path(tempfile.mkdtemp(prefix="ctx-deck-settings-"))
    atexit.register(shutil.rmtree, str(where), True)
    path = where / "deck-settings"
    save_settings(path, values)
    return {"settings_path": path, "brightness_override": None}


async def test_a_file_that_says_scroll_makes_the_top_row_scroll():
    """The settings the deck starts with are the file's, not the defaults: the first `▶` is already a scroll."""
    rows = [row(n, state="idle") for n in range(1, 10)]
    app, d, b = await _app(board_state(rows=rows, selected_key=rows[0]["key"]), clock=lambda: 66.0,
                           **_settings({"top_row": "scroll"}))
    try:
        assert (app.mode, app.offset) == ("scroll", 0), "the file's mode, before a single key has been painted"
        assert app.settings["top_row"] == "scroll"
        assert app.faces[4].lines == ("▶", "1–4/9"), "the first paint was laid out in pages all the same"
        app.press(4)
        assert app.offset == 2 and b.seen == [], f"a scroll reached the board: {b.seen}"
    finally:
        await app.stop()


async def test_a_settings_change_applies_on_the_next_second():
    """The board's `D` screen rewrote the file while the deck was running: the next second's tick reads it, switches
    the mode, moves the window to the highlighted row, and says so in one line."""
    rows = [row(n, state="idle") for n in range(1, 10)]
    settings = _settings({"top_row": "pages"})
    said: list[str] = []
    d = FakeDeck()
    app = DeckApp(d, FakeBoardSync(board_state(rows=rows)), fps=20.0, log=said.append, **settings)
    app.start()
    try:
        assert app.mode == "pages" and d.paints == deck.KEY_COUNT
        app._check_settings()
        assert said == [], f"a file nobody changed said something: {said}"
        assert d.paints == deck.KEY_COUNT, "an untouched file cost a repaint every second"
        save_settings(Path(app.settings_path), {"top_row": "scroll"})      # the board writes it: a new mtime
        app._check_settings()
        assert app.mode == "scroll", "a rewritten file didn't reach the deck"
        assert app.offset == 5, f"the window should be the one showing row 9, the highlighted row: {app.offset}"
        assert rows[-1]["key"] in [r["key"] for r in app._page_rows()]
        assert sum(1 for line in said if "scroll" in line) == 1, said
        app._check_settings()
        assert sum(1 for line in said if "scroll" in line) == 1, f"the same file said it twice: {said}"
        save_settings(Path(app.settings_path), {"top_row": "pages"})
        for _ in range(deck.SETTINGS_TICKS):                                # the tick finds it by itself, 1 s in 20
            app._tick()
        assert app.mode == "pages", "the animation tick never looked at the file"
        assert app.page == 2, f"pages mode centres on the highlighted row too: page {app.page}"
    finally:
        await app.stop()


async def test_a_brightness_in_the_file_lights_the_keys_it_says():
    """The file's number reaches the device, once per change rather than once a second."""
    settings = _settings({"brightness": 60})
    d = FakeDeck()
    said: list[str] = []
    app = DeckApp(d, FakeBoardSync(), fps=0.0, log=said.append, **settings)
    app.start()
    try:
        assert d.brightness == 60, "start didn't take the file's brightness"
        save_settings(Path(app.settings_path), {"brightness": 30})
        app._check_settings()
        assert d.brightness == 30, "the file's brightness never reached the deck"
        assert sum(1 for line in said if "brightness" in line) == 1, said
        app._check_settings()
        assert sum(1 for line in said if "brightness" in line) == 1, f"the same brightness said twice: {said}"
    finally:
        await app.stop()


async def test_a_brightness_said_on_the_command_line_beats_the_file():
    """`--brightness` is the operator speaking now; the file is the board's `D` screen speaking for every run. Now
    wins for the run, at start and on every check after it — the file can't take the lights back."""
    settings = _settings({"brightness": 20})
    d = FakeDeck()
    app = DeckApp(d, FakeBoardSync(), fps=0.0, log=lambda m: None,
                  settings_path=settings["settings_path"], brightness_override=80)
    app.start()
    try:
        assert d.brightness == 80, "--brightness lost to the file"
        save_settings(Path(app.settings_path), {"brightness": 10})
        app._check_settings()
        assert d.brightness == 80, "a rewritten file overrode an explicit --brightness"
    finally:
        await app.stop()


async def test_animations_off_leaves_the_faces_still_but_the_state_moving():
    """`animations: false` is the quiet deck: no breath, no sweep, no white, no pulse drawn — but a switch this deck
    asked for is still tracked until the board answers it, and a state that changes still repaints its keys."""
    st = board_state(rows=[row(1, state="waiting"), row(2, state="busy"), row(3, state="idle")])
    clock = {"t": 200.0}
    app, d, b = await _app(st, clock=lambda: clock["t"], **_settings({"animations": False}))
    try:
        assert app.anim_on is False
        assert app.layout.animating(app.state, app._key_wipes(), app.page, app.pending, app.mode, app.offset,
                                    app.anim_on) is False, "the deck still thinks its own clock is moving"
        painted = d.paints
        for _ in range(2 * deck.FPS):
            clock["t"] += 1 / deck.FPS
            assert app._tick() == 0, "a static deck put a key on the wire"
        assert d.paints == painted, f"the still faces were repainted {d.paints - painted} times"
        assert app.faces[0] == Layout().plan(st, 0.0)[0], "a waiting row shows the face it would show at t=0"
        assert app.faces[1].edge == Layout().plan(st, 0.0)[1].edge, "a busy row keeps the bar its t=0 face has"
        assert app.faces[0].border == deck.WAIT_BG and app.faces[0].dot == deck.WAIT_BG, "the state still reads"
        app.press(1)                                              # a press is still a press, and still tracked
        assert list(app.pending) == [st["rows"][1]["key"]], "the switch stopped being tracked, not just drawn"
        assert app.faces[1] == Layout().plan(app.state, 0.0)[1], "…and its key is its normal face, not white"
        assert app._anim_count() == 0, "the heartbeat says keys are moving when nothing is"
        b.cb(board_state(rows=[row(1, state="waiting"), row(2, state="busy"), row(3, state="ended")]))
        assert d.paints > painted, "a state that changed still repaints, animations off or not"
    finally:
        await app.stop()


async def test_animations_switch_off_and_on_while_the_deck_runs():
    """The same file, changed under a deck that is already breathing: the keys stop moving and come back to their
    still faces, and switching it on again starts the same frames up."""
    st = board_state(rows=[row(1, state="waiting"), row(2, state="idle")])
    clock = {"t": 300.0}
    app, d, b = await _app(st, clock=lambda: clock["t"], **_settings({"animations": True}))
    try:
        assert app.anim_on is True
        save_settings(Path(app.settings_path), {"animations": False})
        app._check_settings()
        assert app.anim_on is False and app.faces[0].bg == SLOT_BG[0], "the breath kept going"
        assert app._tick() == 0
        save_settings(Path(app.settings_path), {"animations": True})
        app._check_settings()
        assert app.anim_on is True
        clock["t"] += deck.BREATHE_S / 4
        assert app._tick() == 1, "the breath didn't start again"
        assert app.faces[0].bg == deck.blend(SLOT_BG[0], deck.WAIT_BG, deck.breathe(clock["t"])) != SLOT_BG[0]
    finally:
        await app.stop()


def test_a_brightness_said_on_the_command_line_reaches_the_device_in_main(tmp_path, monkeypatch, capfd):
    """`ctx deck --brightness 90` lights the keys to 90 before the loop starts; said nothing, and the file does."""
    path = tmp_path / "deck-settings"
    save_settings(path, {"brightness": 20})
    monkeypatch.setattr(deck, "SETTINGS_FILE", path)      # main reads it the way it imports it, not via the env
    lit: list[int] = []

    class Deck(FakeDeck):
        def set_brightness(self, percent: int) -> None:
            lit.append(percent)
            super().set_brightness(percent)

    real_start = deck._start

    async def _start_once(app, loop) -> None:
        await real_start(app, loop)                  # the app is what lights the keys, from the file or from now
        loop.call_later(0.05, loop.stop)              # …and this test stops there: main() runs until Ctrl-C

    monkeypatch.setattr(deck, "open_deck", lambda log=None: Deck())
    monkeypatch.setattr(deck, "_start", _start_once)
    assert deck.main(["--sock", str(tmp_path / "nope.sock"), "--brightness", "90"]) == 0
    assert lit == [90], f"the deck was lit to {lit}"
    capfd.readouterr()
    assert deck.main(["--sock", str(tmp_path / "nope.sock")]) == 0
    assert lit[-1] == 20, f"with nothing said the file lights the keys, got {lit}"


@pytest.mark.parametrize("offset,delta,want", [
    (0, 1, 2),      # the first step right skips the offset where nothing would move
    (2, -1, 0),     # and the step back from there comes all the way home
    (1, -1, 0),     # a window left at 1 by follow-the-highlight also goes home
    (2, 1, 3), (3, -1, 2), (5, 1, 6), (0, -1, 0),
])
def test_the_first_scroll_right_goes_two_so_the_rows_visibly_move(offset, delta, want):
    assert deck.scroll_step(offset, delta) == want


def test_scroll_from_the_far_left_shows_rows_three_to_five():
    rows = _rows(9) if "_rows" in globals() else None
    assert deck.scroll_window(9, deck.scroll_step(0, 1))[0] == ["prev", 3, 4, 5, "next"]
    assert deck.scroll_window(9, deck.scroll_step(2, -1))[0] == [1, 2, 3, 4, "next"]


# -------------------------------------------------------------------------------------- the device, coming and going
#
# The board is not the only thing that can leave. Under WSL the deck arrives through usbipd, and an unplug and a hand
# run `usbipd attach --wsl` swap it for a device the running process has no handle to. The app's device supervisor
# looks after that: it is the app's own work — a device that comes back has to be painted from the state the app is
# holding — so these tests drive it the same way the rest of the file drives the app, with `open_deck` and the poll
# interval handed in and no hardware anywhere near them.


class Goner(FakeDeck):
    """A device that dies on the wire after N writes: which is what an unplugged one looks like from here — a handle
    that answers the next call with a transport error, and the one after that the same way, forever."""

    def __init__(self, fail_after: int, serial="FAKEGONE"):
        super().__init__()
        self.serial, self.fail_after = serial, fail_after
        self.writes = 0

    def get_serial_number(self) -> str:
        return self.serial

    def set_key_image(self, key: int, image: bytes) -> None:
        self.writes += 1
        if self.writes > self.fail_after:
            raise TransportError("failed to write to device")
        super().set_key_image(key, image)


class Back(FakeDeck):
    """The device that turns up where the old one was: its own serial, and a memory of everything it was told."""

    def __init__(self, serial="FAKEBACK"):
        super().__init__()
        self.serial = serial

    def get_serial_number(self) -> str:
        return self.serial


async def _watched(d, b, said, clock=None, **kw) -> DeckApp:
    """A deck whose device the app looks after: `open_deck` handed over, and the poll short enough that a test can
    turn it by hand with `_device_poll()` rather than waiting on it."""
    app = DeckApp(d, b, fps=0.0, log=said.append, clock=clock or (lambda: 500.0),
                  open_deck=kw.pop("open_deck", lambda: d), poll_s=0.05, **kw)
    app.start()
    return app


async def test_a_device_that_starts_answering_badly_is_dropped_once():
    """The failure the whole supervisor exists for: 15 keys painted, then the handle goes bad, and every key after
    that fails the same way. One line says the deck is gone — not one per press, and not one per key of the repaint
    that press sets off — and the app is left holding nothing."""
    said: list[str] = []
    d = Goner(fail_after=deck.KEY_COUNT + 1)                # the 15 keys, one more for the first press, then dead
    app = await _watched(d, FakeBoardSync(), said)
    try:
        assert d.paints == deck.KEY_COUNT and app.deck is d
        app.press(1)                                        # a press repaints one key: the 16th write, which lives
        assert app.deck is d, "one key short of the failure shouldn't drop the device"
        assert app._device_words() == f"open {d.serial}", app._device_words()
        app.press(2)                                        # the 17th: the handle is dead, and says so
        assert app.deck is None, "the dead handle is still being held"
        assert sum(1 for line in said if "Stream Deck gone" in line) == 1, said
        assert not [line for line in said if "failed to write" in line], f"the error itself was printed: {said}"
        assert not [line for line in said if "No HID device" in line], said
        for key in range(deck.KEY_COUNT):                   # every key of a whole board repaint fails the same way
            app.press(key)
        assert sum(1 for line in said if "Stream Deck gone" in line) == 1, f"a gone line per key: {said}"
        assert d.closed, "the handle that died was not let go of"
    finally:
        await app.stop()


async def test_presses_while_the_device_is_gone_send_nothing():
    """Keys the operator is pressing in the dark. There is no device to press, so the board must not hear a word —
    and the deck stays quiet about it, because it has already said the deck is gone."""
    said: list[str] = []
    d = Goner(fail_after=deck.KEY_COUNT + 1)
    b = FakeBoardSync()
    app = await _watched(d, b, said)
    try:
        app.press(1)
        app.press(2)                                        # the 16th and 17th writes: the first drops the device
        b.seen.clear()
        for key in (0, 3, 7, 11, 14):
            app.press(key)
        assert b.seen == [], f"a press with no device reached the board: {b.seen}"
        assert d.writes == deck.KEY_COUNT + 2, f"the dead handle was still written to {d.writes} times"
        assert sum(1 for line in said if "Stream Deck gone" in line) == 1, said
    finally:
        await app.stop()


async def test_a_device_the_app_never_had_costs_nothing_per_press():
    """`--no-device` with nothing attached, which is what a machine before the first plug-in looks like: a press is
    not an error and not news, and nothing is drawn because there is nothing to draw on."""
    said: list[str] = []
    b = FakeBoardSync()
    app = DeckApp(None, b, fps=0.0, log=said.append, open_deck=lambda: None, poll_s=0.05)
    app.start()
    try:
        assert app._device_words() == "waiting"
        for key in (0, 5, 14):
            app.press(key)
        assert b.seen == [], f"a press with no device reached the board: {b.seen}"
        assert said == [], f"a press with no device said something: {said}"
    finally:
        await app.stop()


async def test_the_supervisor_keeps_looking_for_a_device_and_takes_the_one_it_finds():
    """The poll is the whole recovery: nothing attached at start says so once, and the deck comes up by itself when
    `usbipd attach` finally runs — lit, wired to the app, and painted with all fifteen keys."""
    said: list[str] = []
    later = Back()
    tries: list[int] = []

    def sometimes():
        tries.append(len(tries))
        if len(tries) < 3:
            raise RuntimeError("no Stream Deck found")
        later.open()
        return later

    app = DeckApp(None, FakeBoardSync(), fps=0.0, log=said.append, clock=lambda: 500.0,
                  open_deck=sometimes, poll_s=0.05)
    app.start()
    try:
        assert sum(1 for line in said if "waiting for one" in line) == 1, said
        for _ in range(3):                                  # the same call the timer makes, turned by the test
            app._device_poll()
        assert len(tries) == 3, f"the device was looked for {len(tries)} times: the poll gave up"
        assert app.deck is later
        assert sum(1 for line in said if "waiting for one" in line) == 1, f"the wait said twice: {said}"
        assert later.brightness == deck.load_settings(app.settings_path)["brightness"], "the new device was never lit"
        assert later.cb is not None, "the new device was never told who to ask about a press"
        assert later.reset_called, "the new device was not cleared before it was painted"
        assert len(later.images) == deck.KEY_COUNT and later.paints == deck.KEY_COUNT
        assert app._device_words() == f"open {later.serial}", app._device_words()
        back = [line for line in said if "Stream Deck back" in line]
        assert len(back) == 1 and later.serial in back[0] and "15 keys" in back[0], said
    finally:
        await app.stop()


async def test_a_device_that_was_already_open_is_lit_from_the_settings_file():
    """The same take-up when the device was open before the app started: the file's brightness (or an explicit
    `--brightness`, which wins for the run) reaches the device that turns up later, not only the one at start."""
    said: list[str] = []
    later = Back()
    app = DeckApp(None, FakeBoardSync(), fps=0.0, log=said.append, clock=lambda: 500.0,
                  open_deck=lambda: later.open() or later, poll_s=0.05, brightness_override=33)
    app.start()
    try:
        app._device_poll()
        assert later.brightness == 33, "the file or the command line never reached the device that came back"
    finally:
        await app.stop()


async def test_a_replugged_deck_is_painted_from_the_state_and_not_from_the_cache():
    """The cache only knows which faces *this process* sent. A deck that came back from a replug is showing what the
    last one painted, so the take-up clears the cache and sends the whole board again — every key of it, from the
    state the app is holding, whatever the cache had decided was already there."""
    said: list[str] = []
    gone, back = Goner(fail_after=deck.KEY_COUNT + 1), Back()
    rows = [row(n, state="idle") for n in range(1, 4)]
    b = FakeBoardSync(board_state(rows=rows))
    app = await _watched(gone, b, said, open_deck=lambda: back)
    try:
        app.press(1)
        app.press(2)                                        # the write that finds the handle is dead
        assert app.deck is None
        b.cb(board_state(rows=[rows[0], {**rows[1], "title": "renamed"}, rows[2]]))
        assert app.deck is None and b.cb is not None, "a state with no device must still update the app's state"
        app._device_poll()
        assert app.deck is back
        assert len(back.images) == deck.KEY_COUNT, \
            f"the new device got {len(back.images)} keys: the old device's face cache was still standing"
        assert back.paints == deck.KEY_COUNT
        assert back.images == {k: render(app.faces[k], back) for k in range(deck.KEY_COUNT)}, \
            "the keys that the cache thought were already painted didn't match the state they were painted from"
        assert back.images[1] != back.images[0]
        app.press(2)                                        # and it is a device again: a press paints, and asks
        assert len(back.images) == deck.KEY_COUNT and {"cmd": "slot", "n": 3} in b.seen, b.seen
        assert sum(1 for line in said if "Stream Deck gone" in line) == 1, said
    finally:
        await app.stop()


async def test_a_device_that_says_it_is_not_there_is_dropped_by_the_poll():
    """`connected()` — the library's own word, where it has one — is checked on the poll, so a deck that went without
    anyone pressing it goes into waiting rather than sitting there looking painted."""
    class Quiet(Back):
        def connected(self) -> bool:
            return False

    said: list[str] = []
    d = Quiet()
    app = await _watched(d, FakeBoardSync(), said)
    try:
        assert app.deck is d
        app._device_poll()
        assert app.deck is None and app._device_words() == "waiting"
        assert sum(1 for line in said if "Stream Deck gone" in line) == 1, said
    finally:
        await app.stop()


async def test_a_device_that_still_answers_costs_the_poll_nothing():
    """The poll runs every 2 s for the life of the process, so it must not touch the wire: one healthy device, many
    polls, and no write, read or reset between them."""
    said: list[str] = []
    d = Back()
    app = await _watched(d, FakeBoardSync(), said)
    try:
        paints, opened = d.paints, d.opened
        for _ in range(20):
            app._device_poll()
        assert (d.paints, d.opened) == (paints, opened), "the poll put something on the wire"
        assert app.deck is d and said == [], said
    finally:
        await app.stop()


async def test_the_heartbeat_says_whether_there_is_a_device():
    """`ctx deck` runs for hours unattended, and the one line it prints has to say which of the two things it is
    waiting on — the board, or a deck that nobody has attached yet."""
    said: list[str] = []
    d = Back()
    app = DeckApp(d, FakeBoardSync(), fps=0.0, log=lambda m: None, heartbeat_s=0.05, echo=said.append)
    app.start()
    try:
        await until("a heartbeat line", lambda: any("keys" in line for line in said), timeout=2)
        assert f"device: open {d.serial}" in " ".join(said), said
        said.clear()
        app._mark_gone()                                     # the same thing the poll does when a device won't answer
        await until("a heartbeat line", lambda: any("keys" in line for line in said), timeout=2)
        assert "device: waiting" in " ".join(said), said
    finally:
        await app.stop()


def test_no_extra_installed_says_how_to_get_it(tmp_path, capfd, monkeypatch):
    monkeypatch.setitem(sys.modules, "StreamDeck", None)       # `import StreamDeck` now raises ImportError
    assert deck.main(["--sock", str(tmp_path / "nope.sock")]) == 2
    assert "install with: uv pip install -e '.[deck]'" in capfd.readouterr().out
