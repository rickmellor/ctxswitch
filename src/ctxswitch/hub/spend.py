"""What the sessions under this hub cost: cloud turns priced per token type, and SAINT's local energy.

One vocabulary for both halves. A **turn** carries ``usage`` ({"input","output","cache_read","cache_creation"}, all
four ints, or None when the harness records nothing: shell, agy), ``model`` and, from pi, ``cost_usd`` — pi prices
cloud providers itself, so that number wins over the table. A model with no rate contributes tokens and never $0, and
says so (``unpriced``), because a silent 0 reads as "free".

``RATES`` are USD per million tokens, Bedrock on-demand as billed here (read from ccwb 2026-10-30) — an estimate, not
the invoice; ``bedrock-spend`` has the bill. Keys are canonical model names, matched by longest prefix, so
"claude-haiku-4-5-20251001" finds "claude-haiku-4-5". ``[hub.prices]`` in config.toml overrides or extends the table
and flips :func:`spend_report`'s ``rates`` to "config".

Local work runs on the SAINT box: :class:`SaintUsage` reads its ``/usage/v1`` endpoint (energy, not tokens per type —
which is why "if this had run in the cloud" is priced from the window's token totals with one chosen model's rates).
Every fetch has a 5 s timeout and a 60 s cache; an unreachable SAINT costs the report an energy number, nothing more.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

TOKEN_TYPES = ("input", "output", "cache_read", "cache_creation")

# USD per 1M tokens, Bedrock on-demand (ccwb, 2026-10-30). Longest-prefix match, so a dated version finds its family.
RATES: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.4, "output": 22.0, "cache_creation": 5.5, "cache_read": 0.22},
    "claude-opus-5": {"input": 5.5, "output": 27.5, "cache_creation": 6.875, "cache_read": 0.55},
    "claude-sonnet-5": {"input": 2.2, "output": 11.0, "cache_creation": 2.75, "cache_read": 0.22},
    "claude-fable-5-1": {"input": 11.0, "output": 55.0, "cache_creation": 13.75, "cache_read": 0.275},
    "claude-haiku-4-5": {"input": 1.1, "output": 5.5, "cache_creation": 1.375, "cache_read": 0.11},
    "claude-sonnet-4-6": {"input": 3.3, "output": 16.5, "cache_creation": 4.125, "cache_read": 0.33},
}

_REGIONS = ("apac.", "global.", "eu.", "us.")        # Bedrock / Vertex style routing prefixes
_CLOUD = ("anthropic.", "anthropic/", "bedrock/", "bedrock.")   # ours: stripped, because the model behind it is a Claude
_LOCAL_PROVIDERS = ("saint", "local", "qwen")        # a "provider/model" naming a machine, not a cloud account


def norm_model(s: str) -> str:
    """The canonical name for a model as a transcript names it: lowercase, no routing or Anthropic-cloud prefix, no
    ``-v1:0`` suffix. A ``provider/model`` from pi keeps its provider unless that provider is the Anthropic cloud, so
    a SAINT model stays distinguishable from the Bedrock one it would otherwise share a name with."""
    m = " ".join((s or "").strip().lower().split())
    for _ in range(3):                               # prefixes nest in either order: "us.anthropic.x", "bedrock.us.x"
        for p in _REGIONS + _CLOUD:
            if m.startswith(p):
                m = m[len(p):]
                break
        else:
            break
    if m.endswith(":0"):                             # "…-v1:0" (Bedrock's inference-profile suffix)
        m = m[:-2]
    if m.endswith("-v1"):
        m = m[:-3]
    return m


def is_local_model(model: str) -> bool:
    """A model that names the SAINT box rather than a cloud account: ``saint/saint-auto``, ``qwen3:32b``, ``local/x``."""
    m = (model or "").strip().lower()
    return m.split("/", 1)[0] in _LOCAL_PROVIDERS if "/" in m else m.startswith("saint") or m.startswith("qwen")


def rates_for(model: str, table: dict[str, dict[str, float]] | None = None) -> dict[str, float] | None:
    """The rates for a model: the longest key that is a prefix of it ("" never matches). A model no key covers —
    a SAINT or qwen model, an unknown one — has no rate."""
    m = norm_model(model)
    if not m:
        return None
    table = RATES if table is None else table
    best = ""
    for k in table:
        if m.startswith(k) and len(k) > len(best):
            best = k
    return dict(table[best]) if best else None


def price(usage: dict[str, Any], rates: dict[str, float]) -> float:
    """Tokens/1M × the rate, summed over the four types. A type with tokens but no rate raises KeyError: that model
    isn't priced, and the caller must not report $0 for it."""
    total = 0.0
    for t in TOKEN_TYPES:
        n = usage.get(t) or 0
        if not n:
            continue
        if t not in rates:
            raise KeyError(t)
        total += n / 1e6 * rates[t]
    return total


