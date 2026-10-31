"""Hub store: the single source of truth every client reads. SQLite in WAL mode.

Ingest is idempotent: sessions upsert by key, turns upsert by (key, idx) keeping the version with the larger
offset_end, asks upsert by (key, ref). A monotonically increasing ``change`` counter lets clients poll or be
notified cheaply.

Messages (operator -> session) go queued -> sent (the collector wrote it to the session's socket) -> delivered (a
turn whose prompt contains the body arrived) -> replied (that turn completed), or failed / cancelled. A delivered
message's turn is recorded as yours (origin human, prompt = what you wrote), whatever the harness wrapped it in, and
so is the answer it gave to an open plain-text ask. A quick-reply is a message naming the ask it answers (``ask_ref``,
checked when sent, shown with it); delivering it answers that ask the way any prompt does.

An agent's message (``from_key`` = the sending session) is the same, except its turn is recorded as a peer's (origin
peer, ``from_key`` on the turn), never yours, and an ask it happens to answer says who answered. Collectors only get
agent messages when they say they know the header to deliver them with (outbox ``agents``), so an older one can't
deliver one as yours. One in flight per sender and recipient, and AGENT_PER_HOUR per sender, so two agents can't
keep each other busy.

Workstreams: sessions on the same repo (by remote, so clones on other hosts match) and branch; see workstream_of.
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
    last_viewed_at TEXT, updated REAL NOT NULL, recap TEXT, recap_anchor TEXT, recap_at REAL, live_name TEXT, reentry TEXT,
    parent_key TEXT, job TEXT, job_status TEXT, job_brief TEXT
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
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL,
    from_host TEXT, created REAL NOT NULL, sent_at REAL, delivered_at REAL, turn_idx INTEGER, reply TEXT, error TEXT,
    ask_ref TEXT, from_key TEXT, workstream TEXT
);
CREATE INDEX IF NOT EXISTS messages_key ON messages(key, status);
CREATE TABLE IF NOT EXISTS links (
    to_key TEXT PRIMARY KEY, from_key TEXT NOT NULL, at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tracker (
    key TEXT PRIMARY KEY, through_idx INTEGER NOT NULL DEFAULT -1, purpose TEXT, purpose_turn INTEGER,
    purpose_history TEXT NOT NULL DEFAULT '[]', updated REAL
);
"""

LLM_ASK = "llm:"                                    # asks.ref prefix for plain-text asks the tracker found
ANSWER_CHARS = 300
REPLY_CHARS = 2000
MATCH_CHARS = 200                                   # a turn is the message's when its prompt contains this much of the body
MSG_OPEN = ("queued", "sent")
AGENT_PER_HOUR = 20                                 # messages one agent session may send in an hour
NOT_DELIVERABLE = ("claude-win", "agy", "shell")  # Windows-native Claude: no socket reachable from WSL; agy: no input
                                                  # channel; shell: a bash, nobody is listening for a prompt
REPLYABLE = ("plain", "text")                       # asks a message can answer; the rest are dialogs in the session
SIBLINGS = 6                                        # other sessions shown for a workstream
SIBLING_DAYS = 14                                   # ... live, or active within this many days
SEARCH_KINDS = ("decision", "instruction", "claim", "prompt")

TOKEN_TYPES = ("input", "output", "cache_read", "cache_creation")        # every usage dict has all four ints


def _usage_cols(alias: str = "t") -> str:
    """The aggregates one GROUP BY over turns needs: the four token types summed (``u_<type>``), the same totals over
    the turns the hub must price itself (``p_<type>`` — pi prices a turn with cost_usd already), pi's own total, the
    newest turn's index and a turn count. json_extract of a missing key is NULL, so COALESCE it away; a shell or agy
    turn contributes nothing."""
    out = []
    for t in TOKEN_TYPES:
        j = f"COALESCE(json_extract({alias}.data,'$.usage.{t}'),0)"
        out.append(f"COALESCE(SUM({j}),0) AS u_{t}")
        out.append(f"COALESCE(SUM(CASE WHEN COALESCE(json_extract({alias}.data,'$.cost_usd'),0)>0 THEN 0 ELSE {j} END),0) AS p_{t}")
    out += [f"COALESCE(SUM(COALESCE(json_extract({alias}.data,'$.cost_usd'),0)),0) AS reported",
            f"MAX({alias}.idx) AS last_idx", "COUNT(*) AS turn_usage"]
    return ", ".join(out)


