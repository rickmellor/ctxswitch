"""`ctx deck`: the board's table as fifteen keys.

The layout is tested as pure data (`Layout.plan` / `Layout.on_press` / `Layout.on_release` — no Pillow, no device,
no board), the drawing
as bytes off a fake deck, and the wire against a fake board: a real `asyncio.start_unix_server` that speaks
PROTOCOL.md, so the client is tested on the contract the board's own job implements.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys

import pytest

from conftest import unix_sockets_ok
from ctxswitch.client import deck
from ctxswitch.client.deck import BoardClient, DeckApp, FakeDeck, KeyFace, Layout, render

pytestmark = pytest.mark.timeout(10)
needs_unix = pytest.mark.skipif(not unix_sockets_ok(), reason="this kernel won't let a test bind a unix socket")

SLOT_BG = deck.SLOT_BG


def row(n: int, **kw) -> dict:
    r = {"n": n, "key": f"laptop/claude/{n}", "title": f"session {n}", "host": "laptop", "harness": "claude",
         "state": "busy", "visible": False, "selected": False, "pool_jobs": 0}
    return {**r, **kw}


def board_state(rows=None, modal=None, options=None) -> dict:
    """A `state` object as PROTOCOL.md describes it: three live rows unless told otherwise."""
    rows = rows if rows is not None else [row(1), row(2), row(3)]
    return {"proto": 1, "rows": rows, "slots": [r["key"] for r in rows], "visible_key": rows[0]["key"],
            "selected_key": rows[-1]["key"], "modal": modal, "options": options or []}


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
    for i in range(5, 10):
        assert faces[i] == KeyFace(), "reserved keys stay blank"


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


def test_a_waiting_session_wears_a_red_border_and_flashes():
    waiting = board_state(rows=[row(1, state="waiting"), row(2, state="busy")])
    on = Layout().plan(waiting, blink=True)
    off = Layout().plan(waiting, blink=False)
    assert on[0].bg == deck.WAIT_BG and off[0].bg == SLOT_BG[0]
    assert on[0].border == off[0].border == deck.WAIT_BG, "the border is the steady half of the flash"
    assert on[0].dot == off[0].dot == deck.WAIT_BG
    assert on[0].lines == off[0].lines, "the flash changes the colour, not what the key says"
    assert on[1].bg == off[1].bg == SLOT_BG[1], "a session that isn't waiting doesn't flash"


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
    for i in (10, 11, 12, 13, 14):
        assert faces[i].lines == ("cancel",), f"key {i} should offer cancel"
    assert faces[10].bg == faces[11].bg == faces[12].bg == faces[13].bg


def test_start_mode_is_the_same_screen_as_the_picker():
    assert Layout().plan(board_state(modal="start", options=[{"n": 1, "label": "x"}])) == \
        Layout().plan(board_state(modal="picker", options=[{"n": 1, "label": "x"}]))


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


@pytest.mark.parametrize("key", list(range(5, 10)))
def test_a_blank_key_sends_nothing(key):
    assert Layout().on_press(board_state(), key) == []


def test_a_press_with_no_board_sends_nothing():
    assert Layout().on_press(None, 0) == []


def test_the_modal_keys():
    L = Layout()
    picker = board_state(modal="picker", options=[{"n": 3, "label": "pi"}])
    assert L.on_press(picker, 2) == [{"cmd": "pick", "n": 3}]
    for key in (10, 11, 12, 13, 14):
        assert L.on_press(picker, key) == [{"cmd": "cancel"}], f"key {key} should cancel a picker"
    assert L.on_press(picker, 7) == []
    confirm = board_state(modal="confirm")
    assert L.on_press(confirm, 14) == [{"cmd": "confirm", "yes": True}]
    for key in (10, 11, 12, 13):
        assert L.on_press(confirm, key) == [{"cmd": "cancel"}], f"key {key} should answer the confirm no"
    assert L.on_press(confirm, 0) == [], "the slot keys do nothing while a confirm is up"
    assert L.on_press(board_state(modal="input"), 0) == []


def test_a_key_outside_the_deck_sends_nothing():
    assert Layout().on_press(board_state(), 15) == [] and Layout().on_press(board_state(), -1) == []


# --------------------------------------------------------------------------------------- what a held key releases


def test_an_arrow_held_past_the_threshold_opens_the_highlighted_row():
    """▲▼ press as they always did; hold one long enough and letting go opens the row the highlight landed on."""
    L = Layout()
    third = board_state(rows=[row(1), row(2), row(3, selected=True)])
    assert L.on_release(third, 11, deck.HOLD_MS) == [{"cmd": "slot", "n": 3}], "held exactly long enough should open"
    assert L.on_release(third, 11, 700) == [{"cmd": "slot", "n": 3}]
    assert L.on_release(third, 10, 700) == [{"cmd": "slot", "n": 3}], "▲ held opens it the same way"
    assert L.on_release(third, 11, 300) == [], "letting go early is only the move the press already sent"
    assert L.on_release(third, 11, deck.HOLD_MS - 1) == [], "the threshold itself, to the millisecond"


def test_a_release_with_nothing_to_open_sends_nothing():
    L = Layout()
    assert L.on_release(board_state(), 11, 900) == [], "no row is highlighted, so there is nothing to open"
    assert L.on_release(None, 11, 900) == [], "with no board there is nothing to open"
    picker = board_state(modal="picker", options=[{"n": 3, "label": "pi"}])
    assert L.on_release(picker, 11, 900) == [], "a held arrow still only cancels a picker"
    assert L.on_release(picker, 10, 900) == []
    assert L.on_release(board_state(rows=[row(1, selected=True)]), 2, 900) == [], "a slot key held does nothing"
    assert L.on_release(board_state(rows=[row(1, selected=True)]), 14, 900) == [], "nor does end"
    assert L.on_release(board_state(modal="confirm"), 11, 900) == []
    assert L.on_release(board_state(modal="input"), 11, 900) == []
    assert L.on_release(board_state(rows=[row(1, selected=True)]), 11, 0) == []


# --------------------------------------------------------------------------------------------------- drawing


def test_every_kind_of_face_draws_a_jpeg_the_deck_can_take():
    d = FakeDeck()
    faces = [Layout().plan(board_state())[i] for i in range(deck.KEY_COUNT)]
    faces += [Layout().plan(None)[7], Layout().plan(board_state(modal="confirm"))[14],
              Layout().plan(board_state(modal="picker", options=[{"n": 1, "label": "x"}]))[0],
              Layout().plan(board_state(rows=[row(1, state="waiting", visible=True, selected=True)]))[0],
              KeyFace()]
    from PIL import Image
    for face in faces:
        img = render(face, d)
        assert img[:2] == b"\xff\xd8", f"{face.lines} didn't come out as JPEG"
        im = Image.open(io.BytesIO(img))
        assert im.size == (72, 72) and im.format == "JPEG"


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


async def _app(state=None, blink_s: float = 0.0) -> tuple[DeckApp, FakeDeck, FakeBoardSync]:
    d, b = FakeDeck(), FakeBoardSync(state if state is not None else board_state())
    app = DeckApp(d, b, blink_s=blink_s, log=lambda m: None)
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
    app, d, b = await _app()
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
        d.cb(d, 13, False)                                    # a release is nothing until a key is held down
        assert b.seen == [{"cmd": "slot", "n": 3}, {"cmd": "move", "delta": 1}]
    finally:
        await app.stop()


async def test_an_arrow_held_opens_the_row_it_left_the_highlight_on():
    """The app's side of the long press: ▼ moves at once, and letting go after HOLD_MS opens the highlighted row —
    read off the state as it stands at release, which is where the move left it. A quick tap sends only the move."""
    clock = {"t": 1000.0}

    class Held(FakeBoardSync):
        """A board a test can advance the clock on and push a state through, as the real one does over the socket."""

        def push(self, state: dict) -> None:
            self.state = state
            self.cb(state)

    d, b = FakeDeck(), Held()
    app = DeckApp(d, b, blink_s=0.0, log=lambda m: None, clock=lambda: clock["t"])
    app.start()
    try:
        d.cb(d, 11, True)                                       # ▼ down: the move goes now
        assert b.seen == [{"cmd": "move", "delta": 1}], b.seen
        b.push(board_state(rows=[row(1), row(2), row(3, selected=True)]))   # the board's own move, come back
        clock["t"] += 0.7                                       # held 700 ms, past the 600 ms threshold
        d.cb(d, 11, False)
        assert [r["cmd"] for r in b.seen] == ["move", "slot"], "a hold sends the move, then the slot it landed on"
        assert b.seen[-1] == {"cmd": "slot", "n": 3}, b.seen

        b.seen.clear()
        d.cb(d, 10, True)
        d.cb(d, 10, False)                                      # up again at once: nothing held, nothing sent
        assert b.seen == [{"cmd": "move", "delta": -1}], b.seen

        b.seen.clear()
        clock["t"] += 2.0
        d.cb(d, 2, True)
        clock["t"] += 2.0
        d.cb(d, 2, False)                                       # a slot key held out is still just its press
        assert b.seen == [{"cmd": "slot", "n": 3}], b.seen
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
    app = DeckApp(d, b, blink_s=0.0, log=lambda m: None)
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
    app = DeckApp(d, b, blink_s=0.0, log=said.append)
    app.start()
    try:
        d.cb(d, 4, True)
        assert any("no such slot" in line for line in said), said
    finally:
        await app.stop()


async def test_blinking_repaints_the_waiting_key_alone():
    app, d, _b = await _app(state=board_state(rows=[row(1, state="waiting"), row(2)]))
    try:
        assert d.paints == 15
        before = dict(d.images)
        assert app.set_blink(True) == 1, "only the waiting key should change on the flash"
        assert d.paints == 16 and d.images[0] != before[0] and d.images[1] == before[1]
        assert app.set_blink(False) == 1
        assert d.images[0] == before[0], "the flash should come back to the slot's own colour"
        app._state(board_state(rows=[row(1, state="busy"), row(2)]))
        painted = d.paints
        assert app.set_blink(True) == 0 and d.paints == painted, "nothing is waiting, so nothing repaints"
    finally:
        await app.stop()


async def test_the_blink_timer_runs_by_itself():
    app, _d, _b = await _app(state=board_state(rows=[row(1, state="waiting")]), blink_s=0.05)
    try:
        await until("the blink phase to flip", lambda: app.blink, timeout=1.0)
    finally:
        await app.stop()


async def test_stopping_resets_the_deck_and_closes_it():
    app, d, _b = await _app()
    await app.stop()
    assert d.reset_called and d.closed


async def test_the_heartbeat_says_the_deck_is_alive():
    """`ctx deck` runs for hours, so it prints a line of proof now and then — and repaints in passing, which the
    face cache makes cost nothing when nothing changed."""
    d, b = FakeDeck(), FakeBoardSync(board_state())
    said: list[str] = []
    app = DeckApp(d, b, blink_s=0.0, log=lambda m: None, heartbeat_s=0.05, echo=said.append)
    app.start()
    try:
        await until("a heartbeat line", lambda: any("keys" in line for line in said), timeout=2)
        assert d.paints == 15, "the heartbeat's redraw found nothing changed, so it painted nothing extra"
    finally:
        await app.stop()
    assert "the board" in " ".join(said)


def test_no_deck_attached_says_so(monkeypatch, capfd):
    def nope(*_a, **_kw):
        raise RuntimeError("no Stream Deck found")
    monkeypatch.setattr(deck, "open_deck", nope)
    assert deck.main([]) == 1
    assert "no Stream Deck" in capfd.readouterr().out


def test_no_extra_installed_says_how_to_get_it(tmp_path, capfd, monkeypatch):
    monkeypatch.setitem(sys.modules, "StreamDeck", None)       # `import StreamDeck` now raises ImportError
    assert deck.main(["--sock", str(tmp_path / "nope.sock")]) == 2
    assert "install with: uv pip install -e '.[deck]'" in capfd.readouterr().out
