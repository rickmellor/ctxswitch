"""The board's control socket: what another program (the Stream Deck daemon) sees, and can make the board do.

The wire contract is docs/board-control.md. Everything here drives a real board the way the other board tests do —
a fake hub, tmux patched out, the app run with ``run_test`` — and asks the endpoint itself: ``dispatch`` for the
commands (the server runs on the app's own loop, exactly as a socket request does), and one real socket round trip
for the NDJSON framing, which is the part a stub could get wrong.

Binding a unix socket is a second thing a sandbox may refuse (the way it refuses tmux), so the tests that need one
carry ``unix_sockets_ok()``; the rest cover the same contract through ``dispatch`` and run anywhere. That a board
which can't bind one still mounts runs everywhere too."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ctxswitch.client.tui import app as tui
from ctxswitch.client.tui.control import board_state
from ctxswitch.common.config import Config
from tests.conftest import unix_sockets_ok

NOW = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
needs_sockets = pytest.mark.skipif(not unix_sockets_ok(), reason="this kernel forbids binding a unix socket")
OPTS = [("claude · bedrock", "claude", "bedrock"), ("claude · max", "claude", "max")]
S = [f"laptop/claude/s{i}" for i in range(1, 10)]


def _ago(mins: int) -> str:
    return (NOW - timedelta(minutes=mins)).isoformat().replace("+00:00", "Z")


def _sess(key, *, status="idle", parent="", host=None, **kw) -> dict:
    """One session row as the hub's list hands it, with the fields the board's state reads."""
    host = host or key.split("/", 1)[0]
    s = {"key": key, "host_id": host, "harness": "claude", "session_id": key.rsplit("/", 1)[-1],
         "title": f"session {key.rsplit('/', 1)[-1]}", "cwd": "/r/webapp", "live_status": status,
         "status_since": _ago(20), "last_activity": _ago(2), "started_at": _ago(60), "repo_root": "/r/webapp",
         "git_branch": "main", "turn_count": 3, "meta": {}, "open_asks": [], "parent_key": parent, "job": "",
         "job_status": "", "job_brief": "", "turns": [], "asks": [], "ledger": [], "messages": [], "misplaced": []}
    return {**s, **kw}


def _job(key, parent, brief="job1.md", status="ok", **kw) -> dict:
    """A pool-job child: nested under ``parent`` on the board, so never a numbered row."""
    return _sess(key, parent=parent, title=f"pool job {key}", job=f"sa-{key.rsplit('/', 1)[-1]}", job_status=status,
                 job_brief=brief, job_turns=12, **kw)


class FakeClient:
    """The hub as these tests need it: the rows it hands out, and the detail pane's session by key."""

    def __init__(self, rows):
        self.rows = rows

    def sessions(self, host=None, live_only=False):
        return {"sessions": [dict(x) for x in self.rows], "change": 1}

    def session(self, key, turns=50):
        return dict(next((x for x in self.rows if x["key"] == key), {"key": key}), turns=[], asks=[], ledger=[],
                    messages=[])

    def attention(self, host=None):
        return {"items": []}

    def hosts(self):
        return [{"host_id": "laptop"}]

    def viewed(self, key, at):
        pass

    def spend(self, frm, to, host=None):
        return None

    def ws_url(self):
        return "ws://x/v1"


