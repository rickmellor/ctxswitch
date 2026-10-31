"""Hub HTTP API (starlette). REST for ingest and queries, WebSocket for change notifications.

  POST /v1/ingest                  {host_id, events:[{seq, kind, payload}]} -> {acked: <last seq>}
  GET  /v1/hosts
  GET  /v1/sessions?host=&live=1
  GET  /v1/sessions/{key}
  POST /v1/sessions/{key}/viewed   {at}
  GET  /v1/sessions/{key}/brief    {text: resume briefing or null}  (hub/brief.py)
  POST /v1/sessions/{key}/ledger/{id}/retire   operator retires a ledger row (e.g. an instruction that no longer holds)
  POST /v1/sessions/{key}/messages   {body, from_host, answers, from_key} -> {id}   queue a message for a session (live
                                     or not); answers = ref of the open plain-text ask it replies to (a quick-reply);
                                     from_key = the agent session sending it (none: the operator)
  POST /v1/workstreams/messages      {workstream, body, from_host} -> {id}   for whichever session is (or next is) live
                                     on that repo + branch, any host
  POST /v1/messages/{id}/cancel      a queued message only
  GET  /v1/outbox?host=&wait=25&agents=1   collector long-poll: queued messages for that host's live sessions (agents'
                                     messages only with agents=1)
  POST /v1/messages/{id}/status      collector report {host_id, status: sent|failed, error}
  GET  /v1/attention?host=
  GET  /v1/spend?from=ISO&to=ISO&host=    what the window cost: cloud turns priced per account / model, SAINT's local
                                     energy, one row per pool job, the top sessions (hub/spend.py)
  GET  /v1/health
  WS   /v1/ws                      server pushes {"change": N} whenever the store changes
Auth: ``Authorization: Bearer <token>`` when hub.token is set (collectors and clients share it).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import timedelta
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from ctxswitch.hub.brief import brief
from ctxswitch.hub.hooks import HookApplier
from ctxswitch.hub.spend import SaintUsage, iso_instant, spend_report, tok_total, utc
from ctxswitch.hub.store import NOT_DELIVERABLE, REPLYABLE, SEARCH_KINDS, Store

log = logging.getLogger("ctxswitch.hub")
MAX_BODY = 20000                                     # characters in one message
OUTBOX_WAIT = 25.0                                   # seconds a collector's long-poll may hang
SPEND_DEFAULT_DAYS = 1                               # /v1/spend with no "from"
LOCAL_HARNESSES = ("claude", "pi")                   # a session of these can have run on SAINT, so ask SAINT


class Hub:
    def __init__(self, store: Store, token: str = "", saint: SaintUsage | None = None,
                 prices: dict[str, dict[str, float]] | None = None, price_as: str = "claude-opus-5-5"):
        """``saint``: the SAINT usage endpoint for local energy (None or an empty url: no local numbers at all).
        ``prices``: [hub.prices] rates, which the store also needs to price the rows it lists."""
        self.store, self.token = store, token
        self.saint = saint
        self.prices = prices or {}
        self.price_as = price_as
        store.prices = self.prices
        self.hooks = HookApplier(store)
        self.recap_status: dict[str, Any] | None = None      # set by the Recapper: {seat, last_ok, last_error, error_at}
        self.ledger_status: dict[str, Any] | None = None     # set by the Extractor: {extracted, dropped, backlog, ...}
        self.subscribers: set[asyncio.Queue] = set()
        self.background: list = []                       # coroutine factories started with the app (e.g. the recap tracker)
        self._tasks: list[asyncio.Task] = []

    # -- auth -----------------------------------------------------------------------------------------------------
    def _authorized(self, headers) -> bool:
        if not self.token:
            return True
        return headers.get("authorization", "") == f"Bearer {self.token}"

    def _notify(self) -> None:
        for q in list(self.subscribers):
            if q.qsize() < 2:
                q.put_nowait(self.store.change)

    # -- handlers -------------------------------------------------------------------------------------------------
    async def ingest(self, req: Request) -> Response:
        if not self._authorized(req.headers):
            return JSONResponse({"error": "unauthorized"}, 401)
        body = await req.json()
        host = body.get("host_id")
        if not isinstance(host, str) or not host:
            return JSONResponse({"error": "host_id required"}, 400)
        acked = None
        n = 0
        for ev in body.get("events", []):
            try:
                self.apply(host, ev.get("kind"), ev.get("payload") or {})
            except Exception:                           # noqa: BLE001 - one bad event must not block the queue
                log.exception("bad event from %s: %s", host, str(ev)[:200])
            acked = ev.get("seq", acked); n += 1
        self.store.host_seen(host, body.get("info"))
        if n:
            self._notify()
        return JSONResponse({"acked": acked, "applied": n, "change": self.store.change})

    def apply(self, host: str, kind: str, payload: dict[str, Any]) -> None:
        if kind == "session":
            self.store.upsert_session(payload)
        elif kind == "turns":
            self.store.upsert_turns(payload["key"], payload.get("turns", []), payload.get("open"), payload.get("ended", False))
        elif kind == "hook":
            self.hooks.apply(host, payload)
        elif kind == "liveness":
            self.store.set_liveness(payload.get("host_id", host), payload.get("sessions", []))
        elif kind == "budget":
            self.store.set_budget(payload)             # the monthly budget as the host with the token reads it
        else:
            log.warning("unknown event kind %r from %s", kind, host)

    async def hosts(self, req: Request) -> Response:
        if not self._authorized(req.headers): return JSONResponse({"error": "unauthorized"}, 401)
        return JSONResponse(self.store.hosts())

    async def sessions(self, req: Request) -> Response:
        if not self._authorized(req.headers): return JSONResponse({"error": "unauthorized"}, 401)
        host = req.query_params.get("host") or None
        live = req.query_params.get("live") in ("1", "true")
        rows = self.store.sessions(host, include_ended=not live)
        self._price_jobs(rows)
        return JSONResponse({"change": self.store.change, "sessions": rows})

    async def session(self, req: Request) -> Response:
        if not self._authorized(req.headers): return JSONResponse({"error": "unauthorized"}, 401)
        s = self.store.session(req.path_params["key"], turns=int(req.query_params.get("turns", "50")))
        if s:
            self._add_local(s)
            self._price_jobs([s])
        return JSONResponse(s) if s else JSONResponse({"error": "not found"}, 404)

    def _price_jobs(self, rows: list[dict[str, Any]]) -> None:
        """A pool job's local cost estimate: its tokens (the ledger's count, else its turns') × SAINT's energy per token
        over the last 30 days. ``spend.local_usd`` is None when SAINT has nothing to say."""
        jobs = [r for r in rows if r.get("job") and isinstance(r.get("spend"), dict)]
        if not jobs:
            return
        rate = None
        if self.saint is not None and self.saint.url:
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            w = self.saint.window(_minus(now, 30), now)
            totals = (w or {}).get("totals") or w or {}
            energy, tokens = totals.get("energy_cost_usd"), tok_total(totals.get("tokens_by_type") or totals.get("total_tokens") or 0)
            if isinstance(energy, (int, float)) and tokens:
                rate = float(energy) / tokens
        for r in jobs:
            t = tok_total((r.get("meta") or {}).get("job_tokens") or {}) or r["spend"].get("tokens") or 0
            r["spend"]["local_usd"] = round(rate * t, 6) if rate is not None and t else None

    def _add_local(self, s: dict[str, Any]) -> None:
        """What SAINT says this session cost it, when the session is one SAINT could have run and there's an endpoint
        to ask. None everywhere else: 'we didn't ask' isn't 'it cost nothing'."""
        spend = s.get("spend")
        if not isinstance(spend, dict):
            return
        spend["local"] = None
        saint = self.saint
        if saint is None or not saint.url or s.get("harness") not in LOCAL_HARNESSES or not s.get("session_id"):
            return
        data = saint.session(s["session_id"])
        if not data:
            return
        totals = data.get("totals") or data
        energy = totals.get("energy_cost_usd")
        tokens = tok_total(totals.get("tokens_by_type") or totals.get("tokens") or totals.get("total_tokens") or 0)
        spend["local"] = {"energy_usd": round(float(energy), 6) if isinstance(energy, (int, float)) else None,
                          "tokens": int(tokens or 0), "requests": int(totals.get("requests") or 0)}

    async def spend(self, req: Request) -> Response:
        """GET /v1/spend?from=ISO&to=ISO[&host=]: the window's cost. Turns count by when they started; the window's
        right edge is exclusive. Both default: the last day up to now."""
        if not self._authorized(req.headers): return JSONResponse({"error": "unauthorized"}, 401)
        p = req.query_params
        to = iso_instant(p.get("to")) if p.get("to") else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if not to:
            return JSONResponse({"error": "to must be an ISO timestamp"}, 400)
        frm = iso_instant(p.get("from")) if p.get("from") else _minus(to, SPEND_DEFAULT_DAYS)
        if not frm:
            return JSONResponse({"error": "from must be an ISO timestamp"}, 400)
        if frm >= to:
            return JSONResponse({"error": "from must be before to"}, 400)
        return JSONResponse(spend_report(self.store, frm, to, p.get("host") or None, self.prices, self.saint, self.price_as))

    async def brief(self, req: Request) -> Response:
        if not self._authorized(req.headers): return JSONResponse({"error": "unauthorized"}, 401)
        return JSONResponse({"text": brief(self.store.session(req.path_params["key"], turns=10))})

    async def handoff(self, req: Request) -> Response:
        """GET: the handoff document and opening prompt for continuing this session in another agent (?to=agy&full=1).
        POST {to, profile}: note that a handoff was started (shown until the new session links itself)."""
        if not self._authorized(req.headers): return JSONResponse({"error": "unauthorized"}, 401)
        from ctxswitch.hub.handoff import prompt, render
        key = req.path_params["key"]
        if req.method == "POST":
            body = await req.json()
            if not self.store.session(key, turns=0):
                return JSONResponse({"error": "not found"}, 404)
            self.store.note_handoff(key, body.get("to", ""), body.get("profile"))
            self._notify()
            return JSONResponse({"ok": True})
        s = self.store.session(key, turns=int(req.query_params.get("turns", "500")))
        if not s:
            return JSONResponse({"error": "not found"}, 404)
        to = req.query_params.get("to") or None
        full = req.query_params.get("full", "1") != "0"
        return JSONResponse({"text": render(s, to, full), "prompt": prompt("{path}", s, to)})

    async def viewed(self, req: Request) -> Response:
        if not self._authorized(req.headers): return JSONResponse({"error": "unauthorized"}, 401)
        body = await req.json()
        self.store.mark_viewed(req.path_params["key"], body.get("at"))
        self._notify()
        return JSONResponse({"ok": True})

    async def retire(self, req: Request) -> Response:
        if not self._authorized(req.headers): return JSONResponse({"error": "unauthorized"}, 401)
        ok = self.store.retire_row(req.path_params["key"], int(req.path_params["row"]))
        if ok:
            self._notify()
        return JSONResponse({"ok": ok}, 200 if ok else 404)

    async def send_workstream(self, req: Request) -> Response:
        if not self._authorized(req.headers): return JSONResponse({"error": "unauthorized"}, 401)
        body = await req.json()
        text, ws = body.get("body"), body.get("workstream")
        if not isinstance(text, str) or not text.strip():
            return JSONResponse({"error": "body required"}, 400)
        if len(text) > MAX_BODY:
            return JSONResponse({"error": f"message longer than {MAX_BODY} characters"}, 400)
        if not isinstance(ws, str) or not self.store.has_workstream(ws):
            return JSONResponse({"error": f"no session works on {ws!r}"}, 404)
        mid = self.store.add_workstream_message(ws, text, body.get("from_host"))
        self._notify()
        return JSONResponse({"id": mid})

    async def send(self, req: Request) -> Response:
        if not self._authorized(req.headers): return JSONResponse({"error": "unauthorized"}, 401)
        key = req.path_params["key"]
        body = await req.json()
        text = body.get("body")
        if not isinstance(text, str) or not text.strip():
            return JSONResponse({"error": "body required"}, 400)
        if len(text) > MAX_BODY:
            return JSONResponse({"error": f"message longer than {MAX_BODY} characters"}, 400)
        s = self.store.session(key, turns=0)
        if s is None:
            return JSONResponse({"error": "not found"}, 404)
        if s["harness"] in NOT_DELIVERABLE:
            return JSONResponse({"error": f"can't deliver to {s['harness']} sessions"}, 400)
        ref, sender = body.get("answers"), body.get("from_key")
        if sender is not None:
            if not isinstance(sender, str) or self.store.session(sender, turns=0) is None:
                return JSONResponse({"error": f"no session {sender!r} to send from"}, 400)
            if sender == key:
                return JSONResponse({"error": "a session can't message itself"}, 400)
            if ref is not None:
                return JSONResponse({"error": "only the operator answers a session's questions"}, 400)
            refused = self.store.agent_send_refused(sender, key)
            if refused:
                return JSONResponse({"error": refused[1]}, refused[0])
        if ref is not None:
            ask = self.store.open_ask(key, str(ref))
            if ask is None:
                return JSONResponse({"error": "that question is already answered, an answer is on its way, or it isn't this session's"}, 409)
            if ask["kind"] not in REPLYABLE:
                return JSONResponse({"error": f"a {ask['kind']} ask is a dialog in the session: answer it there"}, 400)
        mid = self.store.add_message(key, text, body.get("from_host"), ref, sender)
        self._notify()
        return JSONResponse({"id": mid})

    async def cancel(self, req: Request) -> Response:
        if not self._authorized(req.headers): return JSONResponse({"error": "unauthorized"}, 401)
        ok = self.store.cancel_message(int(req.path_params["id"]))
        if ok:
            self._notify()
        return JSONResponse({"ok": ok}, 200 if ok else 409)

    async def outbox(self, req: Request) -> Response:
        """Returns as soon as something is waiting for this host, or after ``wait`` seconds with nothing."""
        if not self._authorized(req.headers): return JSONResponse({"error": "unauthorized"}, 401)
        host = req.query_params.get("host")
        if not host:
            return JSONResponse({"error": "host required"}, 400)
        wait = min(float(req.query_params.get("wait", "0") or 0), OUTBOX_WAIT)
        agents = req.query_params.get("agents") == "1"
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(q)
        try:
            deadline = asyncio.get_running_loop().time() + wait
            while True:
                items = self.store.outbox(host, agents)
                left = deadline - asyncio.get_running_loop().time()
                if items or left <= 0:
                    return JSONResponse({"messages": items})
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(q.get(), timeout=left)
        finally:
            self.subscribers.discard(q)

    async def message_status(self, req: Request) -> Response:
        if not self._authorized(req.headers): return JSONResponse({"error": "unauthorized"}, 401)
        body = await req.json()
        ok = self.store.set_message_status(int(req.path_params["id"]), str(body.get("host_id") or ""),
                                           str(body.get("status") or ""), body.get("error"))
        if ok:
            self._notify()
        return JSONResponse({"ok": ok}, 200 if ok else 409)

    async def attention(self, req: Request) -> Response:
        if not self._authorized(req.headers): return JSONResponse({"error": "unauthorized"}, 401)
        return JSONResponse({"change": self.store.change, "items": self.store.attention(req.query_params.get("host") or None)})

    async def search(self, req: Request) -> Response:
        if not self._authorized(req.headers): return JSONResponse({"error": "unauthorized"}, 401)
        p = req.query_params
        kinds = tuple(k for k in (p.get("kind") or ",".join(SEARCH_KINDS)).split(",") if k in SEARCH_KINDS)
        if not kinds:
            return JSONResponse({"error": f"kind must be one of {', '.join(SEARCH_KINDS)}"}, 400)
        limit = max(1, min(int(p.get("limit", "30") or 30), 200))
        return JSONResponse({"results": self.store.search(p.get("q") or "", kinds, p.get("host") or None, limit)})

    async def health(self, req: Request) -> Response:
        auth = self._authorized(req.headers)                 # unauthenticated callers don't learn the seat
        budgets = ({a: {k: b.get(k) for k in ("remaining_usd", "pct_used", "as_of")}
                    for a, b in self.store.budgets().items()} if auth else None)
        return JSONResponse({"ok": True, "change": self.store.change, "hosts": len(self.store.hosts()),
                             "auth": auth, "recap": self.recap_status if auth else None,
                             "ledger": self.ledger_status if auth else None, "budget": budgets})

    async def ws(self, ws: WebSocket) -> None:
        if not self._authorized(ws.headers) and ws.query_params.get("token") != self.token:
            await ws.close(code=4401); return
        await ws.accept()
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(q)
        try:
            await ws.send_text(json.dumps({"change": self.store.change}))
            while True:
                try:
                    change = await asyncio.wait_for(q.get(), timeout=25)
                    await ws.send_text(json.dumps({"change": change}))
                except asyncio.TimeoutError:
                    await ws.send_text(json.dumps({"ping": True}))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            self.subscribers.discard(q)

    @contextlib.asynccontextmanager
    async def _lifespan(self, app):
        self._tasks = [asyncio.create_task(f()) for f in self.background]
        try:
            yield
        finally:
            for t in self._tasks:
                t.cancel()

    def app(self) -> Starlette:
        return Starlette(lifespan=self._lifespan, routes=[
            Route("/v1/ingest", self.ingest, methods=["POST"]),
            Route("/v1/hosts", self.hosts),
            Route("/v1/sessions", self.sessions),
            Route("/v1/sessions/{key:path}/viewed", self.viewed, methods=["POST"]),
            Route("/v1/sessions/{key:path}/brief", self.brief),
            Route("/v1/sessions/{key:path}/handoff", self.handoff, methods=["GET", "POST"]),
            Route("/v1/sessions/{key:path}/ledger/{row:int}/retire", self.retire, methods=["POST"]),
            Route("/v1/sessions/{key:path}/messages", self.send, methods=["POST"]),
            Route("/v1/workstreams/messages", self.send_workstream, methods=["POST"]),
            Route("/v1/sessions/{key:path}", self.session),
            Route("/v1/messages/{id:int}/cancel", self.cancel, methods=["POST"]),
            Route("/v1/messages/{id:int}/status", self.message_status, methods=["POST"]),
            Route("/v1/outbox", self.outbox),
            Route("/v1/attention", self.attention),
            Route("/v1/spend", self.spend),
            Route("/v1/search", self.search),
            Route("/v1/health", self.health),
            WebSocketRoute("/v1/ws", self.ws),
        ])


def _minus(iso: str, days: int) -> str:
    """``iso`` minus N days, still an ISO instant (for the default /v1/spend window)."""
    d = utc(iso) - timedelta(days=days)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")
