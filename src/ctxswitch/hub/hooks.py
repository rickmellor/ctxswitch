"""Hook events (Claude hooks, pi extension) → asks the transcript can't show, and how they ended.

A collector forwards each spool record as an ingest event of kind "hook":
  {harness, session_id, name (spool file name, unique per event), event, ts, data}

  claude PermissionRequest            opens a "permission" ask: "allow Bash: rm -rf build"
  claude Notification permission_prompt opens one too, unless a PermissionRequest ask is within DEDUPE_S
  claude Notification elicitation_dialog opens an "elicitation" ask (MCP server asking for input)
  claude PostToolUse[Failure]          resolves the oldest open permission ask for that tool: "allowed"
  claude PermissionDenied              … "denied: <reason>"
  claude elicitation_complete/response resolves the oldest open elicitation ask
  claude Stop / UserPromptSubmit / SessionEnd, SessionStart(startup|resume|clear) close every open hook ask
  pi ui_prompt_start / ui_prompt_end   open / resolve a "prompt" ask; session_start / session_shutdown close all

Every event is applied once: (host, name) is recorded, so a replayed batch can't resolve a later ask.
Asks the transcript already carries (AskUserQuestion, ExitPlanMode) are skipped here.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from ctxswitch.common.schema import session_key
from ctxswitch.hub.store import Store

DEDUPE_S = 30
TRANSCRIPT_TOOLS = {"AskUserQuestion", "ExitPlanMode"}
SUMMARY_KEYS = ("command", "file_path", "path", "url", "pattern", "query", "description", "prompt")
CLOSE_TEXT = {"Stop": "closed: turn ended", "UserPromptSubmit": "closed: new prompt", "SessionEnd": "closed: session ended",
              "SessionStart": "closed: session restarted", "session_start": "closed: session restarted",
              "session_shutdown": "closed: session ended"}

SCHEMA = "CREATE TABLE IF NOT EXISTS hook_seen (host_id TEXT NOT NULL, name TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY(host_id, name));"
SEEN_DAYS = 7                                       # replays come from a collector outbox, minutes old at most


def _epoch(ts: str | None) -> float | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def summarize(tool: str, tool_input: Any) -> str:
    """"allow Bash: rm -rf build" from the most telling field of the tool input."""
    detail = ""
    if isinstance(tool_input, dict):
        for k in SUMMARY_KEYS:
            if isinstance(tool_input.get(k), str) and tool_input[k].strip():
                detail = tool_input[k]; break
        else:
            detail = next((v for v in tool_input.values() if isinstance(v, str) and v.strip()), "")
    detail = " ".join(detail.split())
    if len(detail) > 160:
        detail = detail[:159] + "…"
    return f"allow {tool}: {detail}" if detail else f"allow {tool}"


class HookApplier:
    def __init__(self, store: Store):
        self.store = store
        store.db.executescript(SCHEMA)
        self._applied = 0

    def apply(self, host: str, p: dict[str, Any]) -> bool:
        """Apply one hook event; False when it was already applied or is not usable."""
        harness, sid, name, event = p.get("harness"), p.get("session_id"), p.get("name"), p.get("event")
        if harness not in ("claude", "pi") or not all(isinstance(x, str) and x for x in (sid, name, event)):
            return False
        db = self.store.db
        with self.store._lock:
            now = time.time()
            db.execute("INSERT OR IGNORE INTO hook_seen(host_id,name,at) VALUES(?,?,?)", (host, name, now))
            if db.execute("SELECT changes()").fetchone()[0] == 0:
                return False
            key, ts, data = session_key(host, harness, sid), p.get("ts") or "", p.get("data") or {}
            (self._claude if harness == "claude" else self._pi)(key, name, event, ts, data)
            self._applied += 1
            if self._applied % 500 == 0:
                db.execute("DELETE FROM hook_seen WHERE at<?", (now - SEEN_DAYS * 86400,))
            self.store._bump()
        return True

    # -- helpers (called under the store lock) -----------------------------------------------------------------
    def _open(self, key: str, ref: str, kind: str, text: str, ts: str) -> None:
        r = self.store.db.execute("SELECT MAX(idx) FROM turns WHERE key=?", (key,)).fetchone()
        self.store.db.execute("INSERT OR IGNORE INTO asks(key,ref,kind,text,turn_idx,asked_at) VALUES(?,?,?,?,?,?)",
                              (key, ref, kind, text, r[0] if r and r[0] is not None else -1, ts))

    def _resolve(self, key: str, like: str, answer: str, ts: str) -> bool:
        r = self.store.db.execute("SELECT ref FROM asks WHERE key=? AND answered=0 AND ref LIKE ? ESCAPE '\\' ORDER BY asked_at, ref LIMIT 1",
                                  (key, like)).fetchone()
        if r is None:
            return False
        self.store.db.execute("UPDATE asks SET answered=1, answer=?, answered_at=? WHERE key=? AND ref=?", (answer, ts, key, r[0]))
        return True

    def _close_all(self, key: str, answer: str, ts: str) -> None:
        self.store.db.execute("UPDATE asks SET answered=1, answer=?, answered_at=? WHERE key=? AND answered=0 AND ref LIKE 'hook:%'",
                              (answer, ts, key))

    def _near_permission(self, key: str, ts: str, like: str) -> str | None:
        """ref of a hook permission ask (open or not) within DEDUPE_S of ts."""
        t = _epoch(ts)
        if t is None:
            return None
        for ref, asked in self.store.db.execute("SELECT ref, asked_at FROM asks WHERE key=? AND ref LIKE ?", (key, like)):
            a = _epoch(asked)
            if a is not None and abs(a - t) <= DEDUPE_S:
                return ref
        return None

    # -- harnesses ----------------------------------------------------------------------------------------------
    def _claude(self, key: str, name: str, event: str, ts: str, d: dict[str, Any]) -> None:
        tool = d.get("tool_name") if isinstance(d.get("tool_name"), str) else ""
        if event == "PermissionRequest":
            if tool in TRANSCRIPT_TOOLS:
                return
            text = summarize(tool or "tool", d.get("tool_input"))
            note = self._near_permission(key, ts, "hook:notif:%")
            if note:                                    # the Notification came first: give it the better text and tool
                self.store.db.execute("UPDATE asks SET ref=?, text=? WHERE key=? AND ref=?", (f"hook:perm:{tool}:{name}", text, key, note))
            else:
                self._open(key, f"hook:perm:{tool}:{name}", "permission", text, ts)
        elif event == "Notification":
            nt = d.get("notification_type")
            if nt == "permission_prompt":
                if not self._near_permission(key, ts, "hook:perm:%"):
                    self._open(key, f"hook:notif:{name}", "permission", str(d.get("message") or "permission needed"), ts)
            elif nt == "elicitation_dialog":
                self._open(key, f"hook:elic:{name}", "elicitation", str(d.get("message") or d.get("title") or "input requested"), ts)
            elif nt in ("elicitation_complete", "elicitation_response"):
                self._resolve(key, "hook:elic:%", "answered", ts)
        elif event in ("PostToolUse", "PostToolUseFailure", "PermissionDenied"):
            if event == "PermissionDenied":
                answer = f"denied: {d['reason']}" if d.get("reason") else "denied"
            else:
                answer = "allowed" if event == "PostToolUse" else "allowed (tool failed)"
            if not self._resolve(key, f"hook:perm:{_like(tool)}:%", answer, ts):
                self._resolve(key, "hook:notif:%", answer, ts)
        elif event in ("Stop", "UserPromptSubmit", "SessionEnd") or (
                event == "SessionStart" and d.get("source") in ("startup", "resume", "clear")):
            self._close_all(key, CLOSE_TEXT[event], ts)

    def _pi(self, key: str, name: str, event: str, ts: str, d: dict[str, Any]) -> None:
        if event == "ui_prompt_start":
            kind = d.get("kind") if isinstance(d.get("kind"), str) else "prompt"
            title = d.get("title") if isinstance(d.get("title"), str) and d["title"].strip() else ""
            self._open(key, f"hook:prompt:{name}", "prompt", f"{kind}: {title}" if title else kind, ts)
        elif event == "ui_prompt_end":
            self._resolve(key, "hook:prompt:%", "answered", ts)
        elif event in ("session_start", "session_shutdown"):
            self._close_all(key, CLOSE_TEXT[event], ts)
