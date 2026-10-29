"""ctx-collector entry point: index transcripts, poll liveness, push to the hub (outbound only).

Loop: initial scan -> watch transcript roots (watchfiles) + liveness poll every 5 s -> every event lands in the
local outbox -> a pusher drains the outbox to POST /v1/ingest with retry/backoff. The hub being down just grows
the outbox.
Messages: an inbox loop long-polls the hub for messages queued to this host's live sessions and writes each to the
session's own socket (collector/deliver.py), but only a session this collector's registry read says is live: the
hub names what is waiting, never where to write. Off with ``[collector] accept_messages = false``.
Windows-native Claude roots (read from WSL under /mnt/c) are polled every WIN_POLL_S instead: inotify does not fire
for writes made by Windows processes, and tasklist.exe (their liveness) costs ~1.5 s a call.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import time
from pathlib import Path
from typing import Any

import httpx
from watchfiles import Change, awatch

from ctxswitch.collector.deliver import deliver
from ctxswitch.collector.indexer import Indexer
from ctxswitch.collector import spool
from ctxswitch.collector.liveness import read_claude_registry, read_claude_win_registry, read_pi_registry, runtime_dir
from ctxswitch.common.wsl import windows_pids
from ctxswitch.collector.state import CollectorState
from ctxswitch.common.config import Config, data_dir, load

log = logging.getLogger("ctxswitch.collector")
WIN_POLL_S = 30


class Collector:
    def __init__(self, cfg: Config, once: bool = False):
        self.cfg, self.once = cfg, once
        self.host_id = cfg.host_id
        self.state = CollectorState(cfg.collector.queue_path or data_dir() / "collector-queue.db")
        self.ix = Indexer(cfg.collector, self.host_id, self.state, self._emit)
        self._dirty = asyncio.Event()
        self._last_liveness: str | None = None
        self.spool = spool.spool_dir(cfg.collector.spool_dir)
        self.win_live: dict = {}                       # last Windows registry read (refreshed every WIN_POLL_S)
        self.live: dict = {}                           # key -> Liveness from the last registry read
        base = cfg.hub.url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {cfg.hub.token}"} if cfg.hub.token else {}
        self.urls = {"ingest": base + "/v1/ingest", "outbox": base + "/v1/outbox", "messages": base + "/v1/messages"}

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        self.state.enqueue(kind, payload)
        self._dirty.set()

    # -- liveness -------------------------------------------------------------------------------------------------
    def poll_liveness(self) -> None:
        live = read_claude_registry(Path(os.path.expanduser(self.cfg.collector.claude_sessions_dir)), self.host_id)
        live |= read_pi_registry(runtime_dir(), self.host_id)
        live |= self.win_live
        self.live = live
        self.ix.live_keys = set(live)
        snap = {"host_id": self.host_id, "sessions": [l.to_dict() for l in live.values()]}
        fp = json.dumps(snap, sort_keys=True)
        if fp != self._last_liveness:
            self._last_liveness = fp
            self._emit("liveness", snap)

    def win_sessions_dirs(self) -> list[Path]:
        """<windows home>/.claude/sessions for each <windows home>/.claude/projects root."""
        return [r.parent / "sessions" for r, h in self.ix.roots if h == "claude-win"]

    def read_windows_liveness(self) -> dict | None:
        """Blocking (tasklist.exe) but touches no collector state, so it can run in a thread. None = unknown."""
        live: dict = {}
        for d in self.win_sessions_dirs():
            got = read_claude_win_registry(d, self.host_id, windows_pids)
            if got is None:
                return None
            live |= got
        return live

    def poll_windows(self, live: dict | None) -> None:
        if live is not None:                           # interop unavailable: keep the last answer rather than end them all
            self.win_live = live
        self.poll_liveness()                           # first: poll() decides "ended" from live_keys
        self.ix.poll("claude-win")

    def drain_spool(self) -> int:
        n = spool.drain(self.spool, self._emit)
        if n:
            self.poll_liveness()                       # a hook event usually means a status change too
        return n

    # -- pusher ---------------------------------------------------------------------------------------------------
    async def push_loop(self) -> None:
        url, headers = self.urls["ingest"], self.headers
        info = {"collector": "0.0.1", "platform": platform.platform(), "python": platform.python_version()}
        backoff = 1.0
        async with httpx.AsyncClient(timeout=20) as client:
            while True:
                batch = self.state.pending(200)
                if not batch:
                    if self.once:
                        return
                    self._dirty.clear()
                    try:
                        await asyncio.wait_for(self._dirty.wait(), timeout=30)
                    except asyncio.TimeoutError:
                        pass
                    continue
                body = {"host_id": self.host_id, "info": info,
                        "events": [{"seq": s, "kind": k, "payload": p} for s, k, p in batch]}
                try:
                    r = await client.post(url, json=body, headers=headers)
                    r.raise_for_status()
                    acked = r.json().get("acked")
                    if acked is not None:
                        self.state.ack(int(acked))
                    backoff = 1.0
                    log.debug("pushed %d events (pending %d)", len(batch), self.state.pending_count())
                except (httpx.HTTPError, ValueError) as e:
                    log.warning("hub unreachable (%s); %d events queued, retry in %.0fs", e.__class__.__name__, self.state.pending_count(), backoff)
                    if self.once:
                        return
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)

    # -- messages -------------------------------------------------------------------------------------------------
    async def deliver_one(self, client: httpx.AsyncClient, m: dict[str, Any]) -> bool:
        """Deliver one queued message (at most once) and report it. False = not live here yet, or the report failed."""
        mid, key = int(m["id"]), m["key"]
        live = self.live.get(key)
        harness = key.split("/", 2)[1]
        if live is None or not live.socket or harness not in ("claude", "pi"):
            return False
        done = self.state.get(f"msg:{mid}")
        if done is None:                               # never write the same message twice, even if the report is lost
            err = await deliver(live.socket, harness, key.split("/", 2)[2], m["body"], mid)
            done = err or ""
            self.state.set(f"msg:{mid}", done)
            log.info("message %d -> %s: %s", mid, key, done or "sent")
        try:
            r = await client.post(f"{self.urls['messages']}/{mid}/status", headers=self.headers,
                                  json={"host_id": self.host_id, "status": "failed" if done else "sent", "error": done or None})
            return r.status_code in (200, 409)         # 409: already reported
        except httpx.HTTPError:
            return False

    async def inbox_loop(self) -> None:
        if not self.cfg.collector.accept_messages:
            return
        async with httpx.AsyncClient(timeout=40) as client:
            while True:
                try:
                    r = await client.get(self.urls["outbox"], params={"host": self.host_id, "wait": 25}, headers=self.headers)
                    r.raise_for_status()
                    msgs = r.json().get("messages") or []
                except (httpx.HTTPError, ValueError):
                    await asyncio.sleep(10); continue
                done = 0
                for m in msgs:
                    try:
                        done += await self.deliver_one(client, m)
                    except Exception:                  # noqa: BLE001
                        log.exception("message %s", m.get("id"))
                if msgs and not done:                  # the hub thinks it's live; our registry doesn't (yet)
                    await asyncio.sleep(5)

    # -- watchers -------------------------------------------------------------------------------------------------
    async def watch_loop(self) -> None:
        roots = [str(r) for r, h in self.ix.roots if r.is_dir() and h != "claude-win"]
        if not roots:
            log.warning("no transcript roots exist: %s", [str(r) for r, _ in self.ix.roots]); return
        async for changes in awatch(*roots, step=300):
            for change, path in changes:
                if change == Change.deleted or not path.endswith(".jsonl") or "/subagents/" in path:
                    continue
                try:
                    self.ix.process(Path(path))
                except Exception:                      # noqa: BLE001
                    log.exception("processing %s", path)

    async def spool_loop(self) -> None:
        """Hook events matter most when they are fresh (a permission prompt is waiting), so watch the spool."""
        self.spool.mkdir(parents=True, exist_ok=True)
        async for _ in awatch(str(self.spool), step=100, recursive=False):
            try:
                self.drain_spool()
            except Exception:                          # noqa: BLE001
                log.exception("spool drain")

    async def liveness_loop(self) -> None:
        beat = time.time()
        while True:
            try:
                if time.time() - beat >= 60:          # heartbeat: an unchanged snapshot still refreshes last_seen
                    beat, self._last_liveness = time.time(), None
                self.poll_liveness()
                self.drain_spool()                     # backstop for a missed watch event
            except Exception:                          # noqa: BLE001
                log.exception("liveness poll")
            await asyncio.sleep(5)

    async def windows_loop(self) -> None:
        if not self.win_sessions_dirs():
            return
        while True:
            try:
                self.poll_windows(await asyncio.to_thread(self.read_windows_liveness))
            except Exception:                          # noqa: BLE001
                log.exception("windows poll")
            await asyncio.sleep(WIN_POLL_S)

    async def sweep_loop(self) -> None:
        """Re-process everything every 10 min so sessions age into 'ended' without a file change."""
        while True:
            await asyncio.sleep(600)
            try:
                self.ix.scan_all()
            except Exception:                          # noqa: BLE001
                log.exception("sweep")

    async def run(self) -> None:
        t0 = time.time()
        self.poll_liveness()
        n = self.ix.scan_all()
        self.drain_spool()
        log.info("initial scan: %d transcripts in %.1fs, %d events queued, %d live", n, time.time() - t0,
                 self.state.pending_count(), len(self.ix.live_keys))
        if self.once:
            await self.push_loop(); return
        await asyncio.gather(self.push_loop(), self.watch_loop(), self.liveness_loop(), self.sweep_loop(),
                             self.spool_loop(), self.windows_loop(), self.inbox_loop())


def main() -> None:
    ap = argparse.ArgumentParser(prog="ctx-collector")
    ap.add_argument("--once", action="store_true", help="scan, push, exit")
    ap.add_argument("--hub", help="override hub url")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    cfg = load()
    if a.hub:
        cfg.hub.url = a.hub
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)       # one INFO line per push otherwise
    asyncio.run(Collector(cfg, once=a.once).run())


if __name__ == "__main__":
    main()