def _board(monkeypatch, tmp_path, rows, *, visible=None, in_tmux=True, sock=None, bind=None) -> tui.CtxApp:
    """A board with ``rows`` on it: in tmux, ``visible`` in the main pane, its control socket at ``sock`` (a test's
    own tmp path — never the operator's live board), and its bind made to fail with ``bind`` when one is given.
    P's panel starts off, so a board here never runs anyone's ``saint-pool`` on its own, and neither does g's."""
    cfg = Config()
    cfg.hub.token = "t"
    cfg.collector.host_id = "laptop"
    # conftest isolates the board's own state files; g's panel is not in that fixture, and the operator's real file
    # says the panel is up — so a board here would open it on mount and fetch from a client that has no metrics()
    monkeypatch.setattr(tui, "METRICS_FILE", tmp_path / "metrics-panel")
    monkeypatch.setattr(tui, "_pool_panel_state", lambda: False)     # else P's panel runs the operator's saint-pool
    monkeypatch.setattr(tui.tmux, "in_tmux", lambda: in_tmux)
    monkeypatch.setattr(tui.tmux, "reap_viewer", lambda *a, **k: None)
    monkeypatch.setattr(tui.tmux, "restore_viewer", lambda *a, **k: None)
    monkeypatch.setattr(tui.tmux, "panes", lambda *a, **k: [])
    monkeypatch.setattr(tui.tmux, "visible_key", lambda ps=None, sessions=None: visible)
    monkeypatch.setattr(tui.tmux, "on_screen_keys", lambda ps=None, sessions=None: set())
    monkeypatch.setattr(tui.tmux, "reach_all", lambda sessions, host=None, ssh=None: {})
    monkeypatch.setattr(tui.tmux, "agent_pane", lambda key, ps=None:
                        tui.tmux.Pane(id="%1", window="@1", window_name="1", index=1, pid=4242, key=key,
                                      session="ctx", role="agent"))
    monkeypatch.setattr(tui.CtxApp, "listen_ws", lambda self: None)
    monkeypatch.setattr(tui.CtxApp, "join_tmux", lambda self: None)
    monkeypatch.setattr(tui, "datetime", _Frozen)
    if sock is not None:
        monkeypatch.setenv("CTXSWITCH_BOARD_SOCK", str(sock))   # this test's own path, never the live board's
    if bind is not None:
        async def _refuse(*a, **k):
            raise bind
        monkeypatch.setattr(tui.control.asyncio, "start_unix_server", _refuse)
    app = tui.CtxApp(cfg, None)
    app.client = FakeClient(rows)
    return app


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


def _rows(n: int) -> list[dict]:
    return [_sess(k) for k in S[:n]]


def _modal(app):
    """The top screen, if it's one the protocol can answer."""
    return tui.control.top_modal(app)


def _sock(tmp_path) -> Path:
    """A control socket for this test alone: the default is the operator's own live board."""
    return tmp_path / "board.sock"


async def _wait(pilot, what: str, pred, tries: int = 150) -> None:
    """Wait for something the app has to do, failing with what it was waiting for rather than hanging."""
    for _ in range(tries):
        await pilot.pause(0.02)
        if pred():
            return
    raise AssertionError(f"waited on {what} for {tries * 0.02:.1f}s and it never happened")


async def _connect(sock: Path):
    """The client's end of the board's socket, on the loop the board runs on."""
    return await asyncio.open_unix_connection(path=str(sock))


class _Pipe:
    """A client's end without a socket: lines in one side, whatever the endpoint writes out the other. A kernel that
    won't bind unix sockets still lets the framing — the part a stub gets wrong — be driven for real."""

    def __init__(self):
        self.reader = asyncio.StreamReader()
        self.sent: list = []
        self.closed = False

    def send(self, line: str) -> None:
        self.reader.feed_data(line.encode())

    def hang_up(self) -> None:
        self.reader.feed_eof()
        self.closed = True

    def write(self, data: bytes) -> None:                 # as StreamWriter's: sync, with drain() to follow
        self.sent.append(data)

    def close(self) -> None:
        self.closed = True

    async def drain(self) -> None:
        pass

    def lines(self) -> list[dict]:
        return [json.loads(x) for x in b"".join(self.sent).split(b"\n") if x.strip()]

    async def read(self, want: int, what: str, tries: int = 150) -> list[dict]:
        for _ in range(tries):
            got = self.lines()
            if len(got) >= want:
                return got
            await asyncio.sleep(0.02)
        raise AssertionError(f"waited on {want} line(s) of {what}, got {self.lines()}")


async def _endpoint(app) -> _Pipe:
    """One connection to the endpoint, driven straight: the same ``handle`` a socket would run."""
    pipe = _Pipe()
    pipe.task = asyncio.ensure_future(app.control.handle(pipe.reader, pipe))
    return pipe


async def _ask(writer, req: dict) -> None:
    writer.write((json.dumps(req) + "\n").encode())
    await writer.drain()


async def _lines(reader, want: int, what: str, tries: int = 250) -> list[dict]:
    """``want`` NDJSON replies, saying what did arrive when they don't."""
    out: list[dict] = []
    for _ in range(tries):
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=0.1)
        except asyncio.TimeoutError:
            continue
        if not raw:
            break
        out.append(json.loads(raw))
        if len(out) >= want:
            return out
    raise AssertionError(f"waited on {want} line(s) of {what}, got {len(out)}: {out}")


