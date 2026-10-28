"""Thin HTTP client for the hub, shared by CLI, TUI and MCP."""
from __future__ import annotations

from typing import Any

import httpx

from ctxswitch.common.config import Config


class HubClient:
    def __init__(self, cfg: Config):
        self.base = cfg.hub.url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {cfg.hub.token}"} if cfg.hub.token else {}
        self.token = cfg.hub.token
        self._c = httpx.Client(base_url=self.base, headers=self.headers, timeout=10)

    def health(self) -> dict[str, Any]:
        return self._c.get("/v1/health").json()

    def hosts(self) -> list[dict[str, Any]]:
        return self._c.get("/v1/hosts").json()

    def sessions(self, host: str | None = None, live_only: bool = False) -> dict[str, Any]:
        params = {}
        if host: params["host"] = host
        if live_only: params["live"] = "1"
        r = self._c.get("/v1/sessions", params=params); r.raise_for_status()
        return r.json()

    def session(self, key: str, turns: int = 50) -> dict[str, Any] | None:
        r = self._c.get(f"/v1/sessions/{key}", params={"turns": turns})
        return r.json() if r.status_code == 200 else None

    def attention(self, host: str | None = None) -> dict[str, Any]:
        r = self._c.get("/v1/attention", params={"host": host} if host else None); r.raise_for_status()
        return r.json()

    def viewed(self, key: str, at: str) -> None:
        self._c.post(f"/v1/sessions/{key}/viewed", json={"at": at}).raise_for_status()

    def ws_url(self) -> str:
        u = self.base.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/v1/ws"
        return u + (f"?token={self.token}" if self.token else "")
