"""pi transcript parser (~/.pi/agent/sessions/<cwd-slug>/<ts>_<id>.jsonl, format version 3).

Entries form a tree via parentId. Phase 1 reads the file linearly and flags a branch switch (an entry whose
parentId is not the previous entry) as an event; abandoned branches still count toward the tally. Surveyed on
pi 1.2.0 and 1.4.0: 1.4.0 adds ``compaction``, ``custom`` and ``context_edit`` entries.

  session                 header: id, cwd, timestamp, version
  message/user            content: [{type:text}] (or str)
  message/assistant       content: text | thinking | toolCall(name, arguments); message.model, .provider,
                          .usage {input,output,cacheRead,cacheWrite,cost.total}, stopReason
  message/toolResult      toolName, isError
  message/system, message/bashExecution, custom_message, custom, model_change, thinking_level_change,
  compaction, context_edit
"""
from __future__ import annotations

from typing import Any

from ctxswitch.common.schema import Ask, Turn, add_usage, trim

EDIT_TOOLS = {"edit", "write", "multi_edit", "notebook_edit"}
READ_TOOLS = {"read", "grep", "find", "ls", "glob", "fetch", "web_search", "brave-search", "mcp__brave_search"}
AGENT_TOOLS = {"subagent", "agent", "task"}


class PiParser:
    def __init__(self, max_text: int = 4000):
        self.max_text = max_text
        self.meta: dict[str, Any] = {}
        self._done: list[Turn] = []
        self._cur: Turn | None = None
        self._n = 0
        self._last_id: str | None = None
        self._pending_events: list[str] = []
        self.bad_lines = 0

    def state(self) -> dict[str, Any]:
        return {"meta": self.meta, "cur": self._cur.to_dict() if self._cur else None, "n": self._n,
                "last": self._last_id, "pending": self._pending_events, "bad": self.bad_lines}

    @classmethod
    def from_state(cls, st: dict[str, Any], max_text: int = 4000) -> "PiParser":
        p = cls(max_text)
        p.meta = st["meta"]; p._n = st["n"]; p._last_id = st["last"]; p._pending_events = st["pending"]
        p.bad_lines = st["bad"]
        p._cur = Turn.from_dict(st["cur"]) if st["cur"] else None
        return p

    def drain(self) -> list[Turn]:
        out, self._done = self._done, []
        return out

    @property
    def current(self) -> Turn | None:
        return self._cur

    def finish(self) -> None:
        if self._cur is not None:
            self._close(self._cur.ended_at)

    def feed(self, e: dict[str, Any], offset_end: int) -> None:
        t = e.get("type")
        ts = e.get("timestamp")
        if isinstance(ts, str):
            self.meta["last_activity"] = ts
            self.meta.setdefault("started_at", ts)
        pid = e.get("parentId")
        if t != "session" and self._last_id is not None and pid != self._last_id and pid is not None:
            if t == "message" and (e.get("message") or {}).get("role") == "user":
                self._pending_events.append("branch")       # belongs to the turn this prompt starts
            else:
                self._event("branch", offset_end, ts)
        if isinstance(e.get("id"), str):
            self._last_id = e["id"]
        if t == "session":
            self.meta["session_id"] = e.get("id", "")
            self.meta["cwd"] = e.get("cwd", "")
            self.meta["format_version"] = e.get("version")
        elif t == "message":
            self._message(e, offset_end)
        elif t == "model_change":
            self.meta["model"] = f"{e.get('provider','')}/{e.get('modelId','')}".strip("/")
        elif t == "compaction":
            self._event("compaction", offset_end, ts)
        elif t == "context_edit":
            self._event("context_edit", offset_end, ts)

    def _event(self, name: str, offset_end: int, ts: str | None) -> None:
        if self._cur is not None:
            self._cur.events.append(name); self._cur.offset_end = offset_end
            if ts: self._cur.ended_at = ts
        else:
            self._pending_events.append(name)

    def _close(self, ts: str | None) -> None:
        cur = self._cur
        if cur is None:
            return
        cur.complete = True
        if ts and not cur.ended_at:
            cur.ended_at = ts
        text = cur.assistant_text.rstrip()
        if text.endswith("?"):
            cur.asks.append(Ask("plain", trim(text.rsplit("\n", 1)[-1].strip(), 400), f"{cur.idx}:plain"))
        self._done.append(cur)
        self._cur = None

    @staticmethod
    def _text_of(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
        return ""

    def _message(self, e: dict[str, Any], offset_end: int) -> None:
        m = e.get("message") or {}
        role = m.get("role")
        ts = e.get("timestamp")
        if role == "user":
            text = self._text_of(m.get("content"))
            if self._cur is not None:
                self._close(ts)
                for a in self._done[-1].asks:
                    if a.kind == "plain" and not a.answered:
                        a.answered, a.answer, a.answered_at = True, trim(text, 400), ts
            events, self._pending_events = self._pending_events, []
            self._cur = Turn(idx=self._n, started_at=ts or "", ended_at=ts, user_text=trim(text, self.max_text),
                             user_origin="human", assistant_text="", events=events, offset_end=offset_end)
            self._n += 1
            if "first_prompt" not in self.meta and text.strip():
                self.meta["first_prompt"] = trim(text.strip().splitlines()[0], 120)
            return
        if self._cur is None:
            if role in ("assistant", "toolResult"):
                self._cur = Turn(idx=self._n, started_at=ts or "", ended_at=ts, user_text="", user_origin="unknown",
                                 assistant_text="", offset_end=offset_end)
                self._n += 1
            else:
                return
        cur = self._cur
        if role == "assistant":
            model, provider = m.get("model"), m.get("provider")
            if isinstance(model, str) and model:
                # pi names the provider that served the model; "anthropic" is the plain Claude case
                cur.model = model if provider in ("", None, "anthropic") else f"{provider}/{model}"
                self.meta["model"] = f"{provider or ''}/{model}".strip("/")
            u = m.get("usage")
            if isinstance(u, dict):
                add_usage(cur, u, (("input", "input"), ("output", "output"),
                                   ("cache_read", "cacheRead"), ("cache_creation", "cacheWrite")))
                cost = (u.get("cost") or {}).get("total") if isinstance(u.get("cost"), dict) else None
                # pi prices its cloud providers itself; 0 (a local run) is not a price
                if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0:
                    cur.cost_usd = (cur.cost_usd or 0.0) + float(cost)
            for c in m.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and c.get("text", "").strip():
                    cur.assistant_text = trim(c["text"], self.max_text)
                elif c.get("type") == "toolCall":
                    name = (c.get("name") or "").lower()
                    if name in EDIT_TOOLS: cur.tally.edits += 1
                    elif name == "bash": cur.tally.bash += 1
                    elif name in READ_TOOLS: cur.tally.reads += 1
                    elif name in AGENT_TOOLS: cur.tally.agents += 1
                    else: cur.tally.other += 1
            if m.get("stopReason") in ("aborted", "error"):
                cur.events.append(f"stop:{m.get('stopReason')}")
            # the run ended (not a pause for tool results): complete, but open for a steer that continues it
            cur.complete = m.get("stopReason") in ("stop", "length", "aborted", "error")
        elif role == "toolResult":
            if m.get("isError"):
                cur.tally.errors += 1
        elif role == "bashExecution":
            cur.events.append("user_bash")
        cur.offset_end = offset_end
        if ts:
            cur.ended_at = ts
