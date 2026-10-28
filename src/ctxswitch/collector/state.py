"""Collector-local SQLite: per-file parse progress and the outbox of events not yet acknowledged by the hub.

The outbox is what makes an unreachable hub harmless: events accumulate here and replay in order on reconnect.
Idempotence on the hub side is keyed by (session key, turn idx, offset_end), so a replayed batch is a no-op.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    harness TEXT NOT NULL,
    session_key TEXT,
    offset INTEGER NOT NULL DEFAULT 0,
    parser_state TEXT,
    size INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    turns_emitted INTEGER NOT NULL DEFAULT 0,
    ended INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS outbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class CollectorState:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    # -- files ----------------------------------------------------------------------------------------------------
    def file(self, path: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT path,harness,session_key,offset,parser_state,size,mtime,turns_emitted,ended FROM files WHERE path=?", (path,)).fetchone()
        if r is None:
            return None
        return {"path": r[0], "harness": r[1], "session_key": r[2], "offset": r[3],
                "parser_state": json.loads(r[4]) if r[4] else None, "size": r[5], "mtime": r[6],
                "turns_emitted": r[7], "ended": bool(r[8])}

    def save_file(self, path: str, harness: str, session_key: str | None, offset: int, parser_state: dict | None,
                  size: int, mtime: float, turns_emitted: int, ended: bool = False) -> None:
        self.db.execute(
            "INSERT INTO files(path,harness,session_key,offset,parser_state,size,mtime,turns_emitted,ended) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET harness=excluded.harness, session_key=excluded.session_key, offset=excluded.offset, "
            "parser_state=excluded.parser_state, size=excluded.size, mtime=excluded.mtime, turns_emitted=excluded.turns_emitted, ended=excluded.ended",
            (path, harness, session_key, offset, json.dumps(parser_state) if parser_state is not None else None, size, mtime, turns_emitted, int(ended)))

    def files(self) -> Iterator[dict[str, Any]]:
        for r in self.db.execute("SELECT path FROM files"):
            f = self.file(r[0])
            if f: yield f

    # -- outbox ---------------------------------------------------------------------------------------------------
    def enqueue(self, kind: str, payload: dict[str, Any]) -> int:
        cur = self.db.execute("INSERT INTO outbox(kind,payload,created) VALUES(?,?,?)", (kind, json.dumps(payload), time.time()))
        return int(cur.lastrowid)

    def pending(self, limit: int = 200) -> list[tuple[int, str, dict[str, Any]]]:
        return [(r[0], r[1], json.loads(r[2])) for r in
                self.db.execute("SELECT seq,kind,payload FROM outbox ORDER BY seq LIMIT ?", (limit,))]

    def ack(self, upto_seq: int) -> None:
        self.db.execute("DELETE FROM outbox WHERE seq<=?", (upto_seq,))

    def pending_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])

    # -- kv -------------------------------------------------------------------------------------------------------
    def get(self, k: str, default: str | None = None) -> str | None:
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def set(self, k: str, v: str) -> None:
        self.db.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def close(self) -> None:
        self.db.close()
