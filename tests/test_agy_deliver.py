"""Messages into an Antigravity session: typed into its tmux pane while it sits idle there, else written to the
inbox its hooks read. No test here runs a real tmux: every call goes to a fake ``run`` that only records argv."""
import json
import os
from pathlib import Path

import httpx
import pytest
from test_messages import Hub2, _collector

from ctxswitch.collector import deliver
from ctxswitch.common.schema import Liveness
from ctxswitch.hub.store import Store

NOW = "2026-10-29T10:00:00Z"
SID = "4abc123"
KEY = f"laptop/agy/{SID}"
AT = "@1.%7"                                # "<session>:<window_id>.<pane_id>"


TTY = "/dev/pts/9"                          # the pane's terminal


@pytest.fixture(autouse=True)
def _proc(tmp_path, monkeypatch):
    """A /proc where pid 99 is agy on the pane's terminal (ctx's launcher runs it under `sh -c`, so the pane's own
    command is "sh"), and pid 98 is a bash that took the terminal over after agy exited."""
    for pid, comm in ((99, "agy"), (98, "bash")):
        d = tmp_path / "proc" / str(pid)
        (d / "fd").mkdir(parents=True)
        (d / "comm").write_text(comm + "\n")
        os.symlink(TTY, d / "fd" / "0")
    monkeypatch.setattr(deliver, "PROC", str(tmp_path / "proc"))


class Cmd:
    """A tmux server that answers what it's told. ``fails`` names the subcommand that returns non-zero;
    ``command`` is what the pane runs."""
    def __init__(self, command="agy", fails=(), screen=None):
        self.command, self.fails, self.calls = command, list(fails), []
        self.screen = IDLE_SCREEN if screen is None else screen

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        bad = next((f for f in self.fails if f in argv), None)
        out = (TTY + "\n" if any("pane_tty" in a for a in argv)
               else self.screen if "capture-pane" in argv else "")
        return _R(1 if bad else 0, out, "" if bad is None else f"{bad} failed")


# agy 1.5.12's screens, as the spike captured them: waiting at an empty input box, and its trust dialog
IDLE_SCREEN = """  MANGO
────────────────────────────────────────
>
────────────────────────────────────────
? for shortcuts                       auto · Gemini 4 Hydrogen · medium
"""
DIALOG_SCREEN = """Do you trust the contents of this project?
> Yes, I trust this folder
  No, exit
  ↑/↓ Navigate · enter Confirm
"""
DRAFT_SCREEN = IDLE_SCREEN.replace("\n>\n", "\n> half a thought of the operator's\n")


class _R:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _argv(run):
    return [a for a, _ in run.calls]


