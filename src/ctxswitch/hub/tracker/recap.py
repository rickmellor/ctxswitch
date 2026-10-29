"""Recap: one at-a-glance line per session — "what do I need to know about this session right now?".

Runs in the hub. A session needs a recap when its newest turn (index + complete flag + open-ask count = the
*anchor*) differs from the one its stored recap was made from. Anchors must hold still for ``debounce`` seconds
before a call is made, so a busy session isn't recapped on every streamed line. Sessions are taken live-first,
then most recent; only sessions live or active in the last ``days`` days are considered (no backfill over history).

The recap is regenerated from source every time (goal prompt + recent exchanges + open asks), never by editing the
previous recap, so a bad one does not compound. The seat is local only (SAINT text seat); a seat failure leaves the
old recap in place and backs off.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from ctxswitch.common.schema import LIVE
from ctxswitch.common.config import SeatConfig
from ctxswitch.hub.store import Store

log = logging.getLogger("ctxswitch.recap")

MAX_CHARS = 180
SYSTEM = (
    "You maintain an operator's short-term memory across many parallel coding-agent sessions. Given one session's "
    "goal, recent exchanges between the operator (\"you\") and the agent, and anything the agent is waiting on, write "
    "ONE short line (at most 120 characters) telling the operator what they need to know about this session right "
    "now: where the work stands and what, if anything, it needs from them. If it is waiting on them, start with "
    "that. Be concrete (name the feature, file, PR or decision). Do not repeat the session title or say whether it "
    "is busy, idle or ended: the operator already sees those. Use only facts in the input; never invent results. "
    "Address the operator as \"you\". No preamble, no quotes.\n"
    "Also write reentry: at most 4 short lines (newline-separated) for when the operator switches back to this "
    "session: what it is doing, what they told it that still holds, and what it is waiting on (if anything). Same "
    "rules: facts from the input only, \"you\" for the operator."
)
SCHEMA = {"type": "object", "properties": {"recap": {"type": "string"}, "reentry": {"type": "string"}},
          "required": ["recap", "reentry"], "additionalProperties": False}
REENTRY_LINES, REENTRY_CHARS = 4, 160


def anchor_of(s: dict[str, Any]) -> str:
    t = s.get("last_turn") or {}
    return f"{t.get('idx', -1)}:{int(bool(t.get('complete')))}:{len(s.get('open_asks') or [])}:{s.get('live_status')}"


def _clip(text: str | None, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def build_prompt(s: dict[str, Any]) -> str:
    turns = s.get("turns") or []
    human = [t for t in turns if t.get("user_origin") == "human"]
    lines = [f"Session: {s.get('title') or '-'} ({s.get('harness')} on {s.get('host_id')}, in {s.get('cwd') or '-'})",
             f"State: {s.get('live_status')}"]
    first = (s.get("meta") or {}).get("first_prompt")
    if s.get("purpose"):
        lines.append(f"Purpose: {s['purpose']}")
    elif first:
        lines.append(f"Original goal (first prompt): {_clip(first, 500)}")
    held = [r for r in s.get("ledger") or [] if r.get("kind") == "instruction" and r.get("active")]
    if held:
        lines.append("Your standing instructions:")
        lines += [f"- {_clip(r['text'], 200)}" for r in held[-8:]]
    lines.append("Recent exchanges, oldest first:")
    for t in turns[-6:]:
        who = {"human": "you", "peer": "peer session", "task": "task notification"}.get(t.get("user_origin"), t.get("user_origin") or "?")
        lines.append(f"- #{t['idx']} {who}: {_clip(t.get('user_text'), 600)}")
        if t.get("assistant_text"):
            lines.append(f"  agent: {_clip(t.get('assistant_text'), 900)}")
        for a in t.get("asks") or []:
            lines.append(f"  agent asked [{a['kind']}]: {_clip(a['text'], 300)}" + (f" -> you answered: {_clip(a.get('answer'), 200)}" if a.get("answered") else ""))
        if not t.get("complete"):
            lines.append("  (agent still working on this)")
    if human and human[-1] not in turns[-6:]:
        lines.append(f"Your last prompt: {_clip(human[-1].get('user_text'), 600)}")
    opens = s.get("open_asks") or []
    if opens:
        lines.append("WAITING ON YOU:")
        lines += [f"- [{a['kind']}] {_clip(a['text'], 400)}" for a in opens]
    return "\n".join(lines)


class Recapper:
    def __init__(self, store: Store, seat: SeatConfig, notify: Callable[[], None] = lambda: None, days: int = 7,
                 debounce: float = 4.0, interval: float = 2.0, transport: httpx.AsyncBaseTransport | None = None):
        self.store, self.seat, self.notify = store, seat, notify
        self.days, self.debounce, self.interval = days, debounce, interval
        self.transport = transport
        self._pending: dict[str, tuple[str, float]] = {}       # key -> (anchor, first seen)
        self._backoff_until = 0.0
        # shown by /v1/health (and so `ctx doctor`): the seat, and when it last answered or failed
        self.status: dict[str, Any] = {"seat": seat.url, "model": seat.model, "last_ok": None, "last_error": None, "error_at": None}

    def due(self, now: float | None = None) -> list[str]:
        """Keys whose anchor moved and has held still for ``debounce`` seconds, in priority order."""
        now = time.time() if now is None else now
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.days)).isoformat().replace("+00:00", "Z")
        out = []
        for s in self.store.sessions():
            if not s.get("last_turn"):
                continue
            if s.get("live_status") not in LIVE and (s.get("last_activity") or "") < cutoff:
                continue
            a = anchor_of(s)
            if a == s.get("recap_anchor"):
                self._pending.pop(s["key"], None)
                continue
            seen = self._pending.get(s["key"])
            if seen is None or seen[0] != a:
                self._pending[s["key"]] = (a, now)
                if self.debounce > 0:
                    continue
                seen = (a, now)
            if now - seen[1] >= self.debounce:
                out.append(s["key"])
        return out                                          # store.sessions() order: live first, then most recent

    async def call_seat(self, prompt: str) -> tuple[str, str]:
        body: dict[str, Any] = {
            "model": self.seat.model, "temperature": 0.2, "max_tokens": 400,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "recap", "strict": True, "schema": SCHEMA}},
        }
        if self.seat.disable_thinking:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        async with httpx.AsyncClient(transport=self.transport, timeout=60) as c:
            r = await c.post(self.seat.url.rstrip("/") + "/chat/completions", json=body,
                             headers={"Authorization": f"Bearer {self.seat.api_key}"})
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"] or ""
        out = json.loads(content)
        lines = [_clip(x, REENTRY_CHARS) for x in str(out.get("reentry") or "").splitlines() if x.strip()]
        return _clip(out["recap"], MAX_CHARS), "\n".join(lines[:REENTRY_LINES])

    async def recap_one(self, key: str) -> bool:
        s = self.store.session(key, turns=8)
        if s is None:
            return False
        a = anchor_of(s)
        text, reentry = await self.call_seat(build_prompt(s))
        if not text.strip():
            return False
        self.store.set_recap(key, text, a, reentry or None)
        self.status["last_ok"] = time.time()
        self._pending.pop(key, None)
        self.notify()
        return True

    async def step(self) -> int:
        if time.time() < self._backoff_until:
            return 0
        n = 0
        for key in self.due():
            try:
                n += await self.recap_one(key)
            except Exception as e:                          # noqa: BLE001 - seat down / bad output: keep old recap
                log.warning("recap %s failed: %s", key, e)
                self.status.update(last_error=f"{e.__class__.__name__}: {e}"[:200], error_at=time.time())
                self._backoff_until = time.time() + 30
                break
        return n

    async def run(self) -> None:
        from ctxswitch.hub.tracker.scheduler import run
        await run(self, None, self.interval)