def _priced(usage: dict[str, Any], rates: dict[str, float] | None) -> float | None:
    if not rates:
        return None
    try:
        return price(usage or {}, rates)
    except KeyError:
        return None


def tok_total(d: Any) -> int:
    """Total tokens of a usage dict, a job's tokens, or a bare int (SAINT reports one number)."""
    if isinstance(d, dict):
        return sum(int(d.get(t) or 0) for t in TOKEN_TYPES) if any(t in d for t in TOKEN_TYPES) else int(d.get("total") or 0)
    return int(d or 0)


def usage_spend(tokens: dict[str, int], model: str, reported: float = 0.0,
                prices: dict[str, dict[str, float]] | None = None, account: str = "default",
                priced: dict[str, int] | None = None) -> dict[str, Any]:
    """One session's spend row: what its turns cost at these rates.

    ``priced`` is the part of ``tokens`` the hub should price — pi prices a turn it reports ``cost_usd`` for itself,
    and those tokens must not be charged twice. ``reported`` is that self-priced total, added to ours. usd is null
    when nothing is priced (no tokens, or a model with no rate): never a false $0.
    """
    total = tok_total(tokens)
    rates = rates_for(model, _table(prices))
    usd = _priced(tokens if priced is None else priced, rates)
    if reported:
        usd = (usd or 0.0) + float(reported)
    return {"usd": round(usd, 6) if usd else None, "tokens": total, "by_type": dict(tokens or {}),
            "model": model or "", "account": account, "unpriced": bool(total and usd is None)}


def session_spend(turns: list[dict[str, Any]], model_fallback: str = "",
                  prices: dict[str, dict[str, float]] | None = None, account: str = "default") -> dict[str, Any]:
    """Sum a session's turns: tokens by type, the model its last turn named (else ``model_fallback``), and the cost —
    a turn's own ``cost_usd`` (pi) priced by pi, everything else by the table. A session whose model no rate covers is
    ``unpriced``: its usd is null, its tokens still count."""
    by_type = {t: 0 for t in TOKEN_TYPES}
    priced = {t: 0 for t in TOKEN_TYPES}
    model, reported = "", 0.0
    for t in turns or []:
        u = t.get("usage") or {}
        mine = not t.get("cost_usd")                # pi priced this turn itself; don't price its tokens again
        for k in TOKEN_TYPES:
            n = int(u.get(k) or 0)
            by_type[k] += n
            priced[k] += n if mine else 0
        if t.get("model"):
            model = t["model"]
        if t.get("cost_usd"):
            reported += float(t["cost_usd"])
    return usage_spend(by_type, model or model_fallback, reported, prices, account, priced)


def _table(prices: dict[str, dict[str, float]] | None) -> dict[str, dict[str, float]]:
    """RATES with [hub.prices] applied on top (a config key overrides that model, others keep the built-in rate)."""
    if not prices:
        return RATES
    return {**RATES, **{norm_model(k): dict(v or {}) for k, v in prices.items()}}