def _usage_sql(where: str = "", alias: str = "t") -> str:
    """One GROUP BY over turns by session *and model*: a session that ran on two models (the cloud, then SAINT) prices
    each part of it right. The rows every spend answer is built from."""
    return (f"SELECT {alias}.key AS key, COALESCE(json_extract({alias}.data,'$.model'),'') AS model, {_usage_cols(alias)} "
            f"FROM turns {alias}{' WHERE ' + where if where else ''} GROUP BY {alias}.key, model")


def _usage_row(r: sqlite3.Row) -> dict[str, Any]:
    """One grouped row as data: what one session turned over on one model."""
    return {"key": r["key"], "model": r["model"] or "", "turns": r["turn_usage"], "reported": r["reported"],
            "last_idx": r["last_idx"], "tokens": {t: r[f"u_{t}"] for t in TOKEN_TYPES},
            "priced": {t: r[f"p_{t}"] for t in TOKEN_TYPES}}


def _blank_usage() -> dict[str, Any]:
    return {"tokens": {t: 0 for t in TOKEN_TYPES}, "priced": {t: 0 for t in TOKEN_TYPES},
            "reported": 0.0, "model": "", "model_idx": -1, "turns": 0}


def _accumulate(d: dict[str, Any], r: dict[str, Any]) -> None:
    """Fold one grouped usage row into a session's totals."""
    for t in TOKEN_TYPES:
        d["tokens"][t] += r["tokens"][t]
        d["priced"][t] += r["priced"][t]
    d["reported"] += r["reported"]
    d["turns"] += r["turns"]
    if r["last_idx"] > d["model_idx"]:                  # the newest turn's model names the session
        d["model_idx"], d["model"] = r["last_idx"], r["model"]



