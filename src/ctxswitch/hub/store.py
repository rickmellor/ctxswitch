"""Hub store: the single source of truth every client reads. SQLite in WAL mode.

Ingest is idempotent: sessions upsert by key, turns upsert by (key, idx) keeping the version with the larger
offset_end, asks upsert by (key, ref). A monotonically increasing ``change`` counter lets clients poll or be
notified cheaply.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ctxswitch.common.schema import LIVE

_LIVE_SQL = ",".join(f"'{s}'" for s in LIVE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (host_id TEXT PRIMARY KEY, last_seen REAL NOT NULL, info TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    key TEXT PRIMARY KEY, host_id TEXT NOT NULL, harness TEXT NOT NULL, session_id TEXT NOT NULL,
    meta TEXT NOT NULL, title TEXT, cwd TEXT, repo_root TEXT, repo_remote TEXT, git_branch TEXT,
    started_at TEXT, last_activity TEXT, ended INTEGER NOT NULL DEFAULT 0,
    live_status TEXT NOT NULL DEFAULT 'unknown', status_since TEXT, pid INTEGER, socket TEXT,
    last_viewed_at TEXT, updated REAL NOT NULL, recap TEXT, recap_anchor TEXT, recap_at REAL, live_name TEXT, reentry TEXT
);
CREATE INDEX IF NOT EXISTS sessions_host ON sessions(host_id);
CREATE TABLE IF NOT EXISTS turns (
    key TEXT NOT NULL, idx INTEGER NOT NULL, data TEXT NOT NULL, complete INTEGER NOT NULL,
    offset_end INTEGER NOT NULL, started_at TEXT, ended_at TEXT, PRIMARY KEY(key, idx)
);
CREATE TABLE IF NOT EXISTS asks (
    key TEXT NOT NULL, ref TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, turn_idx INTEGER NOT NULL,
    asked_at TEXT, answered INTEGER NOT NULL DEFAULT 0, answer TEXT, answered_at TEXT, PRIMARY KEY(key, ref)
);
CREATE INDEX IF NOT EXISTS asks_open ON asks(answered, key);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, quote TEXT NOT NULL,
    turn_idx INTEGER NOT NULL, extra TEXT, active INTEGER NOT NULL DEFAULT 1, retired_turn INTEGER, retired_by TEXT,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_key ON ledger(key, kind, active);
CREATE TABLE IF NOT EXISTS tracker (
    key TEXT PRIMARY KEY, through_idx INTEGER NOT NULL DEFAULT -1, purpose TEXT, purpose_turn INTEGER,
    purpose_history TEXT NOT NULL DEFAULT '[]', updated REAL
);
"""