class SaintUsage:
    """SAINT's ``/usage/v1`` endpoint: energy and token totals for a date range, or for one session.

    GET ``{url}?from=YYYY-MM-DD&to=YYYY-MM-DD[&session=<id>]`` — the dates are UTC calendar days from the ISO instants
    asked for, and inclusive, so a window's edge days can carry a little more than the window itself. One fetch per
    URL for ``ttl`` seconds; any failure gives None and leaves the reason in ``last_error`` (the report says "local —"
    rather than guessing).
    """

    def __init__(self, url: str, timeout: float = 5.0, ttl: float = 60.0, transport: Any = None):
        self.url, self.timeout, self.ttl, self.transport = (url or "").rstrip("/"), timeout, ttl, transport
        self.last_error: str | None = None
        self._cache: dict[Any, tuple[float, Any]] = {}

    @staticmethod
    def _dates(frm: str | None, to: str | None) -> tuple[str, str]:
        def day(iso: str | None, default: datetime) -> str:
            return _iso(iso or "").date().isoformat() if _iso(iso or "") else default.date().isoformat()
        now = datetime.now(timezone.utc)
        return day(frm, now), day(to, now)

    def window(self, from_iso: str | None, to_iso: str | None) -> dict[str, Any] | None:
        f, t = self._dates(from_iso, to_iso)
        return self._get({"from": f, "to": t})

    def session(self, sid: str) -> dict[str, Any] | None:
        return self._get({"session": sid}) if sid else None

    def _get(self, params: dict[str, str]) -> dict[str, Any] | None:
        if not self.url:
            return None
        key = (self.url, tuple(sorted(params.items())))
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        try:
            import httpx
            with httpx.Client(transport=self.transport, timeout=self.timeout) as c:
                r = c.get(self.url, params=params)
                r.raise_for_status()
                data = r.json()
            data = data if isinstance(data, dict) else {"totals": data}
            self.last_error = None
        except Exception as e:                          # noqa: BLE001 - an unreachable SAINT is not an error here
            self.last_error = f"{type(e).__name__}: {e}"
            return None
        self._cache[key] = (time.time(), data)
        return data


def spend_report(store, frm: str, to: str, host: str | None, prices: dict[str, dict[str, float]],
                 saint: SaintUsage | None, price_as: str) -> dict[str, Any]:
    """The body of GET /v1/spend: every session with turns that started in ``[frm, to)``, priced.

    Cloud = what the hub can price from the transcripts; local = SAINT's own energy accounting for the same dates,
    which is also how a pool job's tokens get a dollar figure (the window's $/token, applied to that job's tokens).
    ``total_usd`` adds the two; ``unpriced_models`` lists what no rate covers, so "cheap" is never a silent lie.
    """
    table = _table(prices)
    rows = store.usage_window(frm, to, host)
    cloud_usd, unpriced = 0.0, set()
    per_account: dict[str, dict[str, Any]] = {}
    per_model: dict[str, dict[str, Any]] = {}
    seen: dict[str, set[str]] = {}                  # account -> session keys: a session on two models counts once
    sessions: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    job_tok: dict[str, int] = {}                    # a pool job's turns ran locally: tokens for its job row, no cloud $
    for r in rows:
        if r["job"]:
            job_tok[r["key"]] = job_tok.get(r["key"], 0) + tok_total(r["tokens"])
            continue
        harness = r["harness"]
        account = (r["meta"].get("profile") or "default") if harness == "claude" else (harness or "default")
        s = usage_spend(r["tokens"], r["model"], r["reported"], prices, account, r.get("priced"))   # pi-priced turns once
        usd = s["usd"] or 0.0
        cloud_usd += usd
        if s["tokens"]:
            if s["unpriced"]:
                unpriced.add(r["model"] or "(unknown)")
            a = per_account.setdefault(account, {"account": account, "usd": 0.0, "tokens": 0, "sessions": 0})
            a["usd"] += usd
            a["tokens"] += s["tokens"]
            if r["key"] not in seen.setdefault(account, set()):
                seen[account].add(r["key"]); a["sessions"] += 1
            key = norm_model(r["model"]) or "(unknown)"
            m = per_model.setdefault(key, {"model": key, "usd": 0.0, "tokens": 0, "priced": s["usd"] is not None})
            m["usd"] += usd
            m["tokens"] += s["tokens"]
            m["priced"] = m["priced"] and not s["unpriced"]
        row = by_key.get(r["key"])                      # one entry per session: its models' rows summed
        if row is None:
            row = by_key[r["key"]] = {"key": r["key"], "title": r["title"] or "", "host_id": r["host_id"], "harness": harness,
                                      "account": account, "usd": None, "tokens": 0, "model": r["model"] or ""}
            sessions.append(row)
        if s["usd"] is not None:
            row["usd"] = (row["usd"] or 0.0) + s["usd"]
        row["tokens"] += s["tokens"]
        if r["model"]:
            row["model"] = r["model"]
    sessions.sort(key=lambda d: (-(d["usd"] or 0.0), -d["tokens"], d["key"]))
    sessions = sessions[:30]

    local = _local(saint, frm, to, price_as, table)
    saint_jobs = local.pop("_jobs", [])
    jobs = []
    seen_jobs: set[str] = set()
    for r in rows:
        if not r["job"] or r["key"] in seen_jobs:    # one job row per session (its rows are per model)
            continue
        seen_jobs.add(r["key"])
        t = tok_total(r["meta"].get("job_tokens") or {}) or job_tok.get(r["key"], 0)   # the ledger's count, else its turns'

        jobs.append({"job": r["job"], "key": r["key"], "parent_key": r["parent_key"],
                     "parent_title": _title(store, r["parent_key"]), "status": r["job_status"],
                     "brief": r["job_brief"], "started_at": r["started_at"] or "", "dur_s": r["meta"].get("job_dur_s"),
                     "turns": r["meta"].get("job_turns"), "tokens": t,
                     "local_usd": _per_token(local["energy_usd"], local["tokens"], t),
                     "if_cloud_usd": _priced(r["meta"].get("job_tokens") or {}, rates_for(price_as, table))})
    # SAINT also knows about jobs the ledger never linked to a transcript: show those too, without a key
    named = {j["job"] for j in jobs}
    jobs += [j for j in saint_jobs if j["job"] not in named]
    jobs.sort(key=lambda d: d["started_at"], reverse=True)
    return {"window": {"from": frm, "to": to},
            "cloud": {"usd": round(cloud_usd, 6), "tokens": store.usage_total(frm, to, host)["tokens"],
                      "by_account": _sorted(per_account), "by_model": _sorted(per_model)},
            "local": local,
            "jobs": jobs, "sessions": sessions,
            "total_usd": round(cloud_usd + (local["energy_usd"] or 0.0), 6),
            "unpriced_models": sorted(unpriced), "rates": "config" if prices else "default"}