# -- state --------------------------------------------------------------------------------------------------------
async def test_the_state_lists_the_numbered_rows_and_counts_a_parent_s_pool_jobs(monkeypatch, tmp_path):
    """Eleven sessions, one of them a pool job: nine numbered rows (the board numbers 1…9), the job counted on its
    parent instead of taking a row, and one more slot past the last number."""
    parent, kid = _sess("laptop/claude/parent", status="busy"), None
    rows = _rows(1)
    kid = _job("laptop/claude/kid", parent["key"])
    rows = [rows[0], parent, kid, *_rows(9)[1:]]                 # 11 sessions; the job nests under parent
    app = _board(monkeypatch, tmp_path, rows, visible="laptop/claude/s3")
    async with app.run_test(size=(70, 40)) as pilot:
        await pilot.pause(1)
        assert len(app.slots) == 10                              # the nested job gets no number at all
        st = board_state(app)
        assert st["proto"] == 1 and st["modal"] is None and st["options"] == []
        assert [r["n"] for r in st["rows"]] == list(range(1, 10))          # nine numbered rows
        assert [r["key"] for r in st["rows"]] == app.slots[:9]
        assert st["slots"] == app.slots and len(st["rows"]) == 9
        assert st["visible_key"] == "laptop/claude/s3" and st["selected_key"] == app.slots[0]
        by_key = {r["key"]: r for r in st["rows"]}
        assert by_key["laptop/claude/s3"]["visible"] is True and by_key[app.slots[0]]["selected"] is True
        assert [r["visible"] for r in st["rows"]].count(True) == 1 and [r["selected"] for r in st["rows"]].count(True) == 1
        assert by_key[parent["key"]]["pool_jobs"] == 1           # the child, counted and not listed
        assert all(r["pool_jobs"] == 0 for k, r in by_key.items() if k != parent["key"])
        assert kid["key"] not in by_key and kid["key"] not in app.slots
        assert by_key["laptop/claude/s1"]["title"] == "session s1" and by_key["laptop/claude/s1"]["harness"] == "cc"
        assert by_key[parent["key"]]["state"] == "busy" and by_key["laptop/claude/s1"]["state"] == "idle"
        assert by_key["laptop/claude/s1"]["host"] == "laptop"


async def test_a_waiting_or_ended_session_says_so_in_its_row(monkeypatch, tmp_path):
    rows = [_sess("laptop/claude/busy", status="busy"),
            _sess("laptop/claude/wait", status="waiting",
                  open_asks=[{"kind": "plain", "text": "Ship it?", "asked_at": _ago(3), "answered": False}]),
            _sess("laptop/claude/gone", status="unknown", ended=True)]
    app = _board(monkeypatch, tmp_path, rows)
    app.show_ended = True                                # e: an ended session has a row only when it's shown
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        by_key = {r["key"]: r for r in board_state(app)["rows"]}
        assert [by_key[k]["state"] for k in ("laptop/claude/busy", "laptop/claude/wait", "laptop/claude/gone")] \
            == ["busy", "waiting", "ended"]


# -- hello, state, subscribe --------------------------------------------------------------------------------------
async def test_hello_echoes_the_id_and_answers_the_proto_and_state(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, _rows(2))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        pipe = await _endpoint(app)
        pipe.send(json.dumps({"cmd": "hello", "id": 7}) + "\n")
        (reply,) = await pipe.read(1, "hello's reply")
        assert reply["ok"] and reply["id"] == 7 and reply["proto"] == 1
        assert [r["n"] for r in reply["state"]["rows"]] == [1, 2]
        pipe.send(json.dumps({"cmd": "state"}) + "\n")
        got = await pipe.read(2, "state's reply")
        assert got[1]["ok"] and "proto" not in got[1] and got[1]["state"]["slots"] == app.slots
        pipe.hang_up()
        await pipe.task


