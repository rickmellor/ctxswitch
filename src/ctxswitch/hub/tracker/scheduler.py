"""One loop, one seat slot: recaps first (they track what is happening now), then the ledger extractor works
through its backlog one exchange per tick. While there is a backlog and no recap is due, ticks don't sleep."""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("ctxswitch.tracker")


async def run(recapper, extractor, interval: float = 2.0) -> None:
    log.info("tracker on %s (%s), last %d days", recapper.seat.url, recapper.seat.model, recapper.days)
    while True:
        busy = 0
        try:
            await recapper.step()
            if extractor is not None:
                busy = await extractor.step()
        except Exception:                                   # noqa: BLE001
            log.exception("tracker loop")
        await asyncio.sleep(0.05 if busy else interval)
