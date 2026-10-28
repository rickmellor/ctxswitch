"""Tail transcript files, feed the harness parsers, and emit session/turn events.

Discovery: <claude_root>/<cwd-slug>/<sessionId>.jsonl and <pi_root>/<cwd-slug>/<ts>_<id>.jsonl. Each file is read
from its saved offset; only complete lines are consumed, so a line being written is picked up on the next change.
Parser state is persisted alongside the offset, so a collector restart resumes mid-turn.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ctxswitch.collector.adapters import parse_json_line
from ctxswitch.collector.adapters.claude import ClaudeParser
from ctxswitch.collector.adapters.pi import PiParser
from ctxswitch.collector.gitinfo import repo_info
from ctxswitch.collector.state import CollectorState
from ctxswitch.common.config import CollectorConfig
from ctxswitch.common.schema import SessionMeta, session_key

log = logging.getLogger("ctxswitch.indexer")
Emit = Callable[[str, dict[str, Any]], None]
ENDED_AFTER_S = 15 * 60          # a non-live transcript untouched this long counts as ended


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def session_id_from_path(path: Path, harness: str) -> str:
    stem = path.stem
    if harness == "pi" and "_" in stem:
        return stem.split("_", 1)[1]
    return stem


class Indexer:
    def __init__(self, cfg: CollectorConfig, host_id: str, state: CollectorState, emit: Emit):
        self.cfg, self.host_id, self.state, self.emit = cfg, host_id, state, emit
        self.roots: list[tuple[Path, str]] = []
        for r in cfg.claude_roots:
            self.roots.append((Path(os.path.expanduser(r)), "claude"))
        for r in cfg.pi_roots:
            self.roots.append((Path(os.path.expanduser(r)), "pi"))
        for r in cfg.claude_win_roots:
            self.roots.append((Path(os.path.expanduser(r)), "claude-win"))
        self.live_keys: set[str] = set()         # updated by the liveness poller
        self._meta_sent: dict[str, dict] = {}

    # -- discovery ----------------------------------------------------------------------------------------------
    def harness_for(self, path: Path) -> str | None:
        for root, h in self.roots:
            try:
                path.relative_to(root)
                return h
            except ValueError:
                continue
        return None

    def discover(self) -> list[tuple[Path, str]]:
        cutoff = time.time() - self.cfg.backfill_days * 86400
        out = []
        for root, h in self.roots:
            if not root.is_dir():
                continue
            for p in root.glob("*/*.jsonl"):
                if "/subagents/" in str(p):
                    continue
                try:
                    if p.stat().st_mtime < cutoff and self.state.file(str(p)) is None:
                        continue
                except OSError:
                    continue
                out.append((p, h))
        return sorted(out, key=lambda t: t[0].stat().st_mtime if t[0].exists() else 0)

    def scan_all(self) -> int:
        n = 0
        for p, h in self.discover():
            self.process(p, h); n += 1
        return n

    # -- processing ---------------------------------------------------------------------------------------------
    def process(self, path: Path, harness: str | None = None) -> bool:
        """Read new bytes of one transcript. Returns True when anything was emitted."""
        harness = harness or self.harness_for(path)
        if harness is None:
            return False
        try:
            st = path.stat()
        except OSError:
            return False
        rec = self.state.file(str(path))
        offset = rec["offset"] if rec else 0
        if rec and rec["ended"] and st.st_size == rec["size"]:
            return False
        if st.st_size < offset:                      # truncated/rewritten: start over
            offset, rec = 0, None
        pcls = PiParser if harness == "pi" else ClaudeParser
        parser = pcls.from_state(rec["parser_state"], self.cfg.max_text_chars) if rec and rec["parser_state"] else pcls(self.cfg.max_text_chars)
        emitted = False
        if st.st_size > offset:
            with open(path, "rb") as f:
                f.seek(offset)
                buf = f.read()
            pos = offset
            for line in buf.split(b"\n")[:-1]:         # the tail after the last \n is incomplete
                pos += len(line) + 1
                e = parse_json_line(line)
                if e is None:
                    if line.strip(): parser.bad_lines += 1
                    continue
                parser.feed(e, pos)
            offset = pos
        key = session_key(self.host_id, harness if harness != "claude-win" else "claude-win", session_id_from_path(path, harness))
        ended = key not in self.live_keys and (time.time() - st.st_mtime) > ENDED_AFTER_S and harness != "pi" or \
                harness == "pi" and (time.time() - st.st_mtime) > ENDED_AFTER_S and key not in self.live_keys
        if ended:
            parser.finish()
        turns = parser.drain()
        meta = self._meta(parser.meta, key, path, harness, st)
        if meta != self._meta_sent.get(key):
            self.emit("session", {**meta, "ended": ended})
            self._meta_sent[key] = meta
            emitted = True
        if turns or (parser.current is not None and st.st_size != (rec["size"] if rec else -1)):
            self.emit("turns", {"key": key, "turns": [t.to_dict() for t in turns],
                                "open": parser.current.to_dict() if parser.current else None, "ended": ended})
            emitted = True
        self.state.save_file(str(path), harness, key, offset, parser.state(), st.st_size, st.st_mtime,
                             (rec["turns_emitted"] if rec else 0) + len(turns), ended)
        return emitted

    def _meta(self, pm: dict[str, Any], key: str, path: Path, harness: str, st: os.stat_result) -> dict[str, Any]:
        cwd = pm.get("cwd", "")
        root, remote = repo_info(cwd) if cwd else ("", "")
        title = pm.get("title") or pm.get("first_prompt") or ""
        m = SessionMeta(host_id=self.host_id, harness=harness, session_id=key.rsplit("/", 1)[1],
                        transcript_path=str(path), cwd=cwd, repo_root=root, repo_remote=remote,
                        git_branch=pm.get("git_branch", ""), title=title, name=pm.get("name", ""),
                        harness_version=pm.get("harness_version", ""), started_at=pm.get("started_at") or _iso(st.st_ctime),
                        last_activity=pm.get("last_activity") or _iso(st.st_mtime), model=pm.get("model", ""),
                        permission_mode=pm.get("permission_mode", ""))
        return m.to_dict()
