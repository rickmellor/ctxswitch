"""ctx-hub entry point: serve the API over the configured bind/port."""
from __future__ import annotations

import argparse
import logging

import uvicorn

from ctxswitch.common.config import data_dir, load
from ctxswitch.hub.api import Hub
from ctxswitch.hub.store import Store


def main() -> None:
    ap = argparse.ArgumentParser(prog="ctx-hub")
    ap.add_argument("--bind"); ap.add_argument("--port", type=int); ap.add_argument("--db"); ap.add_argument("--token")
    ap.add_argument("--no-recap", action="store_true", help="don't run the recap tracker")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    cfg = load()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)       # one INFO line per seat call otherwise
    db = a.db or cfg.hub.db_path or str(data_dir() / "hub.db")
    hub = Hub(Store(db), a.token if a.token is not None else cfg.hub.token)
    if cfg.hub.recap and not a.no_recap and cfg.seats:
        from ctxswitch.hub.tracker.recap import Recapper
        seat = sorted(cfg.seats, key=lambda x: x.priority)[0]
        hub.background.append(Recapper(hub.store, seat, notify=hub._notify, days=cfg.hub.recap_days).run)
    logging.getLogger("ctxswitch.hub").info("db=%s bind=%s:%s auth=%s", db, a.bind or cfg.hub.bind, a.port or cfg.hub.port, bool(hub.token))
    uvicorn.run(hub.app(), host=a.bind or cfg.hub.bind, port=a.port or cfg.hub.port, log_level="warning")


if __name__ == "__main__":
    main()
