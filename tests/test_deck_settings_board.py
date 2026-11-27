"""D on the board — the Stream Deck's settings screen, and the same three settings over the control socket.

One JSON file is the whole contract between the board and `ctx deck` (docs/deck-settings.md); the board is its only
writer and the socket's `deck_set` is the same door from the wire. So every test here points `deck_settings.FILE` at
its own tmp_path file — nothing in the suite may read or write the operator's live settings — and then checks the
file, not just the screen, because the file is what the deck actually reads.
"""
from __future__ import annotations

import json

import pytest

from ctxswitch.client.tui import app as tui
from ctxswitch.client.tui.control import board_state
from ctxswitch.common import deck_settings
from ctxswitch.common.config import Config

P1 = "laptop/claude/s1"


def _sess(key) -> dict:
    return {"key": key, "host_id": "laptop", "harness": "claude", "session_id": key.rsplit("/", 1)[-1],
            "title": f"session {key.rsplit('/', 1)[-1]}", "cwd": "/r/webapp", "live_status": "idle",
            "status_since": "2026-11-20T11:40:00Z", "last_activity": "2026-11-20T11:58:00Z",
            "started_at": "2026-11-20T11:00:00Z", "repo_root": "/r/webapp", "git_branch": "main", "meta": {},
            "open_asks": [], "parent_key": "", "job": "", "turns": [], "asks": [], "ledger": [], "messages": []}


def _board(monkeypatch, tmp_path, settings: dict | None = None) -> tui.CtxApp:
    """A board whose deck settings file is this test's own, holding ``settings`` (nothing: no file at all, so the
    defaults). tmux and the hub's WebSocket patched out, g's and P's panels off, as every board test does."""
    cfg = Config()
    cfg.hub.token = "t"
    cfg.collector.host_id = "laptop"
    path = tmp_path / "deck-settings"
    if settings is not None:
        path.write_text(json.dumps(settings))
    monkeypatch.setattr(deck_settings, "FILE", path)
    monkeypatch.setattr(tui, "METRICS_FILE", tmp_path / "metrics-panel")
    monkeypatch.setattr(tui, "_pool_panel_state", lambda: False)
    monkeypatch.setattr(tui.tmux, "in_tmux", lambda: False)
    monkeypatch.setattr(tui.tmux, "reap_viewer", lambda *a, **k: None)
    monkeypatch.setattr(tui.tmux, "restore_viewer", lambda *a, **k: None)
    monkeypatch.setattr(tui.CtxApp, "listen_ws", lambda self: None)
    monkeypatch.setattr(tui.CtxApp, "join_tmux", lambda self: None)
    app = tui.CtxApp(cfg, None)

    class _Client:
        def sessions(self, host=None, live_only=False):
            return {"sessions": [_sess(P1)], "change": 1}

        def session(self, key, turns=50):
            return {**_sess(key), "turns": [], "asks": [], "ledger": [], "messages": []}

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

    app.client = _Client()
    return app


def _file(tmp_path) -> dict:
    return json.loads((tmp_path / "deck-settings").read_text())


def _text(app) -> str:
    return app.screen.query_one("#deck_text").render().plain


# -- the screen ----------------------------------------------------------------------------------------------------
def test_D_is_bound_to_the_deck_settings_screen_and_documented():
    bound = {k.strip(): b.action for b in tui.CtxApp.BINDINGS for k in b.key.split(",")}
    assert bound["D"] == "deck_settings"
    assert ("D",) in {keys for keys, _, _ in tui.HELP_KEYS}
    assert any("Stream Deck" in words for _, shown, words in tui.HELP_KEYS if shown == "D")
    assert "D deck" in tui.key_bar(tui.CtxApp.BINDINGS, 200).plain          # and it shows in the key bar


async def test_D_opens_the_screen_naming_each_setting_and_the_key_that_changes_it(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path)
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert board_state(app)["modal"] is None
        await pilot.press("D"); await pilot.pause(0.3)
        assert isinstance(app.screen, tui.DeckSettingsScreen)
        assert board_state(app)["modal"] == "deck_settings"
        assert board_state(app)["options"] == [], "the screen offers nothing to pick"
        text = _text(app)
        assert "Stream Deck" in text
        assert "pages" in text and "scroll" in text and "top row" in text       # both values, one of them the current
        assert "60" in text and "backlight" in text                             # and the number, not only the blocks
        assert "animations" in text and "on" in text
        assert str(tmp_path / "deck-settings") in app.screen.query_one("#deck_path").render().plain
        for close in (["escape"], ["q"], ["D"]):
            await pilot.press(*close); await pilot.pause(0.3)
            assert not isinstance(app.screen, tui.DeckSettingsScreen), close
            if close != ["D"]:
                await pilot.press("D"); await pilot.pause(0.3)                 # reopen: D is the screen's own key too