async def test_a_subscribe_then_a_render_pushes_one_state_and_two_renders_still_push_one(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, _rows(2))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        pipe = await _endpoint(app)
        pipe.send(json.dumps({"cmd": "subscribe"}) + "\n")
        (reply,) = await pipe.read(1, "subscribe's reply")
        assert reply["ok"] and reply["state"]["slots"] == app.slots
        app.render_table()                                       # one render: one push
        got = await pipe.read(2, "the pushed state")
        assert got[1]["event"] == "state" and got[1]["state"]["slots"] == app.slots
        app.text_filter = "s1"
        app.render_table()                                       # three renders inside one coalescing window:
        app.render_table()
        app.text_filter = ""
        app.render_table()
        before = len(pipe.lines())                               # one line, carrying the newest state
        await _wait(pilot, "the coalesced push", lambda: len(pipe.lines()) > before)
        await asyncio.sleep(0.05)                                # and nothing after it: one, not three
        new = pipe.lines()[before:]
        assert len(new) == 1 and new[0]["event"] == "state", new
        assert [r["key"] for r in new[0]["state"]["rows"]] == app.slots
        pipe.hang_up()
        await pipe.task


@needs_sockets
async def test_the_socket_is_0600_and_answers_a_real_client(monkeypatch, tmp_path):
    sock = _sock(tmp_path)
    app = _board(monkeypatch, tmp_path, _rows(2), sock=sock)
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert sock.exists(), "the board bound no socket"
        assert oct(sock.stat().st_mode & 0o777) == "0o600"
        reader, writer = await _connect(sock)
        try:
            await _ask(writer, {"cmd": "hello", "id": 7})
            (reply,) = await _lines(reader, 1, "hello's reply")
            assert reply["ok"] and reply["id"] == 7 and reply["proto"] == 1
        finally:
            writer.close()


async def test_a_modal_opening_or_closing_pushes_too(monkeypatch, tmp_path):
    """The smallest hook that fires on both push and dismiss: the app's own screen-change signal."""
    app = _board(monkeypatch, tmp_path, _rows(2))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        pushed: list = []
        app.control.push_state = lambda: pushed.append(len(app.screen_stack))
        app.push_screen(tui.Picker(OPTS))
        await pilot.pause(0.2)
        assert pushed == [2], pushed                             # up: pushed, once
        app.screen.dismiss(None)
        await _wait(pilot, "the modal's close to push", lambda: len(pushed) >= 2)
        assert pushed == [2, 1], pushed


# -- rows: slot, select, move -------------------------------------------------------------------------------------
async def test_slot_opens_the_session_by_its_number_and_select_only_highlights(monkeypatch, tmp_path):
    opened: list = []
    app = _board(monkeypatch, tmp_path, _rows(3))
    monkeypatch.setattr(tui.CtxApp, "open_key", lambda self, key: opened.append(key))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert app.control.dispatch({"cmd": "slot", "n": 2})["ok"]
        assert opened == [app.slots[1]]                  # exactly Alt+N / Enter: open_key on that row
        opened.clear()
        r = app.control.dispatch({"cmd": "select", "n": 3})
        assert r["ok"] and opened == [] and app.selected == app.slots[2]     # highlighted, not opened
        assert r["state"]["selected_key"] == app.slots[2]


async def test_move_walks_the_numbered_rows_and_clamps_at_both_ends(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, _rows(3))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert app.selected == app.slots[0]
        assert app.control.dispatch({"cmd": "move", "delta": 1})["ok"] and app.selected == app.slots[1]
        for _ in range(4):                                       # clamped at the last row
            app.control.dispatch({"cmd": "move", "delta": 1})
        assert app.selected == app.slots[2]
        for _ in range(4):                                       # and at the first
            app.control.dispatch({"cmd": "move", "delta": -1})
        assert app.selected == app.slots[0]
        assert app.control.dispatch({"cmd": "move", "delta": 0}) == {"ok": False, "error": "bad request"}


async def test_a_number_outside_the_rows_is_no_such_slot(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, _rows(3))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        for cmd in ({"cmd": "slot", "n": 12}, {"cmd": "select", "n": 0}, {"cmd": "slot", "n": "2"}):
            assert app.control.dispatch(cmd) == {"ok": False, "error": "no such slot"}
        assert app.selected == app.slots[0]                      # nothing moved


async def test_move_on_a_board_with_no_rows_says_there_is_no_such_slot(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, [])
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert app.slots == []
        assert app.control.dispatch({"cmd": "move", "delta": 1}) == {"ok": False, "error": "no such slot"}