async def test_idle_in_tmux_is_typed_into_its_pane(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    slept, real_sleep = [], deliver.asyncio.sleep
    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr(deliver.asyncio, "sleep", fake_sleep)   # restored after the test; no real waiting
    run = Cmd()
    assert await deliver.deliver_agy(Liveness(KEY, "idle", pid=99, tmux_at=AT), SID, "run the tests", 7, run=run,
                                     inbox_dir=inbox) is None
    assert _argv(run) == [["tmux", "display-message", "-p", "-t", "%7", "#{pane_tty}"],
                          ["tmux", "capture-pane", "-p", "-t", "%7"],
                          ["tmux", "load-buffer", "-b", "ctx-7", "-"],
                          ["tmux", "paste-buffer", "-p", "-d", "-b", "ctx-7", "-t", "%7"],
                          ["tmux", "send-keys", "-t", "%7", "Enter"]]
    assert run.calls[2][1].get("input") == deliver.HEADER + "run the tests"      # the text goes in on stdin
    assert all(kw.get("timeout") == deliver.TMUX_TIMEOUT for _, kw in run.calls)  # every call has its own 3 s
    assert slept == [deliver.PASTE_SETTLE_S] and 0.5 <= deliver.PASTE_SETTLE_S <= 2   # Enter only once it landed
    assert not inbox.exists()                                                    # the pane took it: nothing queued


async def test_a_pane_that_no_longer_runs_agy_falls_back_to_the_inbox(tmp_path):
    inbox = tmp_path / "inbox"
    run = Cmd()
    assert await deliver.deliver_agy(Liveness(KEY, "idle", pid=98, tmux_at=AT), SID, "x", 1, run=run,
                                     inbox_dir=inbox) is None
    assert _argv(run) == [["tmux", "display-message", "-p", "-t", "%7", "#{pane_tty}"]]
    assert [json.loads(l) for l in (inbox / f"{SID}.jsonl").read_text().splitlines()] == [
        {"msg_id": 1, "text": deliver.HEADER + "x"}]


async def test_a_failing_tmux_call_falls_back_to_the_inbox(tmp_path):
    inbox = tmp_path / "inbox"
    run = Cmd(fails=["load-buffer"])
    assert await deliver.deliver_agy(Liveness(KEY, "idle", pid=99, tmux_at=AT), SID, "x", 2, run=run,
                                     inbox_dir=inbox) is None
    assert [json.loads(l)["msg_id"] for l in (inbox / f"{SID}.jsonl").read_text().splitlines()] == [2]
    assert "paste-buffer" not in str(_argv(run))             # nothing pasted from a buffer that never loaded


async def test_a_hung_tmux_is_the_inbox_too(tmp_path):
    def boom(argv, **kw):
        raise OSError("tmux: no server")
    assert await deliver.deliver_agy(Liveness(KEY, "idle", tmux_at=AT), SID, "x", 3, run=boom,
                                     inbox_dir=tmp_path / "i") is None
    assert (tmp_path / "i" / f"{SID}.jsonl").exists()


async def test_busy_or_no_tmux_goes_straight_to_the_inbox(tmp_path):
    inbox = tmp_path / "inbox"
    run = Cmd()
    for live in (Liveness(KEY, "busy", pid=99, tmux_at=AT), Liveness(KEY, "idle", pid=99), Liveness(KEY, "unknown", tmux_at=AT)):
        assert await deliver.deliver_agy(live, SID, "x", 4, run=run, inbox_dir=inbox) is None
    assert run.calls == []                                   # a busy session isn't typed over: its hook takes it
    assert [json.loads(l)["msg_id"] for l in (inbox / f"{SID}.jsonl").read_text().splitlines()] == [4, 4, 4]


async def test_two_messages_keep_their_order_in_one_file(tmp_path):
    inbox = tmp_path / "inbox"
    for i, body in enumerate(("first thing", "second thing"), start=1):
        assert await deliver.deliver_agy(Liveness(KEY, "busy"), SID, body, i, run=Cmd(), inbox_dir=inbox) is None
    p = inbox / f"{SID}.jsonl"
    rows = [json.loads(l) for l in p.read_text().splitlines()]
    assert [r["text"] for r in rows] == [deliver.HEADER + "first thing", deliver.HEADER + "second thing"]
    assert [r["msg_id"] for r in rows] == [1, 2]
    assert oct(os.stat(p).st_mode & 0o777) == "0o600" and oct(os.stat(inbox).st_mode & 0o777) == "0o700"
    assert await deliver.deliver_agy(Liveness(KEY, "busy"), SID, "third", 3, from_key="devbox/pi/p1",
                                     from_title="api work", run=Cmd(), inbox_dir=inbox) is None
    assert json.loads(p.read_text().splitlines()[-1])["text"] == deliver.agent_header("devbox/pi/p1", "api work", "agy") + "third"


async def test_an_inbox_that_cant_be_written_is_the_only_failure(tmp_path):
    assert await deliver.deliver_agy(Liveness(KEY, "busy"), SID, "x", 5, run=Cmd(),
                                     inbox_dir=Path("/proc/can't")) is not None


def test_the_agy_header_names_the_reply_tool():
    h = deliver.agent_header("devbox/agy/a1", "api work", "agy")
    assert deliver.REPLY_TOOL["agy"] in h and f'session "devbox/agy/a1"' in h
    assert deliver.message_text("hi", "agy") == deliver.HEADER + "hi"       # agy would read it as the operator's own
    assert deliver.message_text("hi", "pi") == "hi"


async def test_deliver_one_sends_an_agy_message_once(tmp_path, monkeypatch):
    col = _collector(tmp_path, monkeypatch)
    inbox, hub = tmp_path / "inbox", Hub2()
    monkeypatch.setattr(deliver, "agy_inbox_dir", lambda: inbox)
    monkeypatch.setattr(deliver, "PASTE_SETTLE_S", 0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(hub.handler)) as client:
        assert not await col.deliver_one(client, {"id": 1, "key": KEY, "body": "go"})    # not live here: nothing written
        col.live = {KEY: Liveness(KEY, "idle", pid=99, tmux_at=None)}
        assert await col.deliver_one(client, {"id": 1, "key": KEY, "body": "go"})         # idle outside tmux: the inbox
        assert hub.reports == [("/v1/messages/1/status", {"host_id": "laptop", "status": "sent", "error": None})]
        hub.code = 503
        assert not await col.deliver_one(client, {"id": 1, "key": KEY, "body": "go"})      # report lost: retried ...
        hub.code = 409
        assert await col.deliver_one(client, {"id": 1, "key": KEY, "body": "go"})
        assert len((inbox / f"{SID}.jsonl").read_text().splitlines()) == 1                 # ... written once
        assert await col.deliver_one(client, {"id": 2, "key": KEY, "body": "again", "from_key": "devbox/pi/p1"})
    assert [json.loads(l)["text"] for l in (inbox / f"{SID}.jsonl").read_text().splitlines()] == [
        deliver.HEADER + "go", deliver.agent_header("devbox/pi/p1", None, "agy") + "again"]


def test_the_outbox_now_offers_agy_messages(tmp_path):
    store = Store(tmp_path / "hub.db")
    keys = [KEY, "laptop/shell/s1", "laptop/claude-win/w1"]
    for key in keys:
        h, harness, sid = key.split("/")
        store.upsert_session({"key": key, "host_id": h, "harness": harness, "session_id": sid, "title": "T", "cwd": "/r",
                              "last_activity": NOW})
    store.set_liveness("laptop", [{"key": k, "status": "idle", "status_since": NOW, "pid": 1} for k in keys])
    a, b, c = (store.add_message(k, "hi") for k in keys)
    assert [m["id"] for m in store.outbox("laptop")] == [a]           # the agy one goes out; the other two can't take one
    assert store.outbox("laptop")[0]["harness"] == "agy"


async def test_a_dialog_or_a_half_typed_prompt_is_never_typed_into(tmp_path):
    """Enter on a permission or trust dialog would pick its highlighted option, and typing over the operator's own
    draft would send it: either way the message waits in the inbox for the hooks."""
    for screen in (DIALOG_SCREEN, DRAFT_SCREEN):
        inbox = tmp_path / f"inbox{len(screen)}"
        run = Cmd(screen=screen)
        assert await deliver.deliver_agy(Liveness(KEY, "idle", pid=99, tmux_at=AT), SID, "x", 3, run=run,
                                         inbox_dir=inbox) is None
        assert [a[1] for a in _argv(run)] == ["display-message", "capture-pane"]      # nothing typed, no Enter
        assert json.loads((inbox / f"{SID}.jsonl").read_text())["msg_id"] == 3


def test_agy_under_ctx_s_sh_launcher_is_still_found_on_its_pane(tmp_path):
    """The pane says "sh" (ctx runs agents as `sh -c "…; ctx tmux-exit"`): what counts is that the session's own pid
    is agy on the pane's terminal. A pid that's gone, another program, or another terminal is not."""
    assert deliver._agy_on_tty(99, TTY) is True
    assert deliver._agy_on_tty(98, TTY) is False                       # bash on the terminal now
    assert deliver._agy_on_tty(99, "/dev/pts/3") is False              # agy, but on another terminal
    assert deliver._agy_on_tty(12345, TTY) is False and deliver._agy_on_tty(None, TTY) is False