async def test_t_flips_the_top_row_and_writes_it_straight_to_the_file(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, {"top_row": "pages", "brightness": 60, "animations": True})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        await pilot.press("D"); await pilot.pause(0.3)
        assert _file(tmp_path)["top_row"] == "pages"
        await pilot.press("t"); await pilot.pause(0.3)
        assert _file(tmp_path)["top_row"] == "scroll"                           # saved at once: the deck watches mtime
        assert "scroll" in _text(app).split("top row")[1].split("\n")[0]        # and the line says so now
        assert deck_settings.load_settings(tmp_path / "deck-settings")["top_row"] == "scroll"
        await pilot.press("t"); await pilot.pause(0.3)
        assert _file(tmp_path)["top_row"] == "pages"
        assert _file(tmp_path)["brightness"] == 60 and _file(tmp_path)["animations"] is True   # the rest untouched


async def test_the_backlight_steps_ten_a_press_and_stays_between_10_and_100(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, {"top_row": "pages", "brightness": 60, "animations": True})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        await pilot.press("D"); await pilot.pause(0.3)
        for _ in range(2):
            await pilot.press("plus"); await pilot.pause(0.2)
        assert _file(tmp_path)["brightness"] == 80 and "80" in _text(app)
        for _ in range(6):                                                       # 90, 100, then nothing past it
            await pilot.press("plus"); await pilot.pause(0.2)
        assert _file(tmp_path)["brightness"] == 100
        for _ in range(12):                                                      # down through the floor and past it
            await pilot.press("minus"); await pilot.pause(0.2)
        assert _file(tmp_path)["brightness"] == 10 and "10" in _text(app)
        await pilot.press("minus"); await pilot.pause(0.2)
        assert _file(tmp_path)["brightness"] == 10                               # and it stays there


async def test_a_flips_the_animations_flag_and_the_screen_shows_which_way_it_is(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, {"top_row": "pages", "brightness": 60, "animations": True})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        await pilot.press("D"); await pilot.pause(0.3)
        assert _file(tmp_path)["animations"] is True and _text(app).split("animations")[1].startswith(" on")
        await pilot.press("a"); await pilot.pause(0.3)
        assert _file(tmp_path)["animations"] is False and _text(app).split("animations")[1].startswith(" off")
        await pilot.press("a"); await pilot.pause(0.3)
        assert _file(tmp_path)["animations"] is True


async def test_the_screen_reads_the_file_afresh_each_time_it_opens(monkeypatch, tmp_path):
    """A deck that changed the file, or a second board, is not overwritten by a screen that opened before it."""
    app = _board(monkeypatch, tmp_path, {"top_row": "pages", "brightness": 60, "animations": True})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        await pilot.press("D"); await pilot.pause(0.3)
        await pilot.press("q"); await pilot.pause(0.3)
        (tmp_path / "deck-settings").write_text(json.dumps({"top_row": "scroll", "brightness": 30,
                                                            "animations": False}))
        await pilot.press("D"); await pilot.pause(0.3)
        assert "30" in _text(app) and _text(app).split("animations")[1].startswith(" off")
        await pilot.press("plus"); await pilot.pause(0.3)
        assert _file(tmp_path) == {"top_row": "scroll", "brightness": 40, "animations": False}


async def test_a_board_with_no_settings_file_shows_the_defaults_and_writes_a_whole_one(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, None)
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        await pilot.press("D"); await pilot.pause(0.3)
        assert not (tmp_path / "deck-settings").exists()          # looking changes nothing
        assert _text(app).split("backlight")[1].split("\n")[0].strip().endswith("60")
        await pilot.press("a"); await pilot.pause(0.3)
        assert _file(tmp_path) == {"top_row": "pages", "brightness": 60, "animations": False}


async def test_the_screen_keeps_a_key_the_board_does_not_know_about(monkeypatch, tmp_path):
    """A newer deck's setting must survive a board that has never heard of it (docs/deck-settings.md)."""
    app = _board(monkeypatch, tmp_path, {"top_row": "pages", "brightness": 60, "animations": True, "dial": "left"})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        await pilot.press("D"); await pilot.pause(0.3)
        await pilot.press("t"); await pilot.pause(0.3)
        assert _file(tmp_path)["dial"] == "left" and _file(tmp_path)["top_row"] == "scroll"


async def test_the_board_keys_do_not_leak_through_the_deck_screen(monkeypatch, tmp_path):
    """Inside it, its own keys are the only ones that answer: `a` sets the animations rather than answering a
    session, `t` moves no misplaced prompt, `+` says nothing about review rounds."""
    app = _board(monkeypatch, tmp_path, {"top_row": "pages", "brightness": 60, "animations": True})
    ran: list = []
    for name in ("action_reply", "action_misplace_move", "action_pool_rounds_up", "action_detach"):
        monkeypatch.setattr(tui.CtxApp, name, lambda self, _n=name, *a, **k: ran.append(_n))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        await pilot.press("D"); await pilot.pause(0.3)
        for key in ("a", "t", "plus", "minus"):
            await pilot.press(key); await pilot.pause(0.2)
        assert ran == [], ran
        assert _file(tmp_path) == {"top_row": "scroll", "brightness": 60, "animations": False}   # a flipped the
        # animations, t the top row, + and − cancelled out — and none of them reached the board behind the screen


