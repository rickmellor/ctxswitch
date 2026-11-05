"""The board drives its terminals through a Runtime: with a driver that isn't tmux, Enter/n/k/resume go to the
driver and never to client/tmux.py, and the gates that used to need tmux open for it. tests/test_remote_board.py
covers the tmux driver's own behaviour; here the driver is a fake that writes down what it was asked."""
from __future__ import annotations

import pytest

from ctxswitch.client.tui import app as tui
from ctxswitch.common.config import Config

pytestmark = pytest.mark.asyncio

HOME = "/home/someone"


@pytest.fixture(autouse=True)
def _home(monkeypatch):
    monkeypatch.setenv("HOME", HOME)


def _sess(key, *, status="ended", ended=True, title="ctx switch", host="laptop", **kw):
    now = "2026-11-04T12:00:00Z"
    d = {"key": key, "host_id": host, "harness": "claude", "session_id": key.rsplit("/", 1)[-1], "cwd": f"{HOME}/repos/x",
         "title": title, "live_status": status, "ended": ended, "started_at": now, "last_at": now, "updated_at": now,
         "pid": 4242 if not ended else None, "unseen": 0}
    d.update(kw)
    return d


class FakeClient:
    hosts_here = ["laptop", "gpu-host"]

    def __init__(self, rows):
        self.rows = rows
        self.viewed_keys = []

    def sessions(self, host=None, live_only=False):
        return {"sessions": [dict(x) for x in self.rows], "change": 1}

    def session(self, key, turns=50):
        return None

    def attention(self, host=None):
        return {"items": []}

    def hosts(self):
        return [{"host_id": h} for h in self.hosts_here]

    def spend(self, frm, to, host=None):
        return None

    def viewed(self, key, at):
        self.viewed_keys.append(key)

    def ws_url(self):
        return "ws://x/v1"


class FakeRuntime:
    """A driver that remembers every call and answers the reach words it was given."""

    name = "fake"

    def __init__(self, reach=None):
        self.calls: list[tuple] = []
        self.reach_words = dict(reach or {})

    def available(self):
        return True

    def reach(self, rows, this_host, ssh):
        self.calls.append(("reach", [r["key"] for r in rows], this_host))
        return {r["key"]: self.reach_words.get(r["key"], "pane") for r in rows}

    def open(self, row, *, ssh):
        self.calls.append(("open", row["key"]))
        return f"showing {row['title']}"

    def start(self, host, harness, cwd, *, profile=None, profiles=None, ssh, this_host=None):
        self.calls.append(("start", host, harness, cwd, profile))
        return f"new {harness} on {host} in t9"

    def resume_many(self, rows, *, ssh):
        self.calls.append(("resume_many", [r["key"] for r in rows]))
        return f"resumed {len(rows)}"

    def end(self, row, *, ssh, this_host=None):
        self.calls.append(("end", row["key"]))
        return f"ended {row['title']}"

    def visible_keys(self, rows):
        return set()

    def where(self, row):
        return None


def _board(monkeypatch, rows, rt, in_tmux=False) -> tui.CtxApp:
    cfg = Config(); cfg.hub.token = "t"; cfg.collector.host_id = "laptop"
    monkeypatch.setattr(tui.tmux, "in_tmux", lambda: in_tmux)
    monkeypatch.setattr(tui.tmux, "ssh_target", lambda h, ssh: f"{h}.example" if h != "laptop" else None)
    monkeypatch.setattr(tui.CtxApp, "listen_ws", lambda self: None)
    monkeypatch.setattr(tui.CtxApp, "join_tmux", lambda self: None)
    for name in ("restore_viewer", "reap_viewer", "panes", "visible_key", "on_screen_keys", "reach_all"):
        monkeypatch.setattr(tui.tmux, name, lambda *a, _n=name, **k: pytest.fail(f"tmux.{_n} called under another runtime"))
    app = tui.CtxApp(cfg, None, rt)
    app.client = FakeClient(rows)
    return app


async def _settle(app, pilot):
    await pilot.pause(0.6)
    if isinstance(app.screen, tui.StartScreen):
        await pilot.press("escape")
        await pilot.pause(0.4)


