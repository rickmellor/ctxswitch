"""ctx-collector entry point: index transcripts, poll liveness, push to the hub (outbound only).

Loop: initial scan -> watch transcript roots (watchfiles) + liveness poll every 5 s -> every event lands in the
local outbox -> a pusher drains the outbox to POST /v1/ingest with retry/backoff. The hub being down just grows
the outbox.
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

from ctxswitch.collector.indexer import Indexer
from ctxswitch.collector.liveness import read_claude_registry, read_pi_registry
from ctxswitch.collector.state import CollectorState
from ctxswitch.common.config import Config, data_dir, load

log = logging.getLogger("ctxswitch.collector")


class Collector:
    def __init__(self, cfg: Config, once: bool = False):
        self.cfg, self.once = cfg, once
        self.host_id = cfg.host_id
        self.state = CollectorState(cfg.collector.queue_path or data_dir() / "collector-queue.db")
        self.ix = Indexer(cfg.collector, self.host_id, self.state, self._emit)
        self._dirty = asyncio.Event()
        self._last_liveness: str | None = None

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        self.state.enqueue(kind, payload)
        self._dirty.set()

    # -- liveness -------------------------------------------------------------------------------------------------
    def poll_liveness(self) -> None:
        live = read_claude_registry(Path(os.path.expanduser(self.cfg.collector.claude_sessions_dir)), self.host_id)
        live |= read_pi_registry(Path(os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000")), self.host_id)
        self.ix.live_keys = set(live)
        snap = {"host_id": self.host_id, "sessions": [l.to_dict() for l in live.values()]}
        fp = json.dumps(snap, sort_keys=True)
        if fp != self._last_liveness:
            self._last_liveness = fp
            self._emit("liveness", snap)

    # -- pusher ---------------------------------------------------------------------------------------------------
    async def push_loop(self) -> None:
        url = self.cfg.hub.url.rstrip("/") + "/v1/ingest"
        headers = {"Authorization": f"Bearer {self.cfg.hub.token}"} if self.cfg.hub.token else {}
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

    # -- watchers -------------------------------------------------------------------------------------------------
    async def watch_loop(self) -> None:
        roots = [str(r) for r, _ in self.ix.roots if r.is_dir()]
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

    async def liveness_loop(self) -> None:
        while True:
            try:
                self.poll_liveness()
            except Exception:                          # noqa: BLE001
                log.exception("liveness poll")
            await asyncio.sleep(5)

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
        log.info("initial scan: %d transcripts in %.1fs, %d events queued, %d live", n, time.time() - t0,
                 self.state.pending_count(), len(self.ix.live_keys))
        if self.once:
            await self.push_loop(); return
        await asyncio.gather(self.push_loop(), self.watch_loop(), self.liveness_loop(), self.sweep_loop())


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
    asyncio.run(Collector(cfg, once=a.once).run())


if __name__ == "__main__":
    main()
