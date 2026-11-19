"""[ ] on the board: the session list one line shorter or taller, remembered; \\ puts its height back to automatic.

The list grows with its rows up to half the body (the CSS cap). [ ] replaces that cap with a line count of your own,
held in LIST_FILE: below it the list still shrinks to fit fewer rows, above it the rows scroll inside it."""
from __future__ import annotations

import pytest

from textual.css.scalar import Unit

from ctxswitch.client.tui import app as tui
from tests.test_metrics_board import FakeClient, _board, _sess


def _rows(n: int) -> list[dict]:
    """n sessions, so the list has n rows to show."""
    return [_sess(f"laptop/claude/s{i}") for i in range(1, n + 1)]


def _list_board(monkeypatch, tmp_path, n=3, rows=None) -> tui.CtxApp:
    """A board whose list cap file is this run's tmp_path, with n sessions (or the rows given)."""
    app = _board(monkeypatch, tmp_path, rows=rows if rows is not None else _rows(n))
    monkeypatch.setattr(tui, "LIST_FILE", tmp_path / "list-rows")
    app.list_rows = tui._list_rows()                     # as __init__ reads it, now against this run's file
    app.client = FakeClient(list(rows) if rows is not None else _rows(n))
    app.said: list[str] = []
    app.notify = lambda m, **k: app.said.append(m)
    return app


def _cap(table) -> object:
    """What caps the list's height: "auto" for the automatic half (the CSS 50% of the body), or [ ]'s line count."""
    s = table.styles.max_height
    return s.value if s is not None and s.unit is Unit.CELLS else "auto"


def _file(tmp_path) -> str | None:
    """What LIST_FILE says, or None when there isn't one."""
    p = tmp_path / "list-rows"
    return p.read_text().strip() if p.exists() else None


def _table(app):
    return app.query_one("#table")


@pytest.mark.asyncio
async def test_with_no_cap_the_list_keeps_the_automatic_half_and_fits_three_rows(monkeypatch, tmp_path):
    app = _list_board(monkeypatch, tmp_path, n=3)
    async with app.run_test(size=(62, 79)) as pilot:
        await pilot.pause()
        assert _file(tmp_path) is None                                # nothing remembered: the CSS half
        assert _cap(_table(app)) == "auto"
        assert _table(app).size.height == 3                           # three sessions, three lines


@pytest.mark.asyncio
async def test_two_presses_of_the_right_bracket_remember_five_lines(monkeypatch, tmp_path):
    app = _list_board(monkeypatch, tmp_path, n=3)
    async with app.run_test(size=(62, 79)) as pilot:
        await pilot.pause()
        await pilot.press("right_square_bracket"); await pilot.pause()
        await pilot.press("right_square_bracket"); await pilot.pause()
        assert _file(tmp_path) == "5"
        assert app.said[-1] == "session list up to 5 lines"
        assert _cap(_table(app)) == 5
        assert _table(app).size.height == 3                           # three rows still take only three lines


@pytest.mark.asyncio
async def test_the_left_bracket_gives_a_line_back_but_never_the_last_two(monkeypatch, tmp_path):
    app = _list_board(monkeypatch, tmp_path, n=10)
    async with app.run_test(size=(62, 79)) as pilot:
        await pilot.pause()
        await pilot.press("left_square_bracket"); await pilot.pause()
        assert _file(tmp_path) == "9" and _table(app).size.height == 9
        for _ in range(15):                                           # never below two lines, however many presses
            await pilot.press("left_square_bracket"); await pilot.pause()
        assert _file(tmp_path) == "2" and _table(app).size.height == 2


@pytest.mark.asyncio
async def test_a_cap_read_at_startup_holds_ten_rows_to_four_lines(monkeypatch, tmp_path):
    (tmp_path / "list-rows").write_text("4\n")                        # what the last run left behind
    app = _list_board(monkeypatch, tmp_path, rows=_rows(10))
    async with app.run_test(size=(62, 79)) as pilot:
        await pilot.pause()
        assert app.list_rows == 4
        assert _table(app).size.height == 4                           # four lines, the other six scrolling
        assert _file(tmp_path) == "4"                                 # reading it changes nothing


@pytest.mark.asyncio
async def test_a_backslash_forgets_the_cap_and_goes_back_to_automatic(monkeypatch, tmp_path):
    (tmp_path / "list-rows").write_text("6\n")
    app = _list_board(monkeypatch, tmp_path, rows=_rows(10))
    async with app.run_test(size=(62, 79)) as pilot:
        await pilot.pause()
        assert _table(app).size.height == 6
        await pilot.press("backslash"); await pilot.pause()
        assert _file(tmp_path) is None                                # the file is gone
        assert app.list_rows is None
        assert _cap(_table(app)) == "auto"
        assert app.said[-1] == "session list automatic"


@pytest.mark.asyncio
async def test_a_file_this_board_never_wrote_leaves_the_height_automatic(monkeypatch, tmp_path):
    (tmp_path / "list-rows").write_text("abc\n")                       # garbage: no cap, not a crash
    app = _list_board(monkeypatch, tmp_path, n=3)
    async with app.run_test(size=(62, 79)) as pilot:
        await pilot.pause()
        assert app.list_rows is None
        assert _cap(_table(app)) == "auto"
        assert _table(app).size.height == 3


def test_the_brackets_are_bound_and_documented():
    """[ ] \\ are board keys, and the ? screen says what they do."""
    actions = {b.key: b.action for b in tui.CtxApp.BINDINGS}
    assert (actions["left_square_bracket"], actions["right_square_bracket"]) == ("list_shorter", "list_taller")
    assert actions["backslash"] == "list_auto"
    entry = next(e for e in tui.HELP_KEYS if "left_square_bracket" in e[0])
    assert "right_square_bracket" in entry[0] and "backslash" in entry[0]
    assert any(shown == "[ ] \\" for _, shown, _ in tui.HELP_KEYS)
