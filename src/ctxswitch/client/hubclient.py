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

    def resolve(self, q: str) -> tuple[str | None, list[dict[str, Any]]]:
        """A full key, or the one session whose key or title contains ``q``; else (None, the candidates). A key cut
        short ("devbox/pi/01a1") matches like any other part of one."""
        rows = self.sessions(None)["sessions"]
        if any(s["key"] == q for s in rows):
            return q, []
        matches = [s for s in rows if q in s["key"] or q in (s.get("title") or "")]
        if not matches and q.count("/") >= 2:
            return q, []                                             # not listed: let the hub say whether it exists
        return (matches[0]["key"], matches) if len(matches) == 1 else (None, matches)

    def attention(self, host: str | None = None) -> dict[str, Any]:
        r = self._c.get("/v1/attention", params={"host": host} if host else None); r.raise_for_status()
        return r.json()

    def search(self, q: str, kinds: str | None = None, host: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        params = {"q": q, "limit": limit} | ({"kind": kinds} if kinds else {}) | ({"host": host} if host else {})
        r = self._c.get("/v1/search", params=params)
        if r.status_code != 200:
            raise ValueError(_error(r))
        return r.json()["results"]

    def spend(self, frm: str, to: str, host: str | None = None) -> dict[str, Any]:
        """What everything cost between two ISO instants: cloud by account and model, SAINT's local energy estimate,
        the pool jobs, the dearest sessions. An older hub has no such route: ValueError."""
        r = self._c.get("/v1/spend", params={"from": frm, "to": to} | ({"host": host} if host else {}))
        if r.status_code != 200:
            raise ValueError(_error(r))
        return r.json()

    def viewed(self, key: str, at: str) -> None:
        self._c.post(f"/v1/sessions/{key}/viewed", json={"at": at}).raise_for_status()

    def send(self, key: str, body: str, from_host: str | None = None, answers: str | None = None, from_key: str | None = None) -> int:
        """``from_key``: the agent session sending it; None = the operator."""
        r = self._c.post(f"/v1/sessions/{key}/messages", json={"body": body, "from_host": from_host, "answers": answers,
                                                              **({"from_key": from_key} if from_key else {})})
        if r.status_code != 200:
            raise ValueError(_error(r))
        return int(r.json()["id"])

    def send_workstream(self, workstream: str, body: str, from_host: str | None = None) -> int:
        r = self._c.post("/v1/workstreams/messages", json={"workstream": workstream, "body": body, "from_host": from_host})
        if r.status_code != 200:
            raise ValueError(_error(r))
        return int(r.json()["id"])

    def handoff(self, key: str, to: str | None = None, full: bool = True) -> dict[str, Any]:
        """{text, prompt} for continuing ``key`` in agent ``to``; prompt has a {path} placeholder for the document."""
        r = self._c.get(f"/v1/sessions/{key}/handoff", params={"to": to or "", "full": "1" if full else "0"})
        if r.status_code != 200:
            raise ValueError(_error(r))
        return r.json()

    def note_handoff(self, key: str, to: str, profile: str | None = None) -> None:
        self._c.post(f"/v1/sessions/{key}/handoff", json={"to": to, "profile": profile}).raise_for_status()

    def retire(self, key: str, row_id: int) -> bool:
        return self._c.post(f"/v1/sessions/{key}/ledger/{row_id}/retire").status_code == 200

    def cancel(self, msg_id: int) -> bool:
        return self._c.post(f"/v1/messages/{msg_id}/cancel").status_code == 200

    def ws_url(self) -> str:
        u = self.base.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/v1/ws"
        return u + (f"?token={self.token}" if self.token else "")


def _error(r: httpx.Response) -> str:
    """The hub's {"error": ...}, or the status when the body isn't that (an older hub's 404 is plain text)."""
    try:
        return r.json().get("error") or f"HTTP {r.status_code}"
    except ValueError:
        return f"HTTP {r.status_code}"