# -- modals: new / start / pick / cancel --------------------------------------------------------------------------
async def test_new_opens_the_picker_whose_options_a_pick_can_choose(monkeypatch, tmp_path):
    asked: list = []
    app = _board(monkeypatch, tmp_path, _rows(2))
    monkeypatch.setattr(tui, "new_session_options", lambda cfg, which=None: OPTS)
    monkeypatch.setattr(tui.CtxApp, "_ask_dir",
                        lambda self, harness, profile, cwd=None, host=None: asked.append((harness, profile)))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        r = app.control.dispatch({"cmd": "new"})
        assert r["ok"] and r["state"]["modal"] == "picker"       # n's own picker: the profiles, plus the shell
        assert r["state"]["options"] == [{"n": 1, "label": "claude · bedrock"}, {"n": 2, "label": "claude · max"},
                                         {"n": 3, "label": "shell"}]
        await pilot.pause(0.2)
        r = app.control.dispatch({"cmd": "pick", "n": 1})
        assert r["ok"] and r["state"]["modal"] != "picker"
        await _wait(pilot, "the directory prompt a pick ends with", lambda: bool(asked))
        assert asked == [("claude", "bedrock")]


async def test_a_pick_outside_the_picker_s_options_is_refused_and_cancel_closes_it(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, _rows(2))
    monkeypatch.setattr(tui, "new_session_options", lambda cfg, which=None: OPTS)
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert app.control.dispatch({"cmd": "new"})["ok"]
        await pilot.pause(0.2)
        assert isinstance(_modal(app), tui.Picker)
        assert app.control.dispatch({"cmd": "pick", "n": 9})["ok"] is False   # choose() said no
        assert isinstance(_modal(app), tui.Picker)                            # and it is still up
        assert app.control.dispatch({"cmd": "cancel"})["ok"]
        await _wait(pilot, "the picker to close", lambda: _modal(app) is None)
        assert board_state(app)["modal"] is None


async def test_a_start_screen_answers_its_profile_keys(monkeypatch, tmp_path):
    asked: list = []
    app = _board(monkeypatch, tmp_path, _rows(2))
    monkeypatch.setattr(tui, "new_session_options", lambda cfg, which=None: OPTS)
    monkeypatch.setattr(tui.CtxApp, "_ask_dir",
                        lambda self, harness, profile, cwd=None, host=None: asked.append((harness, profile)))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        r = app.control.dispatch({"cmd": "start"})
        assert r["ok"] and r["state"]["modal"] == "start"
        assert [o["n"] for o in r["state"]["options"]] == [1, 2, 3]      # the profiles only, never a recent row
        assert app.control.dispatch({"cmd": "pick", "n": 2})["ok"]
        await _wait(pilot, "the directory prompt a start ends with", lambda: bool(asked))
        assert asked == [("claude", "max")]


async def test_a_pick_when_no_picker_is_open_says_so(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, _rows(2))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert app.control.dispatch({"cmd": "pick", "n": 1}) == {"ok": False, "error": "no picker open"}
        assert app.control.dispatch({"cmd": "cancel"})["ok"]             # ok even with nothing to dismiss


async def test_the_keyboard_through_the_methods_behaves_as_it_always_did(monkeypatch, tmp_path):
    """The refactor must not change the keys: a digit picks, Esc cancels, and the y/n answers either way."""
    seen: list = []
    app = _board(monkeypatch, tmp_path, _rows(2))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        app.push_screen(tui.Picker(OPTS), lambda r: seen.append(r))
        await pilot.pause(0.2)
        await pilot.press("2")
        await _wait(pilot, "the picker's answer", lambda: bool(seen))
        assert seen == [("claude", "max")]
        app.push_screen(tui.Picker(OPTS), lambda r: seen.append(r))
        await pilot.pause(0.2)
        await pilot.press("escape")
        await _wait(pilot, "Esc to cancel the picker", lambda: len(seen) == 2)
        assert seen[1] is None
        app.push_screen(tui.Confirm("end it?"), lambda r: seen.append(r))
        await pilot.pause(0.2)
        await pilot.press("y")
        await _wait(pilot, "the y/n's answer", lambda: len(seen) == 3)
        assert seen[2] is True
        app.push_screen(tui.Confirm("end it?"), lambda r: seen.append(r))
        await pilot.pause(0.2)
        await pilot.press("n")
        await _wait(pilot, "any other key to say no", lambda: len(seen) == 4)
        assert seen[3] is False


