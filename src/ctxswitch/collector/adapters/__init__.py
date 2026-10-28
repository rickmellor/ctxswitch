"""Transcript parsers. Each adapter is an incremental, picklable line parser:

    p = ClaudeParser(...); p.feed(line) for each complete line; turns = p.drain(); meta = p.meta

The indexer persists ``p.state()`` with the byte offset so a restart resumes mid-file without re-reading.
"""
from __future__ import annotations

import json
from typing import Any


def parse_json_line(line: bytes) -> dict[str, Any] | None:
    """A transcript line, or None for blank/corrupt lines (seen in the wild on 2.1.312)."""
    line = line.strip()
    if not line:
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None
