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
from ctxswitch.collector.adapters.shell import ShellParser
from ctxswitch.collector.adapters import agy as agy_store
from ctxswitch.collector.adapters.agy import AgyParser
from ctxswitch.collector.gitinfo import branch, repo_info
from ctxswitch.collector.jobs import Jobs
from ctxswitch.common.wsl import wsl_path
from ctxswitch.collector.state import CollectorState
from ctxswitch.common.config import CollectorConfig
from ctxswitch.common.schema import SessionMeta, session_key

log = logging.getLogger("ctxswitch.indexer")
Emit = Callable[[str, dict[str, Any]], None]
ENDED_AFTER_S = 15 * 60          # a non-live transcript untouched this long counts as ended


def _mtime(p: Path, harness: str) -> float:
    """When the transcript last changed: for an agy conversation, the newer of the db and its WAL."""
    m = p.stat().st_mtime
    if harness == "agy":
        wal = p.with_name(p.name + "-wal")
        if wal.exists():
            m = max(m, wal.stat().st_mtime)
    return m


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def session_id_from_path(path: Path, harness: str) -> str:
    stem = path.stem
    if harness == "pi" and "_" in stem:
        return stem.split("_", 1)[1]
    return stem                                             # a shell transcript's stem is already its uuid


class Indexer:
    def __init__(self, cfg: CollectorConfig, host_id: str, state: CollectorState, emit: Emit, profiles: list | None = None):
        self.cfg, self.host_id, self.state, self.emit = cfg, host_id, state, emit
        self.profiles = profiles or []                  # this host's [[claude_profiles]]: names the account in meta
        self.roots: list[tuple[Path, str]] = []
        for r in cfg.claude_roots:
            self.roots.append((Path(os.path.expanduser(r)), "claude"))
        for r in cfg.pi_roots:
            self.roots.append((Path(os.path.expanduser(r)), "pi"))
        for r in cfg.claude_win_roots:
            self.roots.append((Path(os.path.expanduser(r)), "claude-win"))
        for r in cfg.agy_roots:
            self.roots.append((Path(os.path.expanduser(r)), "agy"))
        for r in cfg.shell_roots:
            self.roots.append((Path(os.path.expanduser(r)), "shell"))
        self.live_keys: set[str] = set()         # updated by the liveness poller
        self._meta_sent: dict[str, dict] = {}
        self.jobs = Jobs()                       # saint-agent pool jobs: which Claude sessions are one (collector/jobs.py)

    # -- discovery ----------------------------------------------------------------------------------------------
    def harness_for(self, path: Path) -> str | None:
        for root, h in self.roots:
            try:
                path.relative_to(root)
                return h
            except ValueError:
                continue
        return None

    @staticmethod
    def _glob(h: str) -> str:
        # shell transcripts sit directly in their root (one file per shell), the others one slug directory down
        return "*.db" if h == "agy" else "*.jsonl" if h == "shell" else "*/*.jsonl"

    def discover(self) -> list[tuple[Path, str]]:
        cutoff = time.time() - self.cfg.backfill_days * 86400
        out = []
        for root, h in self.roots:
            if not root.is_dir():
                continue
            for p in root.glob(self._glob(h)):
                if "/subagents/" in str(p):
                    continue
                try:
                    if _mtime(p, h) < cutoff and self.state.file(str(p)) is None:
                        continue
                except OSError:
                    continue
                out.append((p, h))
        return sorted(out, key=lambda t: t[0].stat().st_mtime if t[0].exists() else 0)

    def poll(self, harness: str) -> int:
        """Process the transcripts of one harness whose size or mtime changed (for roots no watcher sees: inotify
        does not fire on /mnt/c for writes made by Windows processes)."""
        n = 0
        for root, h in self.roots:
            if h != harness or not root.is_dir():
                continue
            for p in root.glob(self._glob(h)):
                try:
                    st = p.stat()
                    mtime = _mtime(p, h)
                except OSError:
                    continue
                rec = self.state.file(str(p))
                if rec and rec["mtime"] == mtime and (h == "agy" or rec["size"] == st.st_size):
                    continue
                if rec is None and mtime < time.time() - self.cfg.backfill_days * 86400:
                    continue
                self.process(p, h); n += 1
        return n

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
        if harness == "agy":
            return self._process_agy(path)
        try:
            st = path.stat()
        except OSError:
            return False
        rec = self.state.file(str(path))
        offset = rec["offset"] if rec else 0
        if st.st_size < offset:                      # truncated/rewritten: start over
            offset, rec = 0, None
        # a shell's command output lives beside its transcript, in the same stem with a .out suffix
        pcls = ShellParser if harness == "shell" else PiParser if harness == "pi" else ClaudeParser
        args = (path.with_suffix(".out"),) if harness == "shell" else ()
        parser = pcls.from_state(rec["parser_state"], self.cfg.max_text_chars, *args) if rec and rec["parser_state"] \
            else pcls(self.cfg.max_text_chars, *args)
        if rec and rec["ended"] and st.st_size == rec["size"]:
            # ended and unchanged: nothing to parse, but once per collector run resend its meta (no turns, no reset),
            # so what this collector derives from its config (the account/profile name) reaches the hub for old
            # sessions too
            key = session_key(self.host_id, harness if harness != "claude-win" else "claude-win", session_id_from_path(path, harness))
            if key in self._meta_sent or not rec["parser_state"]:
                return False
            meta = self._meta(parser.meta, key, path, harness, st)
            self.emit("session", {**meta, "ended": True, "reset": False})
            self._meta_sent[key] = meta
            return True
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
        # a live session never ends (pi and shell say so through their registry, the others through the poller's
        # live_keys); otherwise 15 minutes of silence on the transcript is the answer
        ended = key not in self.live_keys and (time.time() - st.st_mtime) > ENDED_AFTER_S
        if ended:
            parser.finish()
        turns = parser.drain()
        meta = self._meta(parser.meta, key, path, harness, st)
        if meta != self._meta_sent.get(key) or rec is None:
            # rec None = this collector state has never seen the file: a fresh index. Ask the hub to drop the turns it
            # holds for the session so rows from an older parser version cannot linger.
            self.emit("session", {**meta, "ended": ended, "reset": rec is None})
            self._meta_sent[key] = meta
            emitted = True
        if turns or (parser.current is not None and st.st_size != (rec["size"] if rec else -1)):
            self.emit("turns", {"key": key, "turns": [t.to_dict() for t in turns],
                                "open": parser.current.to_dict() if parser.current else None, "ended": ended})
            emitted = True
        self.state.save_file(str(path), harness, key, offset, parser.state(), st.st_size, st.st_mtime,
                             (rec["turns_emitted"] if rec else 0) + len(turns), ended)
        return emitted

    def _process_agy(self, path: Path) -> bool:
        """An Antigravity conversation: the step index is the offset, the step count the size, and the newer of the
        db and its WAL the mtime. Title and cwd come from the summaries db beside the conversations."""
        try:
            st = path.stat()
            mtime = _mtime(path, "agy")
        except OSError:
            return False
        rec = self.state.file(str(path))
        offset = rec["offset"] if rec else 0
        total = agy_store.step_count(str(path))
        if total is None:
            return False
        if total < offset:                               # rewritten: start over
            offset, rec = 0, None
        parser = AgyParser.from_state(rec["parser_state"], self.cfg.max_text_chars) if rec and rec["parser_state"] else AgyParser(self.cfg.max_text_chars)
        sid = path.stem
        key = session_key(self.host_id, "agy", sid)
        summ = agy_store.read_summary(str(path.parent.parent / "conversation_summaries.db"), sid)
        if rec and rec["ended"] and total == offset and rec["mtime"] == mtime:
            if key in self._meta_sent or not rec["parser_state"]:
                return False
            meta = self._meta(parser.meta | self._agy_meta(summ, sid), key, path, "agy", st)
            self.emit("session", {**meta, "ended": True, "reset": False})
            self._meta_sent[key] = meta
            return True
        rows = agy_store.read_steps(str(path), offset) if total > offset else []
        for row in rows:
            parser.feed(row, row["idx"] + 1)
        offset = (rows[-1]["idx"] + 1) if rows else offset
        live = key in self.live_keys
        ended = not live and (time.time() - mtime) > ENDED_AFTER_S
        if ended:
            parser.finish()
        elif not live and parser.current is not None and not summ.get("busy") and summ.get("status", "").endswith("IDLE"):
            parser.finish()                              # agy says the turn is over (idle); the next prompt opens a new one
        turns = parser.drain()
        meta = self._meta(parser.meta | self._agy_meta(summ, sid), key, path, "agy", st)
        emitted = False
        if meta != self._meta_sent.get(key) or rec is None:
            self.emit("session", {**meta, "ended": ended, "reset": rec is None})
            self._meta_sent[key] = meta
            emitted = True
        if turns or (parser.current is not None and rows):
            self.emit("turns", {"key": key, "turns": [t.to_dict() for t in turns],
                                "open": parser.current.to_dict() if parser.current else None, "ended": ended})
            emitted = True
        self.state.save_file(str(path), "agy", key, offset, parser.state(), total, mtime,
                             (rec["turns_emitted"] if rec else 0) + len(turns), ended)
        return emitted

    @staticmethod
    def _agy_meta(summ: dict[str, Any], sid: str) -> dict[str, Any]:
        out: dict[str, Any] = {"session_id": sid}
        if summ.get("title"):
            out["title"] = summ["title"]
        if summ.get("cwd"):
            out["cwd"] = summ["cwd"]
        return out

    def _meta(self, pm: dict[str, Any], key: str, path: Path, harness: str, st: os.stat_result) -> dict[str, Any]:
        cwd = pm.get("cwd", "")
        local = wsl_path(cwd) if harness == "claude-win" else cwd     # C:\x -> /mnt/c/x, so git can be asked
        root, remote = repo_info(local) if cwd else ("", "")
        title = pm.get("title") or pm.get("first_prompt") or ""
        m = SessionMeta(host_id=self.host_id, harness=harness, session_id=key.rsplit("/", 1)[1],
                        transcript_path=str(path), cwd=cwd, repo_root=root, repo_remote=remote,
                        # pi records no branch: read the checkout's HEAD
                        git_branch=pm.get("git_branch") or (branch(root) if root else ""), title=title, name=pm.get("name", ""),
                        harness_version=pm.get("harness_version", ""), started_at=pm.get("started_at") or _iso(st.st_ctime),
                        last_activity=pm.get("last_activity") or _iso(st.st_mtime), model=pm.get("model", ""),
                        permission_mode=pm.get("permission_mode", ""), profile=self._profile(harness, path))
        if harness == "claude":                    # a session saint-agent launched hangs under its parent, with its job
            job = self.jobs.for_session(m.session_id, cwd, pm.get("started_at"))
            if job:
                parent = job["parent_session"]
                m.parent_key = session_key(self.host_id, "claude", parent) if parent else ""
                m.job, m.job_status, m.job_brief = job["job"], job["job_status"], job["job_brief"]
                m.job_dur_s, m.job_turns, m.job_tokens = job["job_dur_s"], job["job_turns"], job["job_tokens"]
        return m.to_dict()

    def _profile(self, harness: str, path: Path) -> str:
        """The account a Claude transcript belongs to, by this host's own profiles (a viewer elsewhere can't tell)."""
        from ctxswitch.common.config import profile_for, profiles_matter
        if harness != "claude" or not profiles_matter(self.profiles):
            return ""
        return profile_for({"meta": {"transcript_path": str(path)}}, self.profiles).name
