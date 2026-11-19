"""What the hub keeps of the samples its hosts post: the latest per host plus a short history, in memory only.

A metrics sample is a few seconds of a machine's life — it says nothing a minute later and nothing a week later, so
it has no row in SQLite and no migration. The hub is the only place a board can see every host at once, so it holds
the last 60 samples per host (60 × ``metrics_every`` = two minutes of curve) and forgets the rest. A hub restart
loses it; the next sample fills it back in.

An LLM host's sample adds the tokens per second of each of its vLLM seats and their total, kept the same way as the
CPU and GPU curves — one line per seat, by the name the collector's config gave it. A host that sends no ``llm``
gets no ``llm`` in its history either, so a board can tell "not an LLM host" from "idle".

A sample may also carry ``calls``: the agent API replies its host's transcripts finished since that host's previous
sample (Claude Code under its profile's name, Antigravity as "agy"). Those are the only numbers that say anything
about a **cloud** model, and one host's transcripts are only part of the rig, so they are kept in one pile for every
host together, and ``agent_rows`` answers what each label was doing — one row per label, across the whole rig.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

KEEP = 60                                    # samples per host in the history (~2 min at metrics_every = 2)
AGENT_WINDOW_S = 600                         # what "recent" means for a row's rates: 60 buckets of 10 s
AGENT_DUTY_S = 86400.0                       # what "recent" means for a row existing at all, and for its duty: 24 h
AGENT_CALLS_MAX = 200000                     # call records held: a day of a busy rig's replies, with room to spare
MIN_CALL_OUT = 200                           # a reply shorter than this is mostly time-to-first-token, not a rate


class Metrics:
    """``put`` from the collector's POST, ``get`` for the board. Thread-safe: the collector's POSTs land on whatever
    worker thread starlette picked, the GETs on another."""

    def __init__(self, keep: int = KEEP):
        self.keep = max(1, int(keep))
        self._lock = threading.Lock()
        self._host: dict[str, dict[str, Any]] = {}     # host_id -> {"at": time.time(), "sample": d, "hist": {...}}
        self._calls: deque[tuple[str, float, float, int]] = deque(maxlen=AGENT_CALLS_MAX)   # (agent, end, dur_s, out)
        self.started = time.time()           # a hub younger than the duty window divides duty by its own age, not 24 h

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
            llm = d.get("llm")
            if isinstance(llm, dict):        # an LLM host: one line per seat, by its configured name, plus their total
                seats = h["hist"].setdefault("llm", {})
                for s in llm.get("seats") or []:
                    if isinstance(s, dict) and isinstance(s.get("name"), str):
                        seats.setdefault(s["name"], deque(maxlen=self.keep)).append(_num(s.get("tok_s")))
                total = llm.get("total")
                h["hist"].setdefault("llm_total", deque(maxlen=self.keep)).append(
                    _num(total.get("tok_s")) if isinstance(total, dict) else None)
            for c in d.get("calls") or []:             # rig-wide, in arrival order: one pile for every host
                rec = _call(c)
                if rec is not None:
                    self._calls.append(rec)
            cutoff = time.time() - AGENT_DUTY_S
            while self._calls and self._calls[0][1] < cutoff:      # ends arrive in order, so this is the front
                self._calls.popleft()

    def get(self, host: str | None = None, now: float | None = None) -> dict[str, Any]:
        """The GET /v1/metrics body: each host's latest sample, how long ago the hub received it, and the history.
        A host that never reported isn't under "hosts" at all; ``?host=`` naming one gives an empty "hosts". The
        "agents" rows come from the whole rig and are absent once no call has arrived for a whole duty window."""
        now = time.time() if now is None else now
        with self._lock:
            names = [host] if host else sorted(self._host)
            out: dict[str, Any] = {}
            for name in names:
                h = self._host.get(name)
                if h is None:
                    continue
                hist = h["hist"]
                history = {"cpu": list(hist["cpu"]),
                           "gpus": {i: list(dq) for i, dq in sorted(hist["gpus"].items(), key=lambda x: _idx(x[0]))}}
                if "llm" in hist:            # only an LLM host has these keys at all
                    history["llm"] = {n: list(dq) for n, dq in hist["llm"].items()}
                    history["llm_total"] = list(hist["llm_total"])
                out[name] = {"latest": h["sample"], "age_s": round(max(0.0, now - h["at"]), 3), "history": history}
            body: dict[str, Any] = {"hosts": out}
            rows = agent_rows(list(self._calls), now, since=self.started)   # rig-wide: ?host= filters hosts, never these
            if rows is not None:
                body["agents"] = rows
            return body


def agent_rows(calls: list[tuple[str, float, float, int]], now: float, window_s: float = AGENT_WINDOW_S,
               recent_s: float = 60.0, bucket_s: float = 10.0, buckets: int = 60,
               duty_s: float = AGENT_DUTY_S, since: float | None = None) -> dict[str, Any] | None:
    """One row per agent label from ``(agent, end_epoch, dur_s, out)`` tuples: what that account was delivering the
    rig, as the cloud models' own transcripts say. A label gets a row for any call that ended in the last ``duty_s``
    seconds — a day — so an account that answered this morning is still on the board this evening, reading 0 tok/s.
    None only when no label has one: a board with no "agents" is one where no agent has run in a day.

    A reply's tokens are treated as spread evenly over the seconds it was streaming, ``[end − dur_s, end]``, which is
    what makes a rate out of a reply that arrived between two samples: ``tok_s`` is the part of that spread falling in
    the last ``recent_s`` seconds, and ``history`` the same proration per bucket. min/avg/max and ``calls`` are over
    the replies that ended in the last ``window_s`` — 10 minutes of what the account is doing now — over replies of
    at least ``MIN_CALL_OUT`` tokens (a short reply's seconds are mostly time-to-first-token), with ``avg``
    time-weighted so a long fast reply isn't diluted by a brief one. ``duty`` is the share of the last ``duty_s``
    covered by the label's reply spans as one union, so two agents answering at once are not counted twice; the
    window starts at ``max(now − duty_s, since)`` when the hub knows how young it is, so a hub that came up an hour
    ago divides by an hour rather than reporting a quiet 24-hour account.
    """
    lo = now - duty_s if since is None else max(now - duty_s, since)
    span = now - lo                                # what the day's duty divides by: a hub that just came up has none
    by: defaultdict[str, list[tuple[float, float, int]]] = defaultdict(list)   # label -> calls ending inside the day
    for a, e, d, o in calls:
        if e >= lo:
            by[a].append((e, d, o))
    if not by:
        return None
    base = now - buckets * bucket_s
    rows = []
    for name in sorted(by):
        grp = by[name]
        short = [c for c in grp if c[0] >= now - window_s]
        recent = sum(o * _overlap(e - d, e, now - recent_s, now) / d for e, d, o in short)
        long_ = [(d, o) for e, d, o in short if o >= MIN_CALL_OUT]
        rates = [o / d for d, o in long_]
        hist = [0.0] * buckets
        for e, d, o in short:                      # each reply's tokens spread into the buckets it crossed
            b_lo, hi = min(max(e - d, base), now), min(e, now)
            rate, i = o / d, max(0, int((b_lo - base) // bucket_s))
            while i < buckets and base + i * bucket_s < hi:
                b0 = base + i * bucket_s
                hist[i] += rate * _overlap(b_lo, hi, b0, b0 + bucket_s) / bucket_s
                i += 1
        rows.append({"name": name, "tok_s": _r1(recent / recent_s),
                     "min": _r1(min(rates) if rates else None),
                     "avg": _r1((sum(o for _, o in long_) / sum(d for d, _ in long_)) if long_ else None),
                     "max": _r1(max(rates) if rates else None),
                     "duty": _r1(100.0 * _union([(e - d, e) for e, d, _ in grp], lo, now) / max(span, 1e-9)),
                     "calls": len(short), "history": [round(h, 1) for h in hist]})
    return {"window_s": window_s, "duty_window_s": duty_s, "rows": rows}


def _overlap(lo: float, hi: float, a: float, b: float) -> float:
    """Seconds of ``[lo, hi]`` that fall inside ``[a, b]``."""
    return max(0.0, min(hi, b) - max(lo, a))


def _union(ivs: list[tuple[float, float]], a: float, b: float) -> float:
    """Seconds covered by the union of the intervals clipped to ``[a, b]`` — overlapping ones counted once."""
    total, cur_end = 0.0, None
    clipped = sorted(((max(lo, a), min(hi, b)) for lo, hi in ivs), key=lambda t: t[0])
    for lo, hi in clipped:
        if hi <= lo:
            continue
        if cur_end is None or lo > cur_end:
            total, cur_end = total + (hi - lo), hi
        elif hi > cur_end:
            total, cur_end = total + (hi - cur_end), hi
    return total


def _call(c: Any) -> tuple[str, float, float, int] | None:
    """A posted call record as the row-builder's tuple, None when it isn't one (a malformed sample says nothing)."""
    if not isinstance(c, dict) or not isinstance(c.get("agent"), str) or not c["agent"]:
        return None
    end = _epoch(c.get("end"))
    dur, out = _num(c.get("dur_s")), c.get("out")
    if end is None or dur is None or dur <= 0 or not isinstance(out, int) or isinstance(out, bool) or out <= 0:
        return None
    return c["agent"], end, dur, out


def _epoch(ts: Any) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _r1(v: float | None) -> float | None:
    return round(v, 1) if v is not None else None


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _idx(s: str) -> tuple[int, str]:
    """GPU history keys sorted as numbers, so "10" doesn't come before "2"."""
    return (int(s), "") if s.lstrip("-").isdigit() else (1 << 30, s)