class Store:
    def __init__(self, path: Path | str, prices: dict[str, dict[str, float]] | None = None):
        """``prices``: [hub.prices] rates per model, overriding the built-in table (see hub/spend.py)."""
        self.prices = prices or {}
        self.path = str(path)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(sessions)")}
        for col, typ in (("recap", "TEXT"), ("recap_anchor", "TEXT"), ("recap_at", "REAL"), ("live_name", "TEXT"), ("reentry", "TEXT"),
                         ("parent_key", "TEXT"), ("job", "TEXT"), ("job_status", "TEXT"), ("job_brief", "TEXT")):   # older databases
            if col not in cols:
                self.db.execute(f"ALTER TABLE sessions ADD COLUMN {col} {typ}")
        mcols = {r[1] for r in self.db.execute("PRAGMA table_info(messages)")}
        for col in ("ask_ref", "from_key", "workstream"):
            if col not in mcols:
                self.db.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
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
                "INSERT INTO sessions(key,host_id,harness,session_id,meta,title,cwd,repo_root,repo_remote,git_branch,started_at,last_activity,ended,updated,"
                "parent_key,job,job_status,job_brief) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET meta=excluded.meta, title=excluded.title, cwd=excluded.cwd, "
                "repo_root=excluded.repo_root, repo_remote=excluded.repo_remote, git_branch=excluded.git_branch, started_at=excluded.started_at, "
                "last_activity=excluded.last_activity, ended=excluded.ended, updated=excluded.updated, "
                "parent_key=excluded.parent_key, job=excluded.job, job_status=excluded.job_status, job_brief=excluded.job_brief",
                (m["key"], m["host_id"], m["harness"], m["session_id"], json.dumps(m), m.get("title", ""), m.get("cwd", ""),
                 m.get("repo_root", ""), m.get("repo_remote", ""), m.get("git_branch", ""), m.get("started_at", ""),
                 m.get("last_activity", ""), int(bool(m.get("ended"))), time.time(),
                 m.get("parent_key") or "", m.get("job") or "", m.get("job_status") or "", m.get("job_brief") or ""))
            if m.get("ended"):
                self.db.execute("UPDATE sessions SET live_status='ended' WHERE key=? AND live_status!='ended'", (m["key"],))
            self._bump()

    def upsert_turns(self, key: str, turns: list[dict[str, Any]], open_turn: dict[str, Any] | None, ended: bool = False) -> None:
        from ctxswitch.hub.handoff import marker_in
        for t in [*turns, *([open_turn] if open_turn else [])]:
            frm = marker_in(t.get("user_text"))
            if frm:
                self.link_handoff(key, frm)
        with self._lock:
            for t in [*turns, *([open_turn] if open_turn else [])]:
                self._match_message(key, t)
                self.db.execute(
                    "INSERT INTO turns(key,idx,data,complete,offset_end,started_at,ended_at) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(key,idx) DO UPDATE SET data=excluded.data, complete=excluded.complete, offset_end=excluded.offset_end, "
                    "started_at=excluded.started_at, ended_at=excluded.ended_at WHERE excluded.offset_end>=turns.offset_end",
                    (key, t["idx"], json.dumps(t), int(t.get("complete", False)), t.get("offset_end", 0), t.get("started_at"), t.get("ended_at")))
                for a in t.get("asks", []):
                    self.db.execute(
                        "INSERT INTO asks(key,ref,kind,text,turn_idx,asked_at,answered,answer,answered_at) VALUES(?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(key,ref) DO UPDATE SET text=excluded.text, answered=MAX(asks.answered, excluded.answered), "
                        "answer=CASE WHEN asks.answered=1 AND excluded.answered_at IS asks.answered_at THEN asks.answer "
                        "WHEN excluded.answered=1 THEN excluded.answer ELSE asks.answer END, "
                        "answered_at=CASE WHEN excluded.answered=1 THEN excluded.answered_at ELSE asks.answered_at END",
                        (key, a["ref"], a["kind"], a["text"], t["idx"], t.get("ended_at") or t.get("started_at"), int(a["answered"]), a.get("answer"), a.get("answered_at")))
            for t in [*turns, *([open_turn] if open_turn else [])]:
                if t.get("via") == "ctx":                # the harness's wrapped text answered it: say what you (or it) wrote
                    who = f"from {t['from_key']}: " if t.get("from_key") else ""
                    self.db.execute("UPDATE asks SET answer=? WHERE key=? AND kind='plain' AND answered=1 AND answered_at=?",
                                    (_clip(who + t["user_text"], ANSWER_CHARS), key, t.get("started_at")))
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

    # -- messages ---------------------------------------------------------------------------------------------------
    def add_message(self, key: str, body: str, from_host: str | None = None, ask_ref: str | None = None,
                    from_key: str | None = None) -> int:
        with self._lock:
            return self._insert_message(key, body, from_host, ask_ref, from_key)

    def add_workstream_message(self, workstream: str, body: str, from_host: str | None = None) -> int:
        """A message for whichever session works on ``workstream`` (any host): the first one live when a collector
        asks (outbox), else the next to run. Unbound until then (key '')."""
        with self._lock:
            return self._insert_message("", body, from_host, None, None, workstream)

    def has_workstream(self, workstream: str) -> bool:
        return any(workstream_of(dict(r)) == workstream for r in self.db.execute(
            "SELECT host_id, repo_remote, repo_root, git_branch FROM sessions WHERE repo_remote IS NOT NULL OR repo_root IS NOT NULL"))

    def workstream_messages(self, workstream: str | None) -> list[dict[str, Any]]:
        """Messages for this work no session has taken yet."""
        if not workstream:
            return []
        return [dict(r) for r in self.db.execute("SELECT * FROM messages WHERE key='' AND workstream=? AND status='queued' ORDER BY id", (workstream,))]

    def _insert_message(self, key: str, body: str, from_host: str | None, ask_ref: str | None, from_key: str | None,
                        workstream: str | None = None) -> int:
        cur = self.db.execute("INSERT INTO messages(key,body,status,from_host,created,ask_ref,from_key,workstream) VALUES(?,?,?,?,?,?,?,?)",
                              (key, body, "queued", from_host, time.time(), ask_ref, from_key, workstream))
        self._bump()
        return int(cur.lastrowid)

    def messages(self, key: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM messages WHERE key=? ORDER BY id DESC LIMIT ?", (key, limit)).fetchall()
        return [dict(r) for r in rows][::-1]

    def outbox(self, host_id: str, agents: bool = False) -> list[dict[str, Any]]:
        """Queued messages for sessions live on ``host_id`` right now, oldest first. The collector checks each
        against its own registry before writing anything: the hub only says what is waiting. Agents' messages (with
        the sender's key and title) only to a collector that asks for them. A workstream message is bound here to a
        session live on this host on that work, the first such host to ask taking it."""
        self._bind_workstream_messages(host_id)
        # a harness that can't take one is filtered here too: api refuses them, but a row queued before that must not go out
        q = (f"SELECT m.id, m.key, m.body, s.harness, m.from_key, COALESCE(NULLIF(f.live_name,''), f.title) AS from_title "
             f"FROM messages m JOIN sessions s ON s.key=m.key LEFT JOIN sessions f ON f.key=m.from_key WHERE m.status='queued' "
             f"AND s.host_id=? AND s.live_status IN ({_LIVE_SQL}) "
             f"AND s.harness NOT IN ({','.join(repr(h) for h in NOT_DELIVERABLE)})"
             f"{'' if agents else ' AND m.from_key IS NULL'} ORDER BY m.id")
        return [dict(r) for r in self.db.execute(q, (host_id,))]

    def _bind_workstream_messages(self, host_id: str) -> None:
        if not self.db.execute("SELECT 1 FROM messages WHERE key='' AND status='queued' LIMIT 1").fetchone():
            return
        rank = {"idle": 0, "busy": 1}                       # an idle session starts on it now; a dialog last
        live = [dict(r) for r in self.db.execute(
            f"SELECT key, host_id, harness, repo_remote, repo_root, git_branch, live_status, last_activity FROM sessions "
            f"WHERE host_id=? AND live_status IN ({_LIVE_SQL}) AND harness NOT IN ({','.join(repr(h) for h in NOT_DELIVERABLE)})", (host_id,))]
        live.sort(key=lambda s: s["last_activity"] or "", reverse=True)
        live.sort(key=lambda s: rank.get(s["live_status"], 2))
        with self._lock:
            for m in self.db.execute("SELECT id, workstream FROM messages WHERE key='' AND status='queued' ORDER BY id").fetchall():
                s = next((s for s in live if workstream_of(s) == m["workstream"]), None)
                if s is not None:
                    self.db.execute("UPDATE messages SET key=? WHERE id=? AND key=''", (s["key"], m["id"]))
                    self._bump()

    def agent_send_refused(self, from_key: str, key: str) -> tuple[int, str] | None:
        """(HTTP status, why) when this agent may not send to ``key`` now, else None."""
        if self.db.execute(f"SELECT 1 FROM messages WHERE from_key=? AND key=? AND status IN ({','.join(repr(x) for x in MSG_OPEN)})",
                           (from_key, key)).fetchone():
            return 409, "your last message to that session hasn't been delivered yet"
        n = self.db.execute("SELECT COUNT(*) FROM messages WHERE from_key=? AND created>? AND status!='cancelled'",
                            (from_key, time.time() - 3600)).fetchone()[0]
        if n >= AGENT_PER_HOUR:
            return 429, f"this session has sent {n} messages in the last hour (the limit is {AGENT_PER_HOUR})"
        return None

    def set_message_status(self, msg_id: int, host_id: str, status: str, error: str | None = None) -> bool:
        """Collector report: ``sent`` or ``failed``, only for a queued message to a session on that host."""
        if status not in ("sent", "failed"):
            return False
        with self._lock:
            self.db.execute("UPDATE messages SET status=?, sent_at=?, error=? WHERE id=? AND status='queued' "
                            "AND key IN (SELECT key FROM sessions WHERE host_id=?)", (status, time.time(), error, msg_id, host_id))
            ok = self.db.execute("SELECT changes()").fetchone()[0] > 0
            if ok:
                self._bump()
            return ok

    def cancel_message(self, msg_id: int) -> bool:
        with self._lock:
            self.db.execute("UPDATE messages SET status='cancelled' WHERE id=? AND status='queued'", (msg_id,))
            ok = self.db.execute("SELECT changes()").fetchone()[0] > 0
            if ok:
                self._bump()
            return ok

    def _match_message(self, key: str, t: dict[str, Any]) -> None:
        """Mark the message this turn delivers (and its reply, once complete); the turn becomes your prompt, or the
        sending agent's."""
        m = self.db.execute("SELECT id, body, status, from_key FROM messages WHERE key=? AND turn_idx=?", (key, t["idx"])).fetchone()
        if m is None:
            text = _norm(t.get("user_text"))
            for r in self.db.execute("SELECT id, body, status, from_key FROM messages WHERE key=? AND status='sent' ORDER BY id", (key,)):
                if _norm(r["body"])[:MATCH_CHARS] in text:
                    m = r; break
            if m is None:
                return
            self.db.execute("UPDATE messages SET status='delivered', delivered_at=?, turn_idx=? WHERE id=?", (time.time(), t["idx"], m["id"]))
        t.update(user_text=m["body"], user_origin="peer" if m["from_key"] else "human", via="ctx", message_id=m["id"])
        if m["from_key"]:
            t["from_key"] = m["from_key"]
        if t.get("complete") and m["status"] != "replied":
            self.db.execute("UPDATE messages SET status='replied', reply=? WHERE id=?", (_clip(t.get("assistant_text"), REPLY_CHARS), m["id"]))

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

    # -- handoffs -------------------------------------------------------------------------------------------------
    def note_handoff(self, key: str, to: str, profile: str | None = None) -> None:
        """The operator started a handoff of ``key`` to agent ``to``; shown until the new session links itself."""
        with self._lock:
            self.db.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                            (f"handoff:{key}", json.dumps({"to": to, "profile": profile, "at": time.time()})))
            self.db.commit(); self._bump()

    def link_handoff(self, to_key: str, from_key: str) -> bool:
        """The new session's first prompt named the session it continues: link them once, and carry the active
        standing instructions and the purpose over to the new session's ledger (marked inherited)."""
        with self._lock:
            if from_key == to_key or self.db.execute("SELECT 1 FROM links WHERE to_key=?", (to_key,)).fetchone():
                return False
            if not self.db.execute("SELECT 1 FROM sessions WHERE key=?", (from_key,)).fetchone():
                return False
            now = time.time()
            self.db.execute("INSERT INTO links(to_key,from_key,at) VALUES(?,?,?)", (to_key, from_key, now))
            for r in self.db.execute("SELECT kind,text,quote,extra FROM ledger WHERE key=? AND kind='instruction' AND active=1 ORDER BY id", (from_key,)).fetchall():
                extra = json.loads(r[3] or "{}") | {"inherited_from": from_key}
                self.db.execute("INSERT INTO ledger(key,kind,text,quote,turn_idx,extra,created) VALUES(?,?,?,?,?,?,?)",
                                (to_key, r[0], r[1], r[2], -1, json.dumps(extra), now))
            src = self.tracker_state(from_key)
            if src.get("purpose") and not self.tracker_state(to_key).get("purpose"):
                self.db.execute("INSERT INTO tracker(key,through_idx,purpose,purpose_turn,purpose_history,updated) VALUES(?,-1,?,-1,'[]',?) "
                                "ON CONFLICT(key) DO UPDATE SET purpose=excluded.purpose, purpose_turn=-1, updated=excluded.updated",
                                (to_key, src["purpose"], now))
            self.db.execute("DELETE FROM kv WHERE k=?", (f"handoff:{from_key}",))
            self.db.commit(); self._bump()
            return True

    def handoff_links(self, key: str) -> dict[str, Any]:
        """continues: the key this session took over from; continued_by: sessions that took over from it; handed_off:
        a handoff started but not yet linked ({to, profile, at})."""
        cont = self.db.execute("SELECT from_key FROM links WHERE to_key=?", (key,)).fetchone()
        by = [r[0] for r in self.db.execute("SELECT to_key FROM links WHERE from_key=? ORDER BY at", (key,))]
        pend = self.db.execute("SELECT v FROM kv WHERE k=?", (f"handoff:{key}",)).fetchone()
        return {"continues": cont[0] if cont else None, "continued_by": by, "handed_off": json.loads(pend[0]) if pend else None}

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
            if any(r["kind"] == "decision" for r in rows):
                answers = self._answered_by(key, idx)
                rows = [r | {"extra": (r.get("extra") or {}) | {"answers": answers}} if r["kind"] == "decision" else r for r in rows]
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

    def _answered_by(self, key: str, idx: int) -> list[str]:
        """Refs of the plain-text asks your prompt at ``idx`` answered: those raised since your previous typed prompt."""
        since = -1
        for (data,) in self.db.execute("SELECT data FROM turns WHERE key=? AND idx<? ORDER BY idx DESC", (key, idx)):
            t = json.loads(data)
            if typed(t):
                since = t["idx"]; break
        return [r[0] for r in self.db.execute(
            "SELECT ref FROM asks WHERE key=? AND answered=1 AND kind IN ('plain','text') AND turn_idx>=? AND turn_idx<? ORDER BY turn_idx, ref",
            (key, since, idx))]

    def retire_row(self, key: str, row_id: int, by: str = "operator") -> bool:
        with self._lock:
            self.db.execute("UPDATE ledger SET active=0, retired_by=? WHERE key=? AND id=? AND active=1", (by, key, row_id))
            ok = self.db.execute("SELECT changes()").fetchone()[0] > 0
            if ok:
                self._bump()
            return ok

    def open_ask(self, key: str, ref: str) -> dict[str, Any] | None:
        """The ask, if it's still open and no answer to it is on its way (queued or sent, not yet in the transcript)."""
        r = self.db.execute("SELECT * FROM asks WHERE key=? AND ref=? AND answered=0 AND NOT EXISTS (SELECT 1 FROM messages m "
                            "WHERE m.key=asks.key AND m.ask_ref=asks.ref AND m.status IN ('queued','sent'))", (key, ref)).fetchone()
        return dict(r) if r else None

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

    def children(self, key: str) -> list[str]:
        """Keys of the sessions this one launched (pool jobs), newest ``started_at`` first."""
        return [r[0] for r in self.db.execute(
            "SELECT key FROM sessions WHERE parent_key=? ORDER BY COALESCE(started_at,'') DESC, key", (key,))]

    def usage_rows(self, frm: str | None = None, to: str | None = None) -> list[dict[str, Any]]:
        """Turns by session *and model* — one GROUP BY, in a window when given one. What a session's spend and the
        spend report are both built from: the window filters turns, so a long-lived session shows what it turned
        over in the window rather than all it ever did."""
        cond, args = ["t.started_at IS NOT NULL"], []
        if frm:
            cond.append("t.started_at>=?"); args.append(frm)
        if to:
            cond.append("t.started_at<?"); args.append(to)
        return [_usage_row(r) for r in self.db.execute(_usage_sql(" AND ".join(cond)), args)]

    def usage_rollup(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Those rows combined per session."""
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            _accumulate(out.setdefault(r["key"], _blank_usage()), r)
        return out

    def spend_window(self, frm: str | None, to: str | None, host: str | None = None) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        """/v1/spend's raw material: the usage rows whose turns started in the window, and the sessions they belong to
        (``{key: {...}}``, one query, host-filtered). Sessions with no turns in the window are absent entirely."""
        rows = self.usage_rows(frm, to)
        keys = {r["key"] for r in rows}
        found: dict[str, dict[str, Any]] = {}
        for k in sorted(keys):
            r = self.db.execute("SELECT * FROM sessions WHERE key=?", (k,)).fetchone()
            if r is None or (host and r["host_id"] != host):
                continue
            d = dict(r)
            d["meta"] = json.loads(d["meta"] or "{}")
            d["title"] = d["live_name"] or d["title"]
            for f in ("parent_key", "job", "job_status", "job_brief"):
                d[f] = d[f] or ""
            found[k] = d
        return [r for r in rows if r["key"] in found], found

    def usage_window(self, frm: str | None, to: str | None, host: str | None = None) -> list[dict[str, Any]]:
        """What /v1/spend prices: one row per (session, model) with turns in the window, carrying the session's own
        fields (harness, meta, title, host, the pool-job link) next to its token totals (spend_window's two halves,
        joined)."""
        rows, found = self.spend_window(frm, to, host)
        out = []
        for r in rows:
            s = found[r["key"]]
            out.append({**r, "harness": s["harness"], "host_id": s["host_id"], "meta": s["meta"], "title": s["title"],
                        "started_at": s["started_at"], "parent_key": s["parent_key"], "job": s["job"],
                        "job_status": s["job_status"], "job_brief": s["job_brief"]})
        return out

    def usage_total(self, frm: str | None, to: str | None, host: str | None = None) -> dict[str, Any]:
        """The window's cloud token total by type, over the same rows."""
        tokens = {t: 0 for t in TOKEN_TYPES}
        for r in self.usage_window(frm, to, host):
            for t in TOKEN_TYPES:
                tokens[t] += r["tokens"][t]
        return {"tokens": tokens}

    def turn_usage(self, key: str) -> dict[str, Any]:
        """One session's totals: tokens by type, the tokens to price here (pi prices a cost_usd turn itself), the
        model of its newest turn ('' when none named one) and pi's own cost total."""
        u = _blank_usage()
        for r in self.db.execute(_usage_sql("t.key=?", "t"), (key,)):
            _accumulate(u, _usage_row(r))
        return u

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
        rows = list(self.db.execute(q, args))
        # two queries for the whole list rather than one per row: everyone's turn totals, and the pool-job child counts
        keys = {r["key"] for r in rows}
        usage = {k: v for k, v in self.usage_rollup(self.usage_rows()).items() if k in keys}
        kids = dict(self.db.execute("SELECT parent_key, COUNT(*) FROM sessions WHERE COALESCE(parent_key,'')!='' "
                                    "GROUP BY parent_key"))
        out = []
        for r in rows:
            d = self._session_row(r)
            d["children"] = kids.get(d["key"], 0)
            d["spend"] = self._spend(d, usage.get(d["key"], _blank_usage()))
            out.append(d)
        return out

    def _spend(self, d: dict[str, Any], u: dict[str, Any]) -> dict[str, Any]:
        """What this session's turns cost at the hub's rates (hub/spend.py). usd is null when its model has no rate or
        it turned over no tokens: an unpriced model contributes tokens, never $0. ``local`` is SAINT's, filled in by
        the API for a single session."""
        from ctxswitch.hub.spend import usage_spend
        harness = d.get("harness") or ""
        account = ((d.get("meta") or {}).get("profile") or "default") if harness == "claude" else (harness or "default")
        return usage_spend(u["tokens"], u["model"], u["reported"], self.prices, account=account, priced=u["priced"])

    def _session_row(self, r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r); d["meta"] = json.loads(d["meta"] or "{}"); d["ended"] = bool(d["ended"])
        for k in ("parent_key", "job", "job_status", "job_brief"):      # a pool job says who launched it and how it went
            d[k] = d.get(k) or ""
        if d.get("live_name"):                          # the running session's own name beats the transcript's
            d["title"] = d["live_name"]
        last = self.db.execute("SELECT data FROM turns WHERE key=? ORDER BY idx DESC LIMIT 1", (d["key"],)).fetchone()
        d["last_turn"] = json.loads(last[0]) if last else None
        d["turn_count"] = self.db.execute("SELECT COUNT(*) FROM turns WHERE key=?", (d["key"],)).fetchone()[0]
        d["open_asks"] = [dict(a) for a in self.db.execute("SELECT * FROM asks WHERE key=? AND answered=0 ORDER BY asked_at", (d["key"],))]
        d["workstream"] = workstream_of(d)
        # since you last looked: exchanges that ended after your last view (None = never viewed, so no baseline)
        d["unseen"], d["seen_through"] = None, None
        if d.get("last_viewed_at"):
            r2 = self.db.execute("SELECT COUNT(*), MIN(idx) FROM turns WHERE key=? AND julianday(COALESCE(ended_at, started_at)) > julianday(?)",
                                 (d["key"], d["last_viewed_at"])).fetchone()
            d["unseen"] = r2[0]
            d["seen_through"] = (r2[1] - 1) if r2[1] is not None else (d["last_turn"] or {}).get("idx", -1)
        return d

    def session(self, key: str, turns: int = 50) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM sessions WHERE key=?", (key,)).fetchone()
        if r is None:
            return None
        d = self._session_row(r)
        d["turns"] = [json.loads(t[0]) for t in self.db.execute("SELECT data FROM turns WHERE key=? ORDER BY idx DESC LIMIT ?", (key, turns))][::-1]
        d["asks"] = [dict(a) for a in self.db.execute("SELECT * FROM asks WHERE key=? ORDER BY asked_at", (key,))]
        d["ledger"] = self.ledger(key)
        d["messages"] = self.messages(key)
        st = self.tracker_state(key)
        d["purpose"], d["purpose_history"], d["tracked_through"] = st["purpose"], st["purpose_history"], st["through_idx"]
        d["siblings"] = self.siblings(d)
        d["workstream_messages"] = self.workstream_messages(d.get("workstream"))
        d.update(self.handoff_links(key))
        d["children_keys"] = self.children(key)
        d["children"] = len(d["children_keys"])
        d["spend"] = self._spend(d, self.turn_usage(key))
        return d

    def search(self, query: str, kinds: tuple[str, ...] = SEARCH_KINDS, host: str | None = None,
               limit: int = 30) -> list[dict[str, Any]]:
        """Across every session, newest first: ledger rows (decision / instruction / claim, retired ones too,
        marked), answered questions (as decisions: the question, then your answer) and your own prompts. Every word
        of ``query`` must appear, case-insensitive, in the text (or quote, or question)."""
        words = query.split()
        if not words:
            return []

        def like(cols: list[str]) -> tuple[str, list[str]]:
            pats = ["%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for w in words]
            hay = " || ' ' || ".join(f"coalesce({c},'')" for c in cols)
            return " AND ".join(f"({hay}) LIKE ? ESCAPE '\\'" for _ in pats), pats

        out: list[dict[str, Any]] = []
        hcond, hargs = (" AND s.host_id=?", [host]) if host else ("", [])
        led = [k for k in kinds if k in ("decision", "instruction", "claim")]
        if led:
            w, a = like(["l.text", "l.quote", "json_extract(l.extra,'$.question')"])
            q = (f"SELECT l.key, l.kind, l.text, l.turn_idx, l.active, l.extra, t.started_at AS at FROM ledger l "
                 f"JOIN sessions s ON s.key=l.key LEFT JOIN turns t ON t.key=l.key AND t.idx=l.turn_idx "
                 f"WHERE l.kind IN ({','.join('?' * len(led))}) AND {w}{hcond}")
            for r in self.db.execute(q, [*led, *a, *hargs]):
                d = dict(r); d["active"] = bool(d["active"])
                d["question"] = json.loads(d.pop("extra") or "{}").get("question") or None
                out.append(d)
        if "decision" in kinds:
            w, a = like(["a.text", "a.answer"])
            q = (f"SELECT a.key, 'decision' AS kind, a.answer AS text, a.text AS question, a.turn_idx, 1 AS active, "
                 f"coalesce(a.answered_at, a.asked_at) AS at FROM asks a JOIN sessions s ON s.key=a.key "
                 f"WHERE a.answered=1 AND {w}{hcond}")
            out += [dict(r) | {"active": True} for r in self.db.execute(q, [*a, *hargs])]
        if "prompt" in kinds:
            w, a = like(["json_extract(t.data,'$.user_text')"])
            q = (f"SELECT t.key, 'prompt' AS kind, json_extract(t.data,'$.user_text') AS text, t.idx AS turn_idx, "
                 f"1 AS active, t.started_at AS at FROM turns t JOIN sessions s ON s.key=t.key "
                 f"WHERE json_extract(t.data,'$.user_origin')='human' AND {w}{hcond}")
            out += [dict(r) | {"active": True, "question": None} for r in self.db.execute(q, [*a, *hargs])]
        out.sort(key=lambda d: d.get("at") or "", reverse=True)
        out = out[:limit]
        meta = {}
        for k in {d["key"] for d in out}:
            r = self.db.execute("SELECT host_id, title, live_name FROM sessions WHERE key=?", (k,)).fetchone()
            meta[k] = {"host_id": r["host_id"], "title": r["live_name"] or r["title"]}
        return [d | meta[d["key"]] for d in out]

    def siblings(self, s: dict[str, Any]) -> list[dict[str, Any]]:
        """Other sessions in the same workstream (any host), live first then most recent, each with its standing
        instructions: shown as context, labelled with where they came from, never applied to this session."""
        ws = s.get("workstream")
        if not ws:
            return []
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - SIBLING_DAYS * 86400))
        rows = self.db.execute(
            f"SELECT * FROM sessions WHERE key!=? AND git_branch=? AND (live_status IN ({_LIVE_SQL}) OR last_activity>=?) "
            f"AND EXISTS (SELECT 1 FROM turns t WHERE t.key=sessions.key) "
            f"ORDER BY CASE WHEN live_status IN ({_LIVE_SQL}) THEN 0 ELSE 1 END, last_activity DESC",
            (s["key"], s.get("git_branch") or "", cutoff)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if workstream_of(d) != ws:
                continue
            out.append({k: d.get(k) for k in ("key", "host_id", "harness", "session_id", "live_status", "status_since", "last_activity", "ended")}
                       | {"title": d.get("live_name") or d.get("title"), "ended": bool(d["ended"]),
                          "instructions": [x["text"] for x in self.ledger(d["key"], active_only=True) if x["kind"] == "instruction"][-3:]})
            if len(out) >= SIBLINGS:
                break
        return out

    def attention(self, host: str | None = None) -> list[dict[str, Any]]:
        """Open asks on sessions that are not ended (and not moved past), plus idle live sessions, oldest wait first."""
        q = ("SELECT a.*, s.host_id, s.harness, s.title, s.live_status, s.status_since FROM asks a JOIN sessions s ON s.key=a.key "
             "WHERE a.answered=0 AND s.live_status!='ended' AND s.ended=0 AND s.harness!='shell' "     # a bash asks nothing
             # a later prompt (yours or a peer's, not a command wrapper or task notice) means it moved on: a dialog
             # left open when the session was killed and resumed never gets an answer in the transcript
             "AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.key=a.key AND t.idx>a.turn_idx "
             "AND json_extract(t.data,'$.user_origin') IN ('human','peer') "
             "AND substr(ltrim(coalesce(json_extract(t.data,'$.user_text'),'')),1,1) NOT IN ('','<'))")
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
            # a shell session is at its prompt nearly all the time: idle there is not "your move"
            if r["key"] not in asked and not (r["harness"] == "shell" and r["live_status"] == "idle"):
                items.append(dict(r) | {"item": r["live_status"], "asked_at": r["status_since"]})
        return items

    def close(self) -> None:
        self.db.close()


def workstream_of(s: dict[str, Any]) -> str | None:
    """Repo + branch. The repo is its remote's last two path parts (owner/name): the host part differs between
    clones (https vs ssh, per-host ssh aliases like github-personal:). No remote: its root on that host, one
    checkout. None outside a repo."""
    remote, root = (s.get("repo_remote") or "").strip(), (s.get("repo_root") or "").rstrip("/")
    if remote:
        r = remote.rstrip("/").removesuffix(".git").split("://", 1)[-1].replace(":", "/")
        repo = "/".join([p for p in r.split("/") if p][-2:]).lower()
    elif root:
        repo = f"{s.get('host_id')}:{root}"
    else:
        return None
    return f"{repo}@{s.get('git_branch') or ''}"


def typed(turn: dict[str, Any]) -> bool:
    """A prompt you typed: not a task notification or peer message, and not a command wrapper (<bash-input> etc)."""
    text = (turn.get("user_text") or "").strip()
    return turn.get("user_origin") == "human" and bool(text) and not text.startswith("<")


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


def _clip(text: str | None, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"

