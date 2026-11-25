import json
import os
import socket
import tempfile
from pathlib import Path

import pytest


def unix_sockets_ok() -> bool:
    """Whether this machine lets a test bind a unix socket, which collector delivery and every tmux server need.
    Some sandboxes refuse it outright (EPERM on socket/bind), the way they refuse tmux; a capability the kernel won't
    do is skipped the way a missing binary is, and the test still runs on a normal host."""
    p = Path(tempfile.gettempdir()) / f"ctxswitch-probe-{os.getpid()}.sock"
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError:
        return False
    try:
        s.bind(str(p))
    except OSError:
        return False
    finally:
        s.close()
        p.unlink(missing_ok=True)
    return True


@pytest.fixture(autouse=True)
def _private_shells(tmp_path_factory, monkeypatch):
    """No test reads or writes the real ~/.local/state/ctxswitch/shells: real `ctx shell` transcripts there would
    leak into every collector a test builds with the default config."""
    if "CTXSWITCH_SHELLS" not in __import__("os").environ:
        monkeypatch.setenv("CTXSWITCH_SHELLS", str(tmp_path_factory.mktemp("shells")))


@pytest.fixture(autouse=True)
def _no_real_tmux_or_popup_log(tmp_path, monkeypatch):
    """No test writes the operator's ~/.local/state/ctxswitch/popup.log, and none can reach their tmux server: the
    suite is often run from a pane of it, and TMUX would point a real display-popup or display-message at their
    screen. A test that wants tmux patches the calls it expects."""
    from ctxswitch.client import notify
    monkeypatch.setattr(notify, "LOG", tmp_path / "popup.log")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setenv("CTXSWITCH_NO_SOUND", "1")             # and never plays a sound on their speakers


@pytest.fixture(autouse=True)
def _no_real_board_state(tmp_path, monkeypatch):
    """No test reads or writes the operator's own board state: a [ ] list cap they set on their board (list-rows)
    made a row-overflow test see 5 rows where it expected 10. A test that wants a cap writes its own."""
    from ctxswitch.client.tui import app as tui
    monkeypatch.setattr(tui, "LIST_FILE", tmp_path / "list-rows")
    # and P's pool panel: the real ~/.local/state file once said "on" (Rick opened the panel live) and every board
    # test then started with the pool panel up, pushing g's panel off the key bar; the P tests wrote to it too
    monkeypatch.setattr(tui, "POOL_PANEL_FILE", tmp_path / "pool-panel")


@pytest.fixture
def jsonl(tmp_path: Path):
    """Write dict rows as a JSONL file; returns (path, offsets-after-each-line)."""
    def _w(rows, name="t.jsonl", trailing_partial: str | None = None):
        p = tmp_path / name
        offs = []
        with open(p, "wb") as f:
            for r in rows:
                f.write((json.dumps(r) + "\n").encode()); offs.append(f.tell())
            if trailing_partial is not None:
                f.write(trailing_partial.encode())
        return p, offs
    return _w


def feed_all(parser, rows):
    """Feed dict rows to a parser with synthetic offsets, then drain."""
    for i, r in enumerate(rows):
        parser.feed(r, (i + 1) * 100)
    return parser
