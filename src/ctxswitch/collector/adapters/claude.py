"""Claude Code transcript parser (~/.claude/projects/<cwd-slug>/<sessionId>.jsonl).

Entry types that matter (surveyed across 2.1.128–2.1.312 files, see DESIGN.md):
  user        human prompt (content: str | [{type:text}]) or tool results (content: [{type:tool_result}]);
              isMeta + origin.kind=peer = message from another session; isSidechain = subagent traffic (skipped);
              isCompactSummary = compaction summary injected as a user entry.
  assistant   content blocks text / tool_use; message.model.
  system      subtype turn_duration (the harness closed the turn), compact_boundary, api_error, away_summary,
              local_command, scheduled_task_fire.
  ai-title / custom-title / agent-name / permission-mode   session metadata.
  everything else (attachment, atis-latch, last-prompt, mode, pr-link, queue-operation, file-history-*,
  cost-state, api-request*, tools-used)   ignored.
"""
from __future__ import annotations

import re
from typing import Any

from ctxswitch.common.schema import Ask, ToolTally, Turn, trim

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch", "ToolSearch"}
AGENT_TOOLS = {"Agent", "Task", "Workflow"}
PLAN_REJECTED = "The user doesn't want to proceed with this tool use."
_REJECT_FEEDBACK = re.compile(r"the user said:\s*(.*)\Z", re.S)
_CMD = re.compile(r"<command-name>(/[^<\s]+)</command-name>")


