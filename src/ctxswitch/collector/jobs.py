"""The saint-agent ledger: which Claude sessions were launched as pool jobs, by whom, and what they cost.

``saint-agent`` appends one JSON line per event (``$CTXSWITCH_SAINT_LEDGER`` overrides the path): a ``start`` when it
launches a child Claude, and an end line when the job finishes. A transcript that matches a line is that job, so the
hub can hang it under the session that launched it and show what the pool cost.

A job's lines are merged into one record keyed by its job id, later lines winning: the start line names the child's
session id and the parent, the end line adds the outcome. A resumed segment (``sa-…-j2``) is its own job.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

LEDGER = Path(os.environ.get("CTXSWITCH_SAINT_LEDGER") or "~/.claude/saint-agent-ledger.jsonl").expanduser()
FALLBACK_SLACK_S = 300          # an old end line without a sid: how far before the job ended a transcript may start


def read_ledger(path: Path | str = LEDGER) -> list[dict[str, Any]]:
    """The ledger's lines, in order; a line that isn't a JSON object is skipped."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _epoch(ts: Any) -> float | None:
    """Seconds since the epoch for an ISO timestamp ("…Z" or an offset); None when it isn't one."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _real(p: Any) -> str:
    return os.path.realpath(os.path.expanduser(str(p))) if p else ""


def _count(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


class Jobs:
    """Ledger lines, indexed by job id, looked up by the session they describe."""

    def __init__(self, path: Path | str | None = None):
        # with no path given, the environment decides now, so an Indexer built after one is set follows it
        self.path = Path(path) if path is not None else \
            Path(os.environ.get("CTXSWITCH_SAINT_LEDGER") or LEDGER).expanduser()
        self._mtime: float | None = None
        self.by_job: dict[str, dict[str, Any]] = {}
        self.by_sid: dict[str, dict[str, Any]] = {}
        self.ends: list[dict[str, Any]] = []      # end lines lacking a sid: match those by workdir and time
        self.reload()

    def reload(self) -> None:
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = None
        self.by_job, self.by_sid, self.ends = {}, {}, []
        for line in read_ledger(self.path):
            job = line.get("job")
            if not isinstance(job, str) or not job:
                continue
            rec = self.by_job.setdefault(job, {})
            rec.update(line)                      # a start line, then its end line once the job is over
            sid = rec.get("sid")
            if isinstance(sid, str) and sid:
                self.by_sid[sid] = rec
            if line.get("event") != "start" and line.get("sid") is None and _epoch(line.get("ts")) is not None:
                self.ends.append(rec)             # an end line from before sids were recorded

    def refresh(self) -> "Jobs":
        """Re-read the ledger when it changed since the last look (a stat per check, so the indexer can ask often)."""
        try:
            m = self.path.stat().st_mtime
        except OSError:
            m = None
        if m != self._mtime:
            self.reload()
        return self

    def for_session(self, sid: str, cwd: str = "", first_ts: str | None = None) -> dict[str, Any] | None:
        """The job a Claude session is, or None when it isn't one.

        The transcript's session id names it outright when the ledger recorded one. Older ledger lines don't, so
        fall back to the job whose end line ran in this session's directory and ended within ``dur_s`` (+ a slack)
        of the transcript's first entry.
        """
        self.refresh()
        rec = self.by_sid.get(sid) if sid else None
        if rec is None:
            rec = self._by_time(cwd, _epoch(first_ts))
        if rec is None:
            return None
        parent = rec.get("session")
        return {"job": rec.get("job", "") or "",
                "job_status": rec.get("status") or "running",
                "job_brief": os.path.basename(str(rec.get("brief") or "")),
                "job_dur_s": _count(rec.get("dur_s")), "job_turns": _count(rec.get("turns")),
                "job_tokens": rec.get("tokens") if isinstance(rec.get("tokens"), dict) else None,
                "parent_session": parent if isinstance(parent, str) and parent else None}

    def _by_time(self, cwd: str, start: float | None) -> dict[str, Any] | None:
        if start is None or not cwd:
            return None
        want, best, best_gap = _real(cwd), None, None
        for rec in self.ends:
            # a job that did get a sid is matched by that, never by a guess from the clock
            if rec.get("sid") or _real(rec.get("workdir")) != want:
                continue
            end = _epoch(rec.get("ts"))
            if end is None:
                continue
            dur = rec.get("dur_s") if isinstance(rec.get("dur_s"), int) and not isinstance(rec.get("dur_s"), bool) else 0
            if end - FALLBACK_SLACK_S - dur <= start <= end:
                gap = end - start
                if best_gap is None or gap < best_gap:
                    best, best_gap = rec, gap
        return best