LLM_ASK = "llm:"                                    # asks.ref prefix for plain-text asks the tracker found
ANSWER_CHARS = 300


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(sessions)")}
        for col, typ in (("recap", "TEXT"), ("recap_anchor", "TEXT"), ("recap_at", "REAL"), ("live_name", "TEXT"), ("reentry", "TEXT")):   # older databases
            if col not in cols:
                self.db.execute(f"ALTER TABLE sessions ADD COLUMN {col} {typ}")
        self._lock = threading.Lock()
        self.change = int(self._get("change", "0"))

    def _get(self, k: str, default: str) -> str:
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def _bump(self) -> None:
        self.change += 1
        self.db.execute("INSERT INTO kv(k,v) VALUES('change',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(self.change),))

    # -- ingest ---------------------------------------------------------------------------------------------------
    def host_seen(self, host_id: str, info: dict[str, Any] | None = None) -> None:
        with self._lock:
            self.db.execute("INSERT INTO hosts(host_id,last_seen,info) VALUES(?,?,?) ON CONFLICT(host_id) DO UPDATE SET last_seen=excluded.last_seen, info=COALESCE(excluded.info, hosts.info)",
                            (host_id, time.time(), json.dumps(info) if info else None))

    def upsert_session(self, m: dict[str, Any]) -> None:
        with self._lock:
            if m.get("reset"):
                self.db.execute("DELETE FROM turns WHERE key=?", (m["key"],))
                self.db.execute("DELETE FROM asks WHERE key=?", (m["key"],))
                self.db.execute("DELETE FROM ledger WHERE key=?", (m["key"],))
                self.db.execute("DELETE FROM tracker WHERE key=?", (m["key"],))
            self.db.execute(
                "INSERT INTO sessions(key,host_id,harness,session_id,meta,title,cwd,repo_root,repo_remote,git_branch,started_at,last_activity,ended,updated) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET meta=excluded.meta, title=excluded.title, cwd=excluded.cwd, "
                "repo_root=excluded.repo_root, repo_remote=excluded.repo_remote, git_branch=excluded.git_branch, started_at=excluded.started_at, "
                "last_activity=excluded.last_activity, ended=excluded.ended, updated=excluded.updated",
                (m["key"], m["host_id"], m["harness"], m["session_id"], json.dumps(m), m.get("title", ""), m.get("cwd", ""),
                 m.get("repo_root", ""), m.get("repo_remote", ""), m.get("git_branch", ""), m.get("started_at", ""),
                 m.get("last_activity", ""), int(bool(m.get("ended"))), time.time()))
            if m.get("ended"):
                self.db.execute("UPDATE sessions SET live_status='ended' WHERE key=? AND live_status!='ended'", (m["key"],))
            self._bump()

    def upsert_turns(self, key: str, turns: list[dict[str, Any]], open_turn: dict[str, Any] | None, ended: bool = False) -> None:
        with self._lock:
            for t in [*turns, *([open_turn] if open_turn else [])]:
                self.db.execute(
                    "INSERT INTO turns(key,idx,data,complete,offset_end,started_at,ended_at) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(key,idx) DO UPDATE SET data=excluded.data, complete=excluded.complete, offset_end=excluded.offset_end, "
                    "started_at=excluded.started_at, ended_at=excluded.ended_at WHERE excluded.offset_end>=turns.offset_end",
                    (key, t["idx"], json.dumps(t), int(t.get("complete", False)), t.get("offset_end", 0), t.get("started_at"), t.get("ended_at")))
                for a in t.get("asks", []):
                    self.db.execute(
                        "INSERT INTO asks(key,ref,kind,text,turn_idx,asked_at,answered,answer,answered_at) VALUES(?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(key,ref) DO UPDATE SET text=excluded.text, answered=MAX(asks.answered, excluded.answered), "
                        "answer=CASE WHEN excluded.answered=1 THEN excluded.answer ELSE asks.answer END, "
                        "answered_at=CASE WHEN excluded.answered=1 THEN excluded.answered_at ELSE asks.answered_at END",
                        (key, a["ref"], a["kind"], a["text"], t["idx"], t.get("ended_at") or t.get("started_at"), int(a["answered"]), a.get("answer"), a.get("answered_at")))
            for t in [*turns, *([open_turn] if open_turn else [])]:
                if typed(t):                            # your next prompt answers a plain-text ask
                    self._close_llm_asks(key, t)
            if turns or open_turn:
                last = (open_turn or turns[-1])
                self.db.execute("UPDATE sessions SET last_activity=MAX(COALESCE(last_activity,''), ?), updated=? WHERE key=?",
                                (last.get("ended_at") or last.get("started_at") or "", time.time(), key))
            if ended:
                self.db.execute("UPDATE sessions SET ended=1, live_status='ended', updated=? WHERE key=?", (time.time(), key))
            self._bump()

    def set_liveness(self, host_id: str, items: list[dict[str, Any]]) -> None:
        """Full snapshot of live sessions for a host; anything else on that host is not live."""
        with self._lock:
            now = time.time()
            live_keys = {i["key"] for i in items}
            for i in items:
                self.db.execute("UPDATE sessions SET live_status=?, status_since=?, pid=?, socket=?, live_name=?, ended=0, updated=? WHERE key=?",
                                (i["status"], i.get("status_since"), i.get("pid"), i.get("socket"), i.get("name"), now, i["key"]))
                if self.db.execute("SELECT changes()").fetchone()[0] == 0:
                    # live but transcript not indexed yet: placeholder row so the TUI shows it
                    h, harness, sid = i["key"].split("/", 2)
                    self.db.execute("INSERT OR IGNORE INTO sessions(key,host_id,harness,session_id,meta,live_status,status_since,pid,socket,live_name,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                    (i["key"], h, harness, sid, "{}", i["status"], i.get("status_since"), i.get("pid"), i.get("socket"), i.get("name"), now))
            # Claude shows a dialog only while "waiting": idle means any hook ask from before then was dismissed
            # (Esc on a permission prompt fires no hook at all).
            for i in items:
                if i["status"] == "idle" and i.get("status_since") and i["key"].split("/", 2)[1] == "claude":
                    self.db.execute("UPDATE asks SET answered=1, answer='closed: dismissed', answered_at=? WHERE key=? AND answered=0 "
                                    "AND ref LIKE 'hook:%' AND julianday(asked_at) < julianday(?)", (i["status_since"], i["key"], i["status_since"]))
            # Both registries are authoritative on their host: a session that was live and is not in the snapshot
            # has exited. Claude rows in any other state end too (the registry covers every Claude session); a pi
            # row that never registered (extension not installed) only ages out via its transcript.
            rows = self.db.execute("SELECT key, harness, live_status FROM sessions WHERE host_id=? AND live_status!='ended'", (host_id,)).fetchall()
            for r in rows:
                if r["key"] in live_keys:
                    continue
                if r["harness"] == "claude" or r["live_status"] in LIVE:
                    self.db.execute("UPDATE sessions SET live_status='ended', ended=1, pid=NULL, socket=NULL, live_name=NULL, updated=? WHERE key=?", (now, r["key"]))
            self._bump()

    def _close_llm_asks(self, key: str, t: dict[str, Any]) -> None:
        self.db.execute(f"UPDATE asks SET answered=1, answer=?, answered_at=? WHERE key=? AND answered=0 AND ref LIKE '{LLM_ASK}%' AND turn_idx<?",
                        (_clip(t.get("user_text"), ANSWER_CHARS), t.get("started_at"), key, t["idx"]))

    # -- tracker (hub/tracker/extract.py) ---------------------------------------------------------------------------
    def tracker_due(self, cutoff: str) -> list[str]:
        """Sessions with a completed exchange the tracker hasn't read yet, live first then most recent. Sessions
        neither live nor active since ``cutoff`` are left alone (no LLM over history)."""
        q = (f"SELECT s.key FROM sessions s LEFT JOIN tracker k ON k.key=s.key WHERE "
             f"(s.live_status IN ({_LIVE_SQL}) OR s.last_activity>=?) AND EXISTS (SELECT 1 FROM turns t WHERE t.key=s.key "
             f"AND t.complete=1 AND t.idx>COALESCE(k.through_idx,-1)) "
             f"ORDER BY CASE WHEN s.live_status IN ({_LIVE_SQL}) THEN 0 ELSE 1 END, s.last_activity DESC")
        return [r[0] for r in self.db.execute(q, (cutoff,))]

    def tracker_state(self, key: str) -> dict[str, Any]:
        r = self.db.execute("SELECT * FROM tracker WHERE key=?", (key,)).fetchone()
        if r is None:
            return {"key": key, "through_idx": -1, "purpose": None, "purpose_turn": None, "purpose_history": []}
        d = dict(r); d["purpose_history"] = json.loads(d["purpose_history"] or "[]")
        return d

    def next_untracked(self, key: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """(the oldest completed turn the tracker hasn't read, the turn before it) for one session."""
        through = self.tracker_state(key)["through_idx"]
        r = self.db.execute("SELECT data FROM turns WHERE key=? AND complete=1 AND idx>? ORDER BY idx LIMIT 1", (key, through)).fetchone()
        if r is None:
            return None, None
        t = json.loads(r[0])
        p = self.db.execute("SELECT data FROM turns WHERE key=? AND idx<? ORDER BY idx DESC LIMIT 1", (key, t["idx"])).fetchone()
        return t, (json.loads(p[0]) if p else None)

    def ledger(self, key: str, active_only: bool = False) -> list[dict[str, Any]]:
        q = "SELECT * FROM ledger WHERE key=?" + (" AND active=1" if active_only else "") + " ORDER BY turn_idx, id"
        out = []
        for r in self.db.execute(q, (key,)):
            d = dict(r); d["extra"] = json.loads(d["extra"] or "{}"); d["active"] = bool(d["active"])
            out.append(d)
        return out

    def apply_extraction(self, key: str, turn: dict[str, Any], rows: list[dict[str, Any]], retire: list[int],
                         asks: list[dict[str, Any]], purpose: str | None) -> bool:
        """Record what the tracker read from one exchange and move past it. Idempotent: a turn already read is a no-op."""
        idx = turn["idx"]
        with self._lock:
            st = self.tracker_state(key)
            if idx <= st["through_idx"]:
                return False
            now = time.time()
            for r in rows:
                self.db.execute("INSERT INTO ledger(key,kind,text,quote,turn_idx,extra,created) VALUES(?,?,?,?,?,?,?)",
                                (key, r["kind"], r["text"], r["quote"], idx, json.dumps(r.get("extra") or {}), now))
            for rid in retire:
                self.db.execute("UPDATE ledger SET active=0, retired_turn=?, retired_by='llm' WHERE key=? AND id=? AND active=1", (idx, key, rid))
            for n, a in enumerate(asks):
                self.db.execute("INSERT OR IGNORE INTO asks(key,ref,kind,text,turn_idx,asked_at) VALUES(?,?,?,?,?,?)",
                                (key, f"{LLM_ASK}{idx}:{n}", "text", a["text"], idx, turn.get("ended_at") or turn.get("started_at")))
            # a prompt that already answered it (catch-up over older turns)
            nxt = self.db.execute("SELECT data FROM turns WHERE key=? AND idx>? ORDER BY idx", (key, idx)).fetchall()
            for (data,) in nxt:
                t = json.loads(data)
                if typed(t):
                    self._close_llm_asks(key, t); break
            hist = st["purpose_history"]
            p, pt = st["purpose"], st["purpose_turn"]
            if purpose and purpose != p:
                if p:
                    hist.append({"purpose": p, "turn": pt})
                p, pt = purpose, idx
            self.db.execute("INSERT INTO tracker(key,through_idx,purpose,purpose_turn,purpose_history,updated) VALUES(?,?,?,?,?,?) "
                            "ON CONFLICT(key) DO UPDATE SET through_idx=excluded.through_idx, purpose=excluded.purpose, "
                            "purpose_turn=excluded.purpose_turn, purpose_history=excluded.purpose_history, updated=excluded.updated",
                            (key, idx, p, pt, json.dumps(hist), now))
            self._bump()
            return True

    def retire_row(self, key: str, row_id: int, by: str = "operator") -> bool:
        with self._lock:
            self.db.execute("UPDATE ledger SET active=0, retired_by=? WHERE key=? AND id=? AND active=1", (by, key, row_id))
            ok = self.db.execute("SELECT changes()").fetchone()[0] > 0
            if ok:
                self._bump()
            return ok

    def mark_viewed(self, key: str, when: str) -> None:
        with self._lock:
            self.db.execute("UPDATE sessions SET last_viewed_at=? WHERE key=?", (when, key)); self._bump()

    def set_recap(self, key: str, text: str, anchor: str, reentry: str | None = None) -> None:
        with self._lock:
            self.db.execute("UPDATE sessions SET recap=?, recap_anchor=?, recap_at=?, reentry=COALESCE(?, reentry) WHERE key=?",
                            (text, anchor, time.time(), reentry, key))
            self._bump()

    # -- queries --------------------------------------------------------------------------------------------------
    def hosts(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT host_id,last_seen FROM hosts ORDER BY host_id")]

    def sessions(self, host: str | None = None, include_ended: bool = True, limit: int = 500) -> list[dict[str, Any]]:
        q = "SELECT * FROM sessions"
        cond, args = [], []
        if host:
            cond.append("host_id=?"); args.append(host)
        if not include_ended:
            cond.append(f"live_status IN ({_LIVE_SQL})")
        # sessions that never had an exchange (e.g. opened and /exit'd) are noise unless they are live right now
        cond.append(f"(live_status IN ({_LIVE_SQL}) OR EXISTS (SELECT 1 FROM turns t WHERE t.key=sessions.key))")
        q += " WHERE " + " AND ".join(cond)
        q += " ORDER BY CASE live_status WHEN 'busy' THEN 0 WHEN 'waiting' THEN 0 WHEN 'shell' THEN 1 WHEN 'idle' THEN 1 WHEN 'unknown' THEN 2 ELSE 3 END, last_activity DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in self.db.execute(q, args):
            d = self._session_row(r)
            out.append(d)
        return out

    def _session_row(self, r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r); d["meta"] = json.loads(d["meta"] or "{}"); d["ended"] = bool(d["ended"])
        if d.get("live_name"):                          # the running session's own name beats the transcript's
            d["title"] = d["live_name"]
        last = self.db.execute("SELECT data FROM turns WHERE key=? ORDER BY idx DESC LIMIT 1", (d["key"],)).fetchone()
        d["last_turn"] = json.loads(last[0]) if last else None
        d["turn_count"] = self.db.execute("SELECT COUNT(*) FROM turns WHERE key=?", (d["key"],)).fetchone()[0]
        d["open_asks"] = [dict(a) for a in self.db.execute("SELECT * FROM asks WHERE key=? AND answered=0 ORDER BY asked_at", (d["key"],))]
        return d

    def session(self, key: str, turns: int = 50) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM sessions WHERE key=?", (key,)).fetchone()
        if r is None:
            return None
        d = self._session_row(r)
        d["turns"] = [json.loads(t[0]) for t in self.db.execute("SELECT data FROM turns WHERE key=? ORDER BY idx DESC LIMIT ?", (key, turns))][::-1]
        d["asks"] = [dict(a) for a in self.db.execute("SELECT * FROM asks WHERE key=? ORDER BY asked_at", (key,))]
        d["ledger"] = self.ledger(key)
        st = self.tracker_state(key)
        d["purpose"], d["purpose_history"], d["tracked_through"] = st["purpose"], st["purpose_history"], st["through_idx"]
        return d

    def attention(self, host: str | None = None) -> list[dict[str, Any]]:
        """Open asks on sessions that are not ended, plus idle live sessions, oldest wait first."""
        q = ("SELECT a.*, s.host_id, s.harness, s.title, s.live_status, s.status_since FROM asks a JOIN sessions s ON s.key=a.key "
             "WHERE a.answered=0 AND s.live_status!='ended' AND s.ended=0")
        args: list[Any] = []
        if host:
            q += " AND s.host_id=?"; args.append(host)
        q += " ORDER BY a.asked_at"
        items = [dict(r) | {"item": "ask"} for r in self.db.execute(q, args)]
        # waiting = Claude's registry says a dialog is up; shown even where no hook ask explains it (hooks not installed)
        q2 = "SELECT key,host_id,harness,title,live_status,status_since FROM sessions WHERE live_status IN ('idle','waiting')"
        if host:
            q2 += " AND host_id=?"
        asked = {i["key"] for i in items}
        for r in self.db.execute(q2, args):
            if r["key"] not in asked:
                items.append(dict(r) | {"item": r["live_status"], "asked_at": r["status_since"]})
        return items

    def close(self) -> None:
        self.db.close()


def typed(turn: dict[str, Any]) -> bool:
    """A prompt you typed: not a task notification or peer message, and not a command wrapper (<bash-input> etc)."""
    text = (turn.get("user_text") or "").strip()
    return turn.get("user_origin") == "human" and bool(text) and not text.startswith("<")


def _clip(text: str | None, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"