async def test_the_default_runtime_is_tmux():
    cfg = Config(); cfg.hub.token = "t"
    assert tui.CtxApp(cfg, None).rt.name == "tmux"


async def test_a_refresh_asks_the_driver_for_reach_not_tmux(monkeypatch):
    rt = FakeRuntime({"laptop/claude/a": "pane", "gpu-host/claude/b": "ssh"})
    app = _board(monkeypatch, [_sess("laptop/claude/a", status="idle", ended=False), _sess("gpu-host/claude/b", host="gpu-host")], rt)
    async with app.run_test(size=(60, 30)) as pilot:
        await _settle(app, pilot)
        assert app.reach == {"laptop/claude/a": "pane", "gpu-host/claude/b": "ssh"}
        assert ("reach", ["laptop/claude/a", "gpu-host/claude/b"], "laptop") in rt.calls


async def test_enter_outside_tmux_opens_through_the_driver(monkeypatch):
    rt = FakeRuntime()
    app = _board(monkeypatch, [_sess("laptop/claude/a", status="idle", ended=False)], rt)
    async with app.run_test(size=(60, 30)) as pilot:
        await _settle(app, pilot)
        app.selected = "laptop/claude/a"
        await pilot.press("enter")
        await pilot.pause(0.6)
        assert ("open", "laptop/claude/a") in rt.calls
        assert "laptop/claude/a" in app.client.viewed_keys


async def test_k_outside_tmux_ends_through_the_driver_after_y(monkeypatch):
    rt = FakeRuntime()
    app = _board(monkeypatch, [_sess("laptop/claude/a", status="idle", ended=False)], rt)
    async with app.run_test(size=(60, 30)) as pilot:
        await _settle(app, pilot)
        app.selected = "laptop/claude/a"
        await pilot.press("k")
        await pilot.pause(0.3)
        assert isinstance(app.screen, tui.Confirm)
        await pilot.press("y")
        await pilot.pause(0.6)
        assert ("end", "laptop/claude/a") in rt.calls


async def test_starting_goes_to_the_driver_with_the_host(monkeypatch):
    rt = FakeRuntime()
    app = _board(monkeypatch, [_sess("laptop/claude/a")], rt)
    async with app.run_test(size=(60, 30)) as pilot:
        await _settle(app, pilot)
        app.start_agent("shell", "~")                 # the start workers are exclusive: one at a time
        await pilot.pause(0.6)
        app.start_remote_agent("gpu-host", "claude", "/srv/x")
        await pilot.pause(0.6)
        assert ("start", "laptop", "shell", "~", None) in rt.calls
        assert ("start", "gpu-host", "claude", "/srv/x", None) in rt.calls


async def test_resume_many_goes_to_the_driver(monkeypatch):
    rt = FakeRuntime()
    app = _board(monkeypatch, [_sess("laptop/claude/a"), _sess("laptop/claude/b")], rt)
    async with app.run_test(size=(60, 30)) as pilot:
        await _settle(app, pilot)
        app.resume_many_sessions(["laptop/claude/a", "laptop/claude/b"])
        await pilot.pause(0.8)
        assert ("resume_many", ["laptop/claude/a", "laptop/claude/b"]) in rt.calls


async def test_the_tmux_runtime_outside_tmux_still_refuses_to_start(monkeypatch):
    from ctxswitch.client.runtime.tmux import TmuxRuntime
    app = _board(monkeypatch, [_sess("laptop/claude/a")], TmuxRuntime())
    for name in ("restore_viewer", "reap_viewer", "panes", "visible_key", "on_screen_keys", "reach_all"):
        monkeypatch.setattr(tui.tmux, name, lambda *a, **k: {} if name == "reach_all" else None)
    monkeypatch.setattr(tui.tmux, "panes", lambda *a, **k: [])
    monkeypatch.setattr(tui.tmux, "on_screen_keys", lambda *a, **k: set())
    notes = []
    monkeypatch.setattr(app, "notify", lambda msg, **k: notes.append(msg))
    async with app.run_test(size=(60, 30)) as pilot:
        await _settle(app, pilot)
        app.action_new_session()
        assert any("needs the tmux manager" in n for n in notes)
