import json
from pathlib import Path

import pytest


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