# -- the socket ----------------------------------------------------------------------------------------------------
async def test_deck_settings_opens_and_closes_over_the_socket(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, {"top_row": "pages", "brightness": 60, "animations": True})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert board_state(app)["modal"] is None
        r = app.control.dispatch({"cmd": "deck_settings"})
        await pilot.pause(0.3)
        assert r["ok"] and isinstance(app.screen, tui.DeckSettingsScreen)
        assert r["state"]["modal"] == "deck_settings" and board_state(app)["modal"] == "deck_settings"
        assert app.control.dispatch({"cmd": "deck_settings"})["ok"] is True     # a toggle, the way `spend` is
        await pilot.pause(0.3)
        assert board_state(app)["modal"] is None and not isinstance(app.screen, tui.DeckSettingsScreen)


async def test_cancel_closes_the_deck_settings_screen(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, {"top_row": "pages", "brightness": 60, "animations": True})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        app.control.dispatch({"cmd": "deck_settings"})
        await pilot.pause(0.3)
        assert board_state(app)["modal"] == "deck_settings"
        assert app.control.dispatch({"cmd": "cancel"})["ok"] is True
        await pilot.pause(0.3)
        assert board_state(app)["modal"] is None and not isinstance(app.screen, tui.DeckSettingsScreen)


async def test_deck_set_writes_what_it_is_given_and_answers_with_the_whole_set(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, {"top_row": "pages", "brightness": 60, "animations": True})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        r = app.control.dispatch({"cmd": "deck_set", "top_row": "scroll"})
        assert r["ok"] is True and r["settings"] == {"top_row": "scroll", "brightness": 60, "animations": True}
        assert _file(tmp_path) == {"top_row": "scroll", "brightness": 60, "animations": True}
        r = app.control.dispatch({"cmd": "deck_set", "brightness": 90, "animations": False})
        assert r["settings"] == {"top_row": "scroll", "brightness": 90, "animations": False}
        assert _file(tmp_path) == r["settings"]
        assert board_state(app)["settings"] == r["settings"]                     # and the state carries it too
        assert app.control.dispatch({"cmd": "deck_set"})["ok"] is True           # nothing asked: nothing to change
        assert _file(tmp_path) == r["settings"]


async def test_deck_set_shows_a_screen_it_changed_and_refuses_a_value_that_is_not_allowed(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, {"top_row": "pages", "brightness": 60, "animations": True})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        app.control.dispatch({"cmd": "deck_settings"})
        await pilot.pause(0.3)
        r = app.control.dispatch({"cmd": "deck_set", "brightness": 7})
        assert r == {"ok": False, "error": "bad setting"}
        assert _file(tmp_path) == {"top_row": "pages", "brightness": 60, "animations": True}   # nothing written
        for bad in ({"brightness": 110}, {"brightness": "80"}, {"brightness": True}, {"top_row": "rows"},
                    {"animations": "yes"}, {"top_row": "scroll", "brightness": 0}):
            assert app.control.dispatch({"cmd": "deck_set", **bad}) == {"ok": False, "error": "bad setting"}, bad
        assert _file(tmp_path) == {"top_row": "pages", "brightness": 60, "animations": True}
        assert app.control.dispatch({"cmd": "deck_set", "animations": False})["ok"] is True
        await pilot.pause(0.3)
        assert "off" in app.screen.query_one("#deck_text").render().plain.split("animations")[1]
        assert board_state(app)["modal"] == "deck_settings"                     # still open, showing the new value


async def test_the_state_carries_the_deck_settings_without_anyone_touching_the_file(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, {"top_row": "scroll", "brightness": 100, "animations": False})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert board_state(app)["settings"] == {"top_row": "scroll", "brightness": 100, "animations": False}
        assert app.control.dispatch({"cmd": "state"})["state"]["settings"]["brightness"] == 100
        assert app.control.dispatch({"cmd": "hello"})["state"]["settings"]["top_row"] == "scroll"
        (tmp_path / "deck-settings").write_text(json.dumps({"brightness": 20}))   # another writer, partly filled
        assert board_state(app)["settings"] == {"top_row": "pages", "brightness": 20, "animations": True}


def test_the_deck_screen_is_a_modal_the_protocol_can_answer_but_offers_nothing_to_pick():
    """`top_modal` is what `pick` and `cancel` act on: the deck screen must answer cancel and never be a choice list."""
    class _Stack:
        def __init__(self, top): self.stack = [object(), top]
    class _App:
        @property
        def screen_stack(self): return self.stack
    app = _App()
    app.stack = [object(), tui.DeckSettingsScreen()]
    assert tui.control.top_modal(app) is app.stack[1] and tui.control.modal_of(app) == "deck_settings"
    assert tui.control.options_of(app) == [] and tui.control.prompt_of(app) == ""
    app.stack = [object(), tui.Help()]
    assert tui.control.top_modal(app) is None and tui.control.modal_of(app) is None