class ClaudeParser:
    def __init__(self, max_text: int = 4000):
        self.max_text = max_text
        self.meta: dict[str, Any] = {}          # cwd, git_branch, harness_version, title, name, model, permission_mode,
                                                # started_at, last_activity, session_id
        self._done: list[Turn] = []
        self._cur: Turn | None = None
        self._n = 0
        self._tools: dict[str, str] = {}        # pending tool_use id -> tool name
        self._pending_events: list[str] = []    # events seen before the next turn opens
        self.bad_lines = 0

    # -- persistence -------------------------------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        return {"meta": self.meta, "cur": self._cur.to_dict() if self._cur else None, "n": self._n,
                "tools": self._tools, "pending": self._pending_events, "bad": self.bad_lines}

    @classmethod
    def from_state(cls, st: dict[str, Any], max_text: int = 4000) -> "ClaudeParser":
        p = cls(max_text)
        p.meta = st["meta"]; p._n = st["n"]; p._tools = st["tools"]; p._pending_events = st["pending"]
        p.bad_lines = st["bad"]
        p._cur = Turn.from_dict(st["cur"]) if st["cur"] else None
        return p

    # -- output ------------------------------------------------------------------------------------------------
    def drain(self) -> list[Turn]:
        """Completed turns since the last drain. The open turn is available as ``current``."""
        out, self._done = self._done, []
        return out

    @property
    def current(self) -> Turn | None:
        return self._cur

    def finish(self) -> None:
        """Session is over (file no longer live): close the open turn."""
        if self._cur is not None:
            self._close(self._cur.ended_at)

    # -- feeding -----------------------------------------------------------------------------------------------
    def feed(self, e: dict[str, Any], offset_end: int) -> None:
        t = e.get("type")
        ts = e.get("timestamp")
        if isinstance(ts, str):
            self.meta["last_activity"] = ts
            self.meta.setdefault("started_at", ts)
        for k_src, k_dst in (("cwd", "cwd"), ("gitBranch", "git_branch"), ("version", "harness_version"),
                             ("sessionId", "session_id")):
            v = e.get(k_src)
            if isinstance(v, str) and v and (k_dst == "git_branch" or k_dst not in self.meta):
                self.meta[k_dst] = v
        if t == "user":
            self._user(e, offset_end)
        elif t == "assistant":
            self._assistant(e, offset_end)
        elif t == "system":
            self._system(e, offset_end)
        elif t == "ai-title":
            self.meta.setdefault("title", e.get("aiTitle", ""))
            if not self.meta.get("custom_title"):
                self.meta["title"] = e.get("aiTitle", "") or self.meta.get("title", "")
        elif t == "custom-title":
            self.meta["title"] = e.get("customTitle", "") or self.meta.get("title", "")
            self.meta["custom_title"] = True
        elif t == "agent-name":
            self.meta["name"] = e.get("agentName", "") or self.meta.get("name", "")
        elif t == "permission-mode":
            self.meta["permission_mode"] = e.get("permissionMode", "") or self.meta.get("permission_mode", "")

    # -- helpers -----------------------------------------------------------------------------------------------
    def _event(self, name: str, offset_end: int, ts: str | None) -> None:
        if self._cur is not None:
            self._cur.events.append(name); self._touch(offset_end, ts)
        else:
            self._pending_events.append(name)

    def _touch(self, offset_end: int, ts: str | None) -> None:
        assert self._cur is not None
        self._cur.offset_end = offset_end
        if ts:
            self._cur.ended_at = ts

    def _close(self, ts: str | None) -> None:
        cur = self._cur
        if cur is None:
            return
        cur.complete = True
        if ts and not cur.ended_at:
            cur.ended_at = ts
        text = cur.assistant_text.rstrip()
        if text.endswith("?") and not any(a.kind in ("question", "plan") and not a.answered for a in cur.asks):
            last = text.rsplit("\n", 1)[-1].strip()
            cur.asks.append(Ask("plain", trim(last, 400), f"{cur.idx}:plain"))
        self._done.append(cur)
        self._cur = None

    def _open(self, e: dict[str, Any], text: str, origin: str, offset_end: int) -> None:
        if self._cur is not None:
            # a new prompt answers any plain question left open by the previous turn
            for a in self._cur.asks:
                if a.kind == "plain" and not a.answered:
                    a.answered, a.answer, a.answered_at = True, trim(text, 400), e.get("timestamp")
            self._close(e.get("timestamp"))
            # the plain-ask answer rule above runs before close adds its own; re-check the closed turn
            prev = self._done[-1]
            for a in prev.asks:
                if a.kind == "plain" and not a.answered:
                    a.answered, a.answer, a.answered_at = True, trim(text, 400), e.get("timestamp")
        events = self._pending_events; self._pending_events = []
        m = _CMD.search(text)
        if m:
            events.append(f"slash_command:{m.group(1)}")
        self._cur = Turn(idx=self._n, started_at=e.get("timestamp") or "", ended_at=e.get("timestamp"),
                         user_text=trim(text, self.max_text), user_origin=origin, assistant_text="",
                         events=events, offset_end=offset_end)
        self._n += 1
        if not self.meta.get("title") and origin == "human" and not m:
            self.meta["first_prompt"] = trim(text.strip().splitlines()[0] if text.strip() else "", 120)

    def _user(self, e: dict[str, Any], offset_end: int) -> None:
        if e.get("isSidechain"):
            return
        if e.get("isCompactSummary"):
            self._event("compaction", offset_end, e.get("timestamp")); return
        msg = e.get("message") or {}
        content = msg.get("content")
        origin_kind = ((e.get("origin") or {}).get("kind")) or ""
        if isinstance(content, str):
            if e.get("isMeta") and origin_kind != "peer":
                self._event("meta_input", offset_end, e.get("timestamp")); return
            origin = "peer" if origin_kind == "peer" else "task" if origin_kind == "task-notification" else "human"
            self._open(e, content, origin, offset_end); return
        if not isinstance(content, list):
            return
        texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        results = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_result"]
        if results:
            self._tool_results(e, results, offset_end)
        if texts and not results:
            if e.get("isMeta") and origin_kind != "peer":
                self._event("meta_input", offset_end, e.get("timestamp")); return
            origin = "peer" if origin_kind == "peer" else "task" if origin_kind == "task-notification" else "human"
            self._open(e, "\n".join(texts), origin, offset_end)

    def _tool_results(self, e: dict[str, Any], results: list[dict], offset_end: int) -> None:
        cur = self._cur
        tur = e.get("toolUseResult")
        for r in results:
            tid = r.get("tool_use_id", "")
            name = self._tools.pop(tid, "")
            if cur is None:
                continue
            if r.get("is_error"):
                cur.tally.errors += 1
            text = r.get("content")
            if isinstance(text, list):
                text = "\n".join(c.get("text", "") for c in text if isinstance(c, dict))
            text = text if isinstance(text, str) else ""
            ask = next((a for a in cur.asks if a.ref == tid), None)
            if ask is None:
                continue
            if name == "AskUserQuestion" or ask.kind == "question":
                answers = tur.get("answers") if isinstance(tur, dict) else None
                if isinstance(answers, dict) and answers:
                    ans = "; ".join(f"{q}: {a}" for q, a in answers.items())
                else:
                    ans = text
                ask.answered, ask.answer, ask.answered_at = True, trim(ans, 1000), e.get("timestamp")
            elif ask.kind == "plan":
                if text.startswith(PLAN_REJECTED) or r.get("is_error"):
                    m = _REJECT_FEEDBACK.search(text)
                    ask.answer = "rejected: " + trim(m.group(1).strip() if m else text, 1000)
                else:
                    ask.answer = "approved"
                ask.answered, ask.answered_at = True, e.get("timestamp")
        if cur is not None:
            self._touch(offset_end, e.get("timestamp"))

    def _assistant(self, e: dict[str, Any], offset_end: int) -> None:
        if e.get("isSidechain"):
            return
        msg = e.get("message") or {}
        model = msg.get("model")
        if isinstance(model, str) and model:
            self.meta["model"] = model
        if self._cur is None:
            # assistant output with no prompt we saw (resumed mid-file): open an anonymous turn
            self._open(e, "", "unknown", offset_end)
        cur = self._cur
        assert cur is not None
        content = msg.get("content")
        if isinstance(content, str):
            if content.strip():
                cur.assistant_text = trim(content, self.max_text)
        elif isinstance(content, list):
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and c.get("text", "").strip():
                    cur.assistant_text = trim(c["text"], self.max_text)
                elif c.get("type") == "tool_use":
                    name = c.get("name", ""); tid = c.get("id", "")
                    self._tools[tid] = name
                    if name in EDIT_TOOLS: cur.tally.edits += 1
                    elif name == "Bash": cur.tally.bash += 1
                    elif name in READ_TOOLS: cur.tally.reads += 1
                    elif name in AGENT_TOOLS: cur.tally.agents += 1
                    else: cur.tally.other += 1
                    inp = c.get("input") or {}
                    if name == "AskUserQuestion":
                        qs = inp.get("questions") or []
                        qtext = " | ".join(q.get("question", "") for q in qs if isinstance(q, dict)) or "(question)"
                        cur.asks.append(Ask("question", trim(qtext, 600), tid))
                    elif name == "ExitPlanMode":
                        cur.asks.append(Ask("plan", "Plan approval requested", tid))
        self._touch(offset_end, e.get("timestamp"))

    def _system(self, e: dict[str, Any], offset_end: int) -> None:
        sub = e.get("subtype")
        ts = e.get("timestamp")
        if sub == "turn_duration":
            if self._cur is not None:
                self._touch(offset_end, ts)
                self._cur.complete = True      # harness closed the turn; stays open for peer follow-ups
        elif sub == "compact_boundary":
            self._event("compaction", offset_end, ts)
        elif sub == "api_error":
            self._event("api_error", offset_end, ts)
        elif sub in ("away_summary", "local_command", "scheduled_task_fire", "model_refusal_no_fallback"):
            self._event(sub, offset_end, ts)
