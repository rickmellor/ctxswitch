"""Hub HTTP API (starlette). REST for ingest and queries, WebSocket for change notifications.

  POST /v1/ingest                  {host_id, events:[{seq, kind, payload}]} -> {acked: <last seq>}
  GET  /v1/hosts
  GET  /v1/sessions?host=&live=1
  GET  /v1/sessions/{key}
  POST /v1/sessions/{key}/viewed   {at}
  GET  /v1/attention?host=
  GET  /v1/health
  WS   /v1/ws                      server pushes {"change": N} whenever the store changes
Auth: ``Authorization: Bearer <token>`` when hub.token is set (collectors and clients share it).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from ctxswitch.hub.hooks import HookApplier
from ctxswitch.hub.store import Store

log = logging.getLogger("ctxswitch.hub")


class Hub:
    def __init__(self, store: Store, token: str = ""):
        self.store, self.token = store, token
        self.hooks = HookApplier(store)
        self.recap_status: dict[str, Any] | None = None      # set by the Recapper: {seat, last_ok, last_error, error_at}
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
        else:
            log.warning("unknown event kind %r from %s", kind, host)

    async def hosts(self, req: Request) -> Response:
        if not self._authorized(req.headers): return JSONResponse({"error": "unauthorized"}, 401)
        return JSONResponse(self.store.hosts())

    async def sessions(self, req: Request) -> Response:
        if not self._authorized(req.headers): return JSONResponse({"error": "unauthorized"}, 401)
        host = req.query_params.get("host") or None
        live = req.query_params.get("live") in ("1", "true")
        return JSONResponse({"change": self.store.change, "sessions": self.store.sessions(host, include_ended=not live)})

    async def session(self, req: Request) -> Response:
        if not self._authorized(req.headers): return JSONResponse({"error": "unauthorized"}, 401)
        s = self.store.session(req.path_params["key"], turns=int(req.query_params.get("turns", "50")))
        return JSONResponse(s) if s else JSONResponse({"error": "not found"}, 404)

    async def viewed(self, req: Request) -> Response:
        if not self._authorized(req.headers): return JSONResponse({"error": "unauthorized"}, 401)
        body = await req.json()
        self.store.mark_viewed(req.path_params["key"], body.get("at"))
        self._notify()
        return JSONResponse({"ok": True})

    async def attention(self, req: Request) -> Response:
        if not self._authorized(req.headers): return JSONResponse({"error": "unauthorized"}, 401)
        return JSONResponse({"change": self.store.change, "items": self.store.attention(req.query_params.get("host") or None)})

    async def health(self, req: Request) -> Response:
        auth = self._authorized(req.headers)                 # unauthenticated callers don't learn the seat
        return JSONResponse({"ok": True, "change": self.store.change, "hosts": len(self.store.hosts()),
                             "auth": auth, "recap": self.recap_status if auth else None})

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
            Route("/v1/sessions/{key:path}", self.session),
            Route("/v1/attention", self.attention),
            Route("/v1/health", self.health),
            WebSocketRoute("/v1/ws", self.ws),
        ])
