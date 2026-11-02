import json
from pathlib import Path

import pytest


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