# -- the y/n: end / confirm ---------------------------------------------------------------------------------------
async def test_end_asks_its_question_and_only_yes_ends_the_session(monkeypatch, tmp_path):
    signalled: list = []
    said: list = []
    app = _board(monkeypatch, tmp_path, _rows(2), visible=S[0])
    monkeypatch.setattr(tui.tmux, "end_agent", lambda p: signalled.append(p.id) or 1)
    monkeypatch.setattr(tui.CtxApp, "notify", lambda self, msg, **k: said.append(msg))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert app.control.dispatch({"cmd": "select", "n": 1})["ok"]
        r = app.control.dispatch({"cmd": "end"})
        assert r["ok"] and r["state"]["modal"] == "confirm"
        assert r["state"]["prompt"].startswith("end session s1") and "cc·laptop" in r["state"]["prompt"]
        assert app.control.dispatch({"cmd": "confirm", "yes": False})["ok"]      # "any other key = no"
        await _wait(pilot, "the cancelled notice", lambda: any("cancelled" in m for m in said))
        assert signalled == []                                        # nothing was ended
        assert app.control.dispatch({"cmd": "end"})["ok"]
        await pilot.pause(0.2)
        assert app.control.dispatch({"cmd": "confirm", "yes": True})["ok"]
        await _wait(pilot, "the agent's processes to be signalled", lambda: bool(signalled))
        assert signalled == ["%1"]


async def test_a_confirm_when_none_is_open_says_so(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, _rows(2))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert app.control.dispatch({"cmd": "confirm", "yes": True}) == {"ok": False, "error": "no confirm open"}
        assert app.control.dispatch({"cmd": "cmd", "n": 1}) == {"ok": False, "error": "unknown cmd"}


# -- framing, and a board that can't bind -------------------------------------------------------------------------
async def test_a_line_that_isnt_a_json_object_says_bad_request_and_keeps_the_connection(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, _rows(2))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        pipe = await _endpoint(app)
        bad = [("not json at all", "bad request"), ("[1, 2, 3]", "bad request"), ('{"cmd": "nope"}', "unknown cmd"),
               ('{"n": 1}', "unknown cmd"), ('{"cmd": "slot", "n": "two"}', "no such slot"),
               ('{"cmd": "move", "delta": 4}', "bad request")]
        for n, (line, why) in enumerate(bad, 1):
            pipe.send(line + "\n")
            got = await pipe.read(n, f"a reply to {line!r}")
            assert got[-1] == {"ok": False, "error": why}, (line, got[-1])
        assert pipe.closed is False                                # the endpoint never closed on the junk
        pipe.send(json.dumps({"cmd": "state", "id": "x"}) + "\n")   # and the next request works
        got = await pipe.read(len(bad) + 1, "the request after the bad lines")
        assert got[-1]["ok"] and got[-1]["id"] == "x" and got[-1]["state"]["slots"] == app.slots
        pipe.hang_up()
        await pipe.task                                            # and it ends when the client hangs up


@needs_sockets
async def test_a_real_client_gets_the_same_replies_over_the_socket(monkeypatch, tmp_path):
    sock = _sock(tmp_path)
    app = _board(monkeypatch, tmp_path, _rows(2), sock=sock)
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        reader, writer = await _connect(sock)
        try:
            for bad, why in ((b"not json at all\n", "bad request"), (b'{"cmd": "nope"}\n', "unknown cmd")):
                writer.write(bad)
                await writer.drain()
                (reply,) = await _lines(reader, 1, f"a reply to {bad!r}")
                assert reply["ok"] is False and reply["error"] == why, reply
            await _ask(writer, {"cmd": "state", "id": "x"})         # the connection is still good
            (reply,) = await _lines(reader, 1, "the request after the bad lines")
            assert reply["ok"] and reply["id"] == "x" and reply["state"]["slots"] == app.slots
        finally:
            writer.close()


async def test_a_board_that_cant_bind_its_socket_still_mounts(monkeypatch, tmp_path):
    """A sandbox that refuses unix sockets (EPERM), or a state directory it can't write: the board runs, it just
    can't be driven, and nothing else about it is different."""
    sock = _sock(tmp_path)
    app = _board(monkeypatch, tmp_path, _rows(2), sock=sock, bind=PermissionError(1, "Operation not permitted"))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert app.control._server is None and not sock.exists()
        assert app.query_one("#table").row_count == 2
        assert app.slots == S[:2] and app.selected == S[0]
        assert app.control.dispatch({"cmd": "hello"})["ok"]           # the board still answers its own loop


