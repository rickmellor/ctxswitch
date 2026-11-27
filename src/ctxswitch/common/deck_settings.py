"""The Stream Deck's settings: one small JSON file the board writes and `ctx deck` reads, see docs/deck-settings.md.

Missing file or unreadable JSON means the defaults; a bad value means the default for that key only. Unknown keys
are kept when the file is rewritten, so an older board and a newer deck can share it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

FILE = Path(os.environ.get("CTXSWITCH_DECK_SETTINGS") or Path.home() / ".local/state/ctxswitch/deck-settings")
TOP_ROW = ("pages", "scroll")
DEFAULTS: dict = {"top_row": "pages", "brightness": 60, "animations": True}


def _valid(key: str, value):
    """``value`` if it is an allowed value for ``key``, else None."""
    if key == "top_row":
        return value if value in TOP_ROW else None
    if key == "brightness":
        return value if isinstance(value, int) and not isinstance(value, bool) and 10 <= value <= 100 else None
    if key == "animations":
        return value if isinstance(value, bool) else None
    return value


def load_settings(path: Path = FILE) -> dict:
    """The settings with every default filled in and every value checked."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    out = dict(raw)
    for key, default in DEFAULTS.items():
        got = _valid(key, raw.get(key))
        out[key] = default if got is None else got
    return out


def save_settings(path: Path, settings: dict) -> None:
    """Write the whole file atomically, keeping keys we do not know about."""
    path = Path(path)
    current = load_settings(path)
    current.update(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)
