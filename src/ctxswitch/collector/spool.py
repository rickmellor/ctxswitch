"""Drain the hook spool: one JSON file per Claude hook / pi extension event (written by ctx-hook and
pi_ext/ctxswitch.ts as a dot-file, then renamed). Each record is queued in the outbox as a "hook" event and the
file deleted only after the outbox commit, so a crash between the two re-sends (the hub dedupes by name).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("ctxswitch.collector")


def spool_dir(configured: str = "") -> Path:
    return Path(os.path.expanduser(configured or os.environ.get("CTXSWITCH_SPOOL") or "~/.local/state/ctxswitch/spool"))


def _order(p: Path) -> tuple[int, str]:
    head = p.name.split("-", 1)[0]
    return (int(head) if head.isdigit() else 0, p.name)


def drain(spool: Path, emit: Callable[[str, dict[str, Any]], None]) -> int:
    if not spool.is_dir():
        return 0
    n = 0
    for p in sorted((p for p in spool.glob("*.json") if not p.name.startswith(".")), key=_order):
        try:
            rec = json.loads(p.read_text())
            if not isinstance(rec, dict) or not all(isinstance(rec.get(k), str) for k in ("harness", "session_id", "event")):
                raise ValueError("not a hook record")
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as e:
            log.warning("spool: moving bad file %s aside (%s)", p.name, e)
            (spool / "bad").mkdir(exist_ok=True)
            os.replace(p, spool / "bad" / p.name)
            continue
        emit("hook", {"harness": rec["harness"], "session_id": rec["session_id"], "name": p.name,
                      "event": rec["event"], "ts": rec.get("ts") or "", "data": rec.get("data") or {}})
        p.unlink(missing_ok=True)
        n += 1
    return n