def _sorted(d: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(d.values(), key=lambda x: (-x["usd"], -x["tokens"], x.get("account") or x.get("model") or ""))


def _iso(s: str) -> datetime | None:
    """An ISO instant as an aware UTC datetime; None when it isn't one."""
    try:
        d = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def utc(s: str) -> datetime:
    """An ISO instant as an aware UTC datetime; the epoch when it isn't one (callers validate with iso_instant)."""
    return _iso(s) or datetime(1970, 1, 1, tzinfo=timezone.utc)


def iso_instant(s: str) -> str | None:
    """An ISO timestamp normalised for the string comparisons the store filters on, or None when it isn't one."""
    d = _iso(s)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


def _title(store, key: str) -> str:
    if not key:
        return ""
    r = store.db.execute("SELECT live_name, title FROM sessions WHERE key=?", (key,)).fetchone()
    return (r["live_name"] or r["title"] or "") if r else ""


def _per_token(energy: float | None, tokens: int, n: int) -> float | None:
    if energy is None or not tokens or not n:
        return None
    return round(energy * n / tokens, 6)


def _local(saint: SaintUsage | None, frm: str, to: str, price_as: str, table: dict[str, dict[str, float]]) -> dict[str, Any]:
    """SAINT's side of the window: energy, token totals, request count, what those tokens would have cost in the
    cloud, and one row per job it ran."""
    out = {"energy_usd": None, "if_cloud_usd": None, "tokens": 0, "requests": 0, "price_as": price_as,
           "source": None, "error": None, "_jobs": []}
    if saint is None or not saint.url:
        return out
    data = saint.window(frm, to)
    if data is None:
        out["error"] = saint.last_error
        return out
    totals = data.get("totals") or data                  # {"totals": {...}} or the totals themselves
    out["source"] = "saint"
    energy = totals.get("energy_cost_usd")
    out["energy_usd"] = round(float(energy), 6) if isinstance(energy, (int, float)) else None
    out["requests"] = int(totals.get("requests") or totals.get("request_count") or 0)
    by_type = {t: int((totals.get("tokens_by_type") or {}).get(t) or 0) for t in TOKEN_TYPES}
    out["tokens"] = tok_total(by_type) or tok_total(totals.get("tokens"))
    out["if_cloud_usd"] = _priced(by_type, rates_for(price_as, table))
    for j in data.get("jobs") or []:
        t = tok_total(j.get("tokens") or j.get("tokens_by_type") or {})
        out["_jobs"].append({"job": j.get("job") or "", "key": "", "parent_key": "", "parent_title": "",
                             "status": j.get("status") or "", "brief": "", "started_at": j.get("started_at") or j.get("first_seen") or "",
                             "dur_s": None, "turns": j.get("turns"), "tokens": t,
                             "local_usd": _per_token(out["energy_usd"], out["tokens"], t),
                             "if_cloud_usd": _priced(j.get("tokens_by_type") or {}, rates_for(price_as, table))})
    return out
