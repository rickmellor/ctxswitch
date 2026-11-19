"""Google Antigravity (agy) conversation parser.

agy keeps one SQLite "trajectory" per conversation at ~/.gemini/antigravity-cli/conversations/<id>.db (WAL mode),
table ``steps(idx, step_type, status, metadata, step_payload, …)`` with protobuf blobs, and a summary row per
conversation in ../conversation_summaries.db (title, workspace_uris, status, step_count, last_user_input_time).
There is no public schema for the blobs, so this reads them as raw protobuf wire format and takes the fields read off
agy 1.4.11 — the step-15 timings and usage below are inferred from one conversation, not from documentation:

  step_type 14  USER_INPUT         payload f19.f2 = the prompt text; metadata f1 = {f1 seconds, f2 nanos}
  step_type 15  PLANNER_RESPONSE   payload f20.f1 = the model's text (f8 the same, rendered); metadata f1 = the request
                                   went out, f6 = first token, f7 = last token (each {f1 seconds, f2 nanos}), f9 = the
                                   usage (f2 in, f3 out, f5 cache-read tokens) — so one step is one call record
  other         a tool call        metadata f4 = {f1 call id, f2 tool name, f3 json args}; step 132 = read/run

The step index is the replay offset: ``feed`` takes rows in idx order and the collector stores how many it has
consumed. A turn is a USER_INPUT and everything after it up to the next one.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ctxswitch.collector.adapters.claude import _POOL_HOOK
from ctxswitch.common.schema import Ask, Turn, trim

USER_INPUT, PLANNER_RESPONSE = 14, 15
SYSTEM_MESSAGE = 101                                     # a system message, e.g. what a SessionStart hook injected
# A tool step's status (agy 1.5.12, probed live): 9 while it waits for the operator's approval, then 2 (running) and
# 3 (done) once allowed, or 6 / 7 once denied or cancelled. ask_question has its own question ask (the hooks).
TOOL_STEP, WAITING, ALLOWED = 132, 9, (2, 3)
QUESTION_TOOL = "ask_question"
READ_TOOLS = {"read_file", "view_file", "view_file_outline", "list_dir", "find_by_name", "grep_search", "codebase_search",
              "search_web", "read_url_content", "view_code_item", "read_resource", "read_terminal", "view_files"}
EDIT_TOOLS = {"write_to_file", "replace_file_content", "multi_replace_file_content", "edit_file", "create_file",
              "edit_notebook", "delete_directory"}
BASH_TOOLS = {"run_command", "command_status", "send_command_input"}
AGENT_TOOLS = {"browser_subagent", "subagent", "delegate"}


# -- raw protobuf ---------------------------------------------------------------------------------------------------
def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        c = b[i]; i += 1; r |= (c & 0x7F) << s; s += 7
        if c < 0x80:
            return r, i


def fields(b: bytes) -> dict[int, list[Any]]:
    """Top-level fields of a message: {field number: [values]}; length-delimited values stay bytes."""
    out: dict[int, list[Any]] = {}
    i = 0
    try:
        while i < len(b):
            tag, i = _varint(b, i); f, wt = tag >> 3, tag & 7
            if wt == 0:
                v, i = _varint(b, i)
            elif wt == 1:
                v, i = b[i:i + 8], i + 8
            elif wt == 5:
                v, i = b[i:i + 4], i + 4
            elif wt == 2:
                n, i = _varint(b, i); v, i = b[i:i + n], i + n
            else:
                break
            out.setdefault(f, []).append(v)
    except (IndexError, TypeError):
        pass
    return out


def path(b: bytes | None, *nums: int) -> Any:
    """Follow field numbers into nested messages; the first value at each hop. None when absent."""
    v: Any = b or b""
    for n in nums:
        if not isinstance(v, (bytes, bytearray)):
            return None
        vals = fields(v).get(n)
        if not vals:
            return None
        v = vals[0]
    return v


def text(v: Any) -> str:
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    return ""


def stamp(v: Any) -> str | None:
    """A {f1 seconds, f2 nanos} timestamp message as ISO (whole seconds; a reply's span wants ``stamp_epoch``)."""
    if not isinstance(v, (bytes, bytearray)):
        return None
    f = fields(v)
    s = f.get(1, [None])[0]
    if not isinstance(s, int) or s < 10 ** 9:
        return None
    return datetime.fromtimestamp(s, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def stamp_epoch(v: Any) -> float | None:
    """The same {f1 seconds, f2 nanos} message as epoch seconds, nanos included — a reply's span is a few seconds, so
    dropping the fraction would round away a real part of it."""
    if not isinstance(v, (bytes, bytearray)):
        return None
    f = fields(v)
    s = f.get(1, [None])[0]
    if not isinstance(s, int) or s < 10 ** 9:
        return None
    ns = f.get(2, [0])[0]
    return s + (ns / 10 ** 9 if isinstance(ns, int) else 0.0)


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


# -- the parser ----------------------------------------------------------------------------------------------------
def question_answer(result: str) -> str:
    """The operator's choice from an ask_question's tool result ("Created At: …\nCompleted At: …\nA1: (Recommended) Yes,
    send it"): the A<n> lines' text, several joined by " | "; "" when there's none (a skipped question)."""
    out = []
    for ln in (result or "").splitlines():
        head, sep, rest = ln.partition(":")
        if sep and head.strip()[:1] == "A" and head.strip()[1:].isdigit() and rest.strip():
            out.append(rest.strip())
    return trim(" | ".join(out), 400)


class AgyParser:
    def __init__(self, max_text: int = 4000):
        self.max_text = max_text
        self.meta: dict[str, Any] = {"pool_seen": True}  # pool*: what saint-pool's SessionStart hook told it (no catch-up needed)
        self._done: list[Turn] = []
        self._cur: Turn | None = None
        self._n = 0
        self._calls: list[dict[str, Any]] = []       # completed model calls awaiting drain_calls()
        self.pending: dict[str, dict[str, Any]] = {}  # tool steps waiting for approval, by step idx (str: json keys)
        self._hooks: list[dict[str, Any]] = []       # hook events the steps imply, awaiting drain_hooks()
        self.bad_lines = 0

    def state(self) -> dict[str, Any]:
        return {"meta": self.meta, "cur": self._cur.to_dict() if self._cur else None, "n": self._n, "bad": self.bad_lines,
                "pending": self.pending}

    @classmethod
    def from_state(cls, st: dict[str, Any], max_text: int = 4000) -> "AgyParser":
        p = cls(max_text)
        p.meta = st["meta"]; p._n = st["n"]; p.bad_lines = st.get("bad", 0)
        p._cur = Turn.from_dict(st["cur"]) if st["cur"] else None
        p.pending = dict(st.get("pending") or {})
        return p

    def drain(self) -> list[Turn]:
        out, self._done = self._done, []
        return out

    def drain_calls(self) -> list[dict[str, Any]]:
        """Call records for the model replies completed since the last drain — ``end``/``dur_s``/``out``/``model``, no
        ``agent`` key. A step row is written whole, so nothing here is still open and nothing needs persisting."""
        out, self._calls = self._calls, []
        return out

    @property
    def current(self) -> Turn | None:
        return self._cur

    def finish(self) -> None:
        if self._cur is not None:
            self._close(self._cur.ended_at)

    def feed(self, row: dict[str, Any], offset_end: int) -> None:
        """One ``steps`` row (idx, step_type, status, metadata, step_payload); offset_end = idx + 1."""
        t = row.get("step_type")
        meta, payload = row.get("metadata") or b"", row.get("step_payload") or b""
        ts = stamp(path(meta, 1)) or stamp(path(payload, 5, 1))
        if ts:
            self.meta["last_activity"] = ts
            self.meta.setdefault("started_at", ts)
        if t == USER_INPUT:
            self._user(text(path(payload, 19, 2)), ts, offset_end)
        elif t == PLANNER_RESPONSE:
            body = text(path(payload, 20, 1))
            cur = self._turn(ts, offset_end)
            if body.strip():
                cur.assistant_text = trim(body, self.max_text)
            self._call(meta)
        else:
            self.pool_step(row)
            self.step_status(row, ts)
            self._tool(meta, ts, offset_end)

    def step_status(self, row: dict[str, Any], ts: str | None = None) -> None:
        """What a tool step's status says the board should show: a permission ask while it waits for the operator
        (status 9), resolved allowed or denied when it moves on, and an ask_question's actual answer once it's done.
        Called for every newly read step and again for each still-waiting one (the indexer re-reads them by idx), so a
        step read while waiting is seen again when it resolves. Events are named by conversation, step and outcome:
        a re-read or a restart can't repeat one."""
        if row.get("step_type") != TOOL_STEP:
            return
        idx, status = str(row.get("idx")), row.get("status")
        meta, payload = row.get("metadata") or b"", row.get("step_payload") or b""
        call, tool = text(path(meta, 4, 1)), text(path(meta, 4, 2))
        ts = ts or stamp(path(meta, 1)) or ""
        if tool == QUESTION_TOOL:
            if status in ALLOWED:
                answer = question_answer(text(path(payload, 140, 2, 1)))
                if answer and call:
                    self._hooks.append({"name": f"answer:{idx}", "event": "AskAnswered", "ts": ts,
                                        "data": {"tool_name": tool, "tool_use_id": call, "answer": answer}})
            return
        if status == WAITING and idx not in self.pending:
            try:
                args = json.loads(text(path(meta, 4, 3)) or "{}")
            except ValueError:
                args = {}
            self.pending[idx] = {"call": call, "tool": tool}
            self._hooks.append({"name": f"perm:{idx}", "event": "PermissionRequest", "ts": ts,
                                "data": {"tool_name": tool, "tool_use_id": call,
                                         "tool_input": {k: trim(v, 400) if isinstance(v, str) else v
                                                        for k, v in (args if isinstance(args, dict) else {}).items()}}})
        elif status != WAITING and idx in self.pending:
            p = self.pending.pop(idx)
            self._hooks.append({"name": f"perm:{idx}:done", "event": "PermissionResolved", "ts": ts,
                                "data": {"tool_name": p["tool"], "tool_use_id": p["call"],
                                         "answer": "allowed" if status in ALLOWED else "denied"}})

    def drain_hooks(self) -> list[dict[str, Any]]:
        """The hook events the steps implied since the last drain ({name, event, ts, data}; the indexer adds the rest)."""
        out, self._hooks = self._hooks, []
        return out

    def pool_step(self, row: dict[str, Any]) -> None:
        """What saint-pool's SessionStart hook told this conversation about the local Qwen pool. agy 1.5.12 keeps a
        hook's injected text as a system-message step ("…injected by a SessionStart hook:\nLOCAL POOL ON (…)…"),
        read with the same patterns as Claude's hook output. A pool that's off makes the hook print nothing, so agy
        records no step and the session stays "never told" (Claude's empty stdout reads as "off")."""
        if row.get("step_type") != SYSTEM_MESSAGE:
            return
        meta, payload = row.get("metadata") or b"", row.get("step_payload") or b""
        blob = (payload + meta).decode("utf-8", "replace")
        if "SessionStart hook" not in blob:
            return
        state = next((st for st, rx in _POOL_HOOK if rx.search(blob)), "")
        if not state:
            return
        self.meta["pool"], self.meta["pool_how"] = state, "startup"   # agy runs SessionStart on a resume too
        ts = stamp(path(meta, 1)) or stamp(path(payload, 5, 1))
        if ts:
            self.meta["pool_at"] = ts

    def _call(self, meta: bytes) -> None:
        """One planner response as a call record: request sent at metadata f1, its last token at f7, its output
        tokens at f9.f3. The model's name isn't in the step, so it stays empty."""
        start, end = stamp_epoch(path(meta, 1)), stamp_epoch(path(meta, 7))
        out = path(meta, 9, 3)
        if not isinstance(out, int) or out <= 0 or start is None or end is None or end <= start:
            return
        self._calls.append({"end": iso(end), "dur_s": round(end - start, 3), "out": out, "model": ""})

    def _turn(self, ts: str | None, offset_end: int) -> Turn:
        """The open turn, or one with no prompt when the trajectory starts mid-way (a resumed export)."""
        if self._cur is None:
            self._cur = Turn(idx=self._n, started_at=ts or "", ended_at=ts, user_text="", user_origin="unknown",
                             assistant_text="", offset_end=offset_end)
            self._n += 1
        self._cur.offset_end = offset_end
        if ts:
            self._cur.ended_at = ts
        return self._cur

    def _user(self, prompt: str, ts: str | None, offset_end: int) -> None:
        if self._cur is not None:
            self._close(ts)
            for a in self._done[-1].asks:
                if a.kind == "plain" and not a.answered:
                    a.answered, a.answer, a.answered_at = True, trim(prompt, 400), ts
        self._cur = Turn(idx=self._n, started_at=ts or "", ended_at=ts, user_text=trim(prompt, self.max_text),
                         user_origin="human", assistant_text="", offset_end=offset_end)
        self._n += 1
        if "first_prompt" not in self.meta and prompt.strip():
            self.meta["first_prompt"] = trim(prompt.strip().splitlines()[0], 120)

    def _tool(self, meta: bytes, ts: str | None, offset_end: int) -> None:
        call = path(meta, 4)
        name = text(path(call, 2)) if isinstance(call, (bytes, bytearray)) else ""
        cur = self._turn(ts, offset_end)
        tally = cur.tally
        if not name:
            tally.other += 1
        elif name in READ_TOOLS:
            tally.reads += 1
        elif name in EDIT_TOOLS:
            tally.edits += 1
        elif name in BASH_TOOLS:
            tally.bash += 1
        elif name in AGENT_TOOLS:
            tally.agents += 1
        else:
            tally.other += 1

    def _close(self, ts: str | None) -> None:
        cur = self._cur
        if cur is None:
            return
        cur.complete = True
        if ts and not cur.ended_at:
            cur.ended_at = ts
        body = cur.assistant_text.rstrip()
        if body.endswith("?"):
            cur.asks.append(Ask("plain", trim(body.rsplit("\n", 1)[-1].strip(), 400), f"{cur.idx}:plain"))
        self._done.append(cur)
        self._cur = None


# -- the conversation store -------------------------------------------------------------------------------------------
def read_steps(db_path: str, start: int) -> list[dict[str, Any]]:
    """Steps from ``start`` on, in order. Read-only and WAL-aware; [] when the db is locked or malformed."""
    import sqlite3
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = c.execute("SELECT idx, step_type, status, metadata, step_payload FROM steps WHERE idx >= ? ORDER BY idx",
                             (start,)).fetchall()
        finally:
            c.close()
    except sqlite3.Error:
        return []
    return [{"idx": r[0], "step_type": r[1], "status": r[2], "metadata": r[3], "step_payload": r[4]} for r in rows]


def read_steps_at(db_path: str, idxs: list[int]) -> list[dict[str, Any]]:
    """The steps at exactly these indexes (the ones still waiting for approval), in order; [] as read_steps does."""
    import sqlite3
    if not idxs:
        return []
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        try:
            q = f"SELECT idx, step_type, status, metadata, step_payload FROM steps WHERE idx IN ({','.join('?' * len(idxs))}) ORDER BY idx"
            rows = c.execute(q, list(idxs)).fetchall()
        finally:
            c.close()
    except sqlite3.Error:
        return []
    return [{"idx": r[0], "step_type": r[1], "status": r[2], "metadata": r[3], "step_payload": r[4]} for r in rows]


def step_count(db_path: str) -> int | None:
    import sqlite3
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        try:
            return int(c.execute("SELECT COUNT(*) FROM steps").fetchone()[0])
        finally:
            c.close()
    except sqlite3.Error:
        return None


def read_summary(summaries_db: str, conversation_id: str) -> dict[str, Any]:
    """title, cwd, status, busy, last_user_input for one conversation from conversation_summaries.db ({} if none)."""
    import sqlite3
    try:
        c = sqlite3.connect(f"file:{summaries_db}?mode=ro", uri=True, timeout=1.0)
        try:
            r = c.execute("SELECT title, workspace_uris, status, not_fully_idle, last_user_input_time, last_modified_time, killed "
                          "FROM conversation_summaries WHERE conversation_id = ?", (conversation_id,)).fetchone()
        finally:
            c.close()
    except sqlite3.Error:
        return {}
    if not r:
        return {}
    cwd = ""
    try:
        uris = json.loads(r[1] or "[]")
        if uris and isinstance(uris[0], str) and uris[0].startswith("file://"):
            cwd = uris[0][len("file://"):]
    except ValueError:
        pass
    return {"title": r[0] or "", "cwd": cwd, "status": r[2] or "", "busy": bool(r[3]), "last_user_input": r[4] or "",
            "last_modified": r[5] or "", "killed": bool(r[6])}