@needs_sockets
async def test_a_stale_socket_is_unlinked_and_the_file_is_gone_when_the_board_exits(monkeypatch, tmp_path):
    sock = _sock(tmp_path)
    sock.write_text("")                                   # a file no board answers on: stale
    app = _board(monkeypatch, tmp_path, _rows(1), sock=sock)
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert sock.exists() and oct(sock.stat().st_mode & 0o777) == "0o600"
    for _ in range(50):
        if not sock.exists():
            break
        await asyncio.sleep(0.02)
    assert not sock.exists(), f"{sock} outlived the board that made it"


async def test_a_board_that_finds_another_one_answering_leaves_it_alone(monkeypatch, tmp_path):
    """The path belongs to whichever board is really running: a second one never unlinks its socket."""
    sock = _sock(tmp_path)
    kept: list = []
    app = _board(monkeypatch, tmp_path, _rows(1), sock=sock)

    class _Writer:                                      # the other board took the handshake: a connection, not an error
        def close(self): pass
        async def wait_closed(self): pass
    async def _someone_else(*a, **k):
        kept.append(str(a[1] if len(a) > 1 else k.get("path")))
        return None, _Writer()
    monkeypatch.setattr(tui.control.asyncio, "open_unix_connection", _someone_else)
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert app.control._server is None and kept == [str(sock)]   # it asked, and found a live board


# -- the panels, the spend screen, and the rest of the keyboard ----------------------------------------------------
def _pool_tools(**kw):
    """This host's pool commands, answered from the test: the panel reads them instead of running anything."""
    from ctxswitch.client.tui import pool_panel as pp

    def run(argv):
        return pp.Cmd(kw.get("rc", 0), kw.get("out", ""))
    return pp.PoolTools(run=run, which=lambda tool: kw.get("where", "/usr/local/bin/saint-pool"))


async def test_gpu_turns_the_cpu_panel_on_and_says_so_in_the_state(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, _rows(2))
    monkeypatch.setattr(tui.CtxApp, "fetch_metrics", lambda self: None)   # nothing to ask the hub about
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert board_state(app)["panels"] == {"gpu": False, "pool": False}
        assert app.control.dispatch({"cmd": "gpu"})["ok"] is True
        assert app.metrics_on is True and board_state(app)["panels"]["gpu"] is True
        assert app.control.dispatch({"cmd": "metrics"})["ok"] is True     # the binding's own name, the same action
        assert app.metrics_on is False and board_state(app)["panels"]["gpu"] is False


async def test_pool_turns_the_local_pool_panel_on_and_says_so(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, _rows(2))
    app.pool_tools = _pool_tools()
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert board_state(app)["panels"] == {"gpu": False, "pool": False}
        assert app.control.dispatch({"cmd": "pool"})["ok"] is True
        assert app.pool_on is True and board_state(app)["panels"]["pool"] is True
        assert app.control.dispatch({"cmd": "pool_panel"})["ok"] is True
        assert app.pool_on is False and board_state(app)["panels"]["pool"] is False


async def test_spend_opens_the_screen_close_again_and_a_cancel_closes_it(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, _rows(2))
    asked: list = []
    monkeypatch.setattr(tui, "spend_windows", lambda client, host: asked.append(host) or {})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert board_state(app)["modal"] is None
        assert app.control.dispatch({"cmd": "spend"})["ok"] is True
        await pilot.pause(0.2)
        assert isinstance(app.screen, tui.SpendScreen) and board_state(app)["modal"] == "spend"
        assert board_state(app)["options"] == [], "the spend screen offers nothing to pick"
        assert app.control.dispatch({"cmd": "spend"})["ok"] is True       # $ again closes it
        await pilot.pause(0.2)
        assert board_state(app)["modal"] is None
        app.control.dispatch({"cmd": "spend"})
        await pilot.pause(0.2)
        assert app.control.dispatch({"cmd": "cancel"})["ok"] is True      # and so does any way out of a modal
        await _wait(pilot, "the spend screen to close", lambda: board_state(app)["modal"] is None)
        assert not isinstance(app.screen, tui.SpendScreen)


async def test_exchange_with_nothing_selected_says_so(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, _rows(2))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        app.selected = None
        assert app.control.dispatch({"cmd": "exchange"}) == {"ok": False, "error": "nothing selected"}


