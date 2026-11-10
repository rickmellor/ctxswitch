"""What the hub keeps of the samples its hosts post: the latest per host plus a short history, in memory only.

A metrics sample is a few seconds of a machine's life — it says nothing a minute later and nothing a week later, so
it has no row in SQLite and no migration. The hub is the only place a board can see every host at once, so it holds
the last 60 samples per host (60 × ``metrics_every`` = two minutes of curve) and forgets the rest. A hub restart
loses it; the next sample fills it back in.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

KEEP = 60                                    # samples per host in the history (~2 min at metrics_every = 2)


class Metrics:
    """``put`` from the collector's POST, ``get`` for the board. Thread-safe: the collector's POSTs land on whatever
    worker thread starlette picked, the GETs on another."""

    def __init__(self, keep: int = KEEP):
        self.keep = max(1, int(keep))
        self._lock = threading.Lock()
        self._host: dict[str, dict[str, Any]] = {}     # host_id -> {"at": time.time(), "sample": d, "hist": {...}}

    def put(self, d: dict[str, Any]) -> None:
        """One sample as the hub received it. A GPU that stops appearing simply stops getting samples: its last
        history line is kept until the host disappears, so a card that goes quiet doesn't erase its curve."""
        host = d.get("host_id")
        if not isinstance(host, str) or not host:
            return
        with self._lock:
            h = self._host.get(host)
            if h is None:
                h = self._host[host] = {"hist": {"cpu": deque(maxlen=self.keep), "gpus": {}}}
            h["at"], h["sample"] = time.time(), d
            cpu = d.get("cpu")
            h["hist"]["cpu"].append(_num((cpu or {}).get("pct")))
            gpus = h["hist"]["gpus"]
            for g in d.get("gpus") or []:
                if not isinstance(g, dict):
                    continue
                i = str(g.get("index"))
                if i not in gpus:
                    gpus[i] = deque(maxlen=self.keep)
                gpus[i].append(_num(g.get("util")))

    def get(self, host: str | None = None, now: float | None = None) -> dict[str, Any]:
        """The GET /v1/metrics body: each host's latest sample, how long ago the hub received it, and the history.
        A host that never reported isn't under "hosts" at all; ``?host=`` naming one gives an empty "hosts"."""
        now = time.time() if now is None else now
        with self._lock:
            names = [host] if host else sorted(self._host)
            out: dict[str, Any] = {}
            for name in names:
                h = self._host.get(name)
                if h is None:
                    continue
                hist = h["hist"]
                out[name] = {"latest": h["sample"], "age_s": round(max(0.0, now - h["at"]), 3),
                             "history": {"cpu": list(hist["cpu"]),
                                         "gpus": {i: list(dq) for i, dq in sorted(hist["gpus"].items(), key=lambda x: _idx(x[0]))}}}
            return {"hosts": out}


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _idx(s: str) -> tuple[int, str]:
    """GPU history keys sorted as numbers, so "10" doesn't come before "2"."""
    return (int(s), "") if s.lstrip("-").isdigit() else (1 << 30, s)