async def test_detach_outside_tmux_quits_the_board(monkeypatch, tmp_path):
    quit: list = []
    app = _board(monkeypatch, tmp_path, _rows(2), in_tmux=False)
    monkeypatch.setattr(tui.CtxApp, "exit", lambda self: quit.append("exit"))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert app.control.dispatch({"cmd": "detach"})["ok"] is True
        assert quit == ["exit"]


async def test_detach_inside_tmux_leaves_the_board_running(monkeypatch, tmp_path):
    quit: list = []
    app = _board(monkeypatch, tmp_path, _rows(2))
    monkeypatch.setattr(tui.tmux, "detach", lambda: quit.append("detach"))
    monkeypatch.setattr(tui.CtxApp, "exit", lambda self: quit.append("exit"))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert app.control.dispatch({"cmd": "detach"})["ok"] is True
        assert quit == ["detach"], "inside tmux q detaches: the board and every agent stay up"


def _binding_cmds() -> list[str]:
    """Every key the board answers, as a command: its binding's action name, with the two the deck's middle row
    calls gpu and pool. The slot and select_slot bindings take the row's number, so `slot` and `select` do that."""
    out = []
    for b in tui.CtxApp.BINDINGS:
        action = (b.action or "").split("(")[0]
        if action in ("slot", "select_slot"):
            continue
        out += [name for name, method in tui.control.ALIASES.items() if method == f"action_{action}"] or [action]
    return sorted(set(out))


@pytest.mark.parametrize("cmd", _binding_cmds())
async def test_every_board_key_can_be_driven_from_the_socket(cmd, monkeypatch, tmp_path):
    """The socket covers the whole keyboard, so that nothing is missing from it can only be found by asking of the
    bindings themselves: one command per binding, each calling that binding's own action."""
    method = tui.control.ALIASES.get(cmd) or tui.control.ACTIONS.get(cmd)
    hit: list = []
    app = _board(monkeypatch, tmp_path, _rows(2))
    app.pool_tools = _pool_tools()
    for name in dir(tui.CtxApp):
        if name.startswith("action_"):
            monkeypatch.setattr(tui.CtxApp, name, lambda self, _n=name, *a, **k: hit.append(_n))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert method is not None, f"{cmd} names no board action"
        assert cmd in tui.control.ACTIONS or cmd in tui.control.ALIASES, cmd
        reply = app.control.dispatch({"cmd": cmd})
        assert reply["ok"] is True and "state" in reply, reply
        # what it ran: the action the binding says, or that action's own reason for saying nothing happened
        assert hit == [method], (cmd, hit)


async def test_a_command_needing_a_row_says_nothing_is_selected(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, _rows(2))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        for cmd in ("resume", "message", "reply", "message_buffer", "mark_viewed", "exchange", "misplace_move",
                    "misplace_keep", "misplace_drop"):
            app.selected = None
            assert app.control.dispatch({"cmd": cmd}) == {"ok": False, "error": "nothing selected"}, cmd
            app.selected = "ws:webapp"                          # a workstream header answers these itself
            assert app.control.dispatch({"cmd": cmd})["ok"] is True, cmd
        app.selected = app.slots[0]                             # these two answer from the detail or the work,
        for cmd in ("retire", "message_work"):                  # so they get on with a row highlighted at all
            assert app.control.dispatch({"cmd": cmd})["ok"] is True, cmd


async def test_cancel_closes_the_box_the_keyboard_is_typing_in(monkeypatch, tmp_path):
    """/ and m and a put the board's Input up; Esc out of it is what `cancel` does, so a client can get the board
    back from a box it didn't open. The text filter goes with it — that is the binding."""
    app = _board(monkeypatch, tmp_path, _rows(2))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        app.action_filter()
        await pilot.pause(0.2)
        assert board_state(app)["modal"] == "input"
        app.query_one("#filter").value = "s1"                   # typed something: it is the filter live
        assert app.control.dispatch({"cmd": "cancel"})["ok"] is True
        await pilot.pause(0.2)
        assert board_state(app)["modal"] is None and app.input_mode == "filter"
        assert app.query_one("#filter").value == "" and not app.query_one("#filter").display
        app.action_message()                                    # a message box: same way out, nothing sent
        await pilot.pause(0.2)
        assert board_state(app)["modal"] == "input"
        assert app.control.dispatch({"cmd": "cancel"})["ok"] is True
        await pilot.pause(0.2)
        assert board_state(app)["modal"] is None and app.pending_reply is None
