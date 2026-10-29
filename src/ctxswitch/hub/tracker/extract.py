"""Tracker: reads each completed exchange once and extracts the operator ledger rows from it.

Runs in the hub. One seat call per human exchange (your prompt + the agent's reply, with its previous reply for
context), oldest unread first within a session, live sessions first. The call sees the session's active
instructions so it can skip repeats and retire the ones your prompt supersedes. It extracts; it never rewrites:

  instruction  a standing directive from you ("don't push", "use the pool"); active until retired
  decision     a choice you made in this prompt, including an answer to the agent's question
  claim        what the agent says it did: done / tests pass / tests fail / blocked
  ask          a question the agent put to you in plain text that the collector's "ends with ?" rule missed
               (stored in ``asks``, closed by your next prompt; dropped when the turn already has a hard ask)
  purpose      one line, replaced only when the model flags that the goal changed (history kept)

Every row carries a verbatim quote, and a row whose quote is not in the text it came from is dropped: the model
can rephrase in ``text`` but can't invent the evidence. Hard signals (AskUserQuestion, plan approvals, hooks) are
already in ``asks`` and are shown to the model as recorded, so it doesn't extract them twice. Turns that aren't
yours (task notifications, peer messages) are read past without a call.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from ctxswitch.common.config import SeatConfig
from ctxswitch.hub.store import Store, typed

log = logging.getLogger("ctxswitch.extract")

CLAIM_KINDS = ["done", "tests_pass", "tests_fail", "blocked", "other"]
TEXT_CHARS = 3000
MAX_BAD = 3                                         # unparseable replies for one exchange before it is skipped
ROW_CHARS = 200
CAPS = {"instruction": 3, "decision": 3, "claim": 2, "ask": 3}  # per exchange; the prompt asks for fewer

SYSTEM = (
    "You keep an operator's ledger for one coding-agent session. You are given ONE exchange: the operator's prompt "
    "(\"you\") and the agent's reply, plus the agent's previous reply for context, the operator's standing "
    "instructions so far (with ids) and questions already recorded. Extract only what this exchange adds:\n"
    "- instructions: STANDING rules from the operator's prompt about how to work, meant to hold for later tasks "
    "too: e.g. \"don't push without asking\", \"keep IPs out of the repo\", \"use the local pool\", \"work on a "
    "feature branch\". A request for a specific change, feature or fix (\"make the pane narrower\", \"add a recap "
    "line\", \"label it X\") is NOT an instruction. Most prompts contain none. Skip any already listed.\n"
    "- retire: ids of listed instructions that this prompt explicitly cancels or reverses. Usually none.\n"
    "- decisions: a choice the operator makes in this prompt: an approval or rejection, or picking between options "
    "(often answering a question in the agent's previous reply). question = what was being decided, text = the "
    "choice. A new request or a question from the operator is not a decision.\n"
    "- claims: at most 2 per reply: the headline outcome (done or blocked) and the test result if one is stated "
    "(tests_pass / tests_fail). Skip secondary details.\n"
    "- asks: questions the agent's reply puts to the operator that need an answer before it continues (including "
    "\"shall I push?\" at the end). Not rhetorical questions.\n"
    "- goal_changed: true if this prompt changes what the session is for; purpose: one line (max 100 chars) "
    "saying what the session is for, written when goal_changed is true or no purpose is known yet, else \"\".\n"
    "Every item needs quote: an exact, contiguous excerpt (5-30 words, one sentence, no ellipsis) copied character "
    "for character from the operator's prompt (instructions, decisions) or the agent's reply (claims, asks), never "
    "from the previous reply. text may paraphrase in a few words. "
    "Use empty lists when nothing applies; most exchanges add little or nothing."
)

_item = lambda **extra: {"type": "object", "additionalProperties": False,
                         "properties": {"text": {"type": "string"}, "quote": {"type": "string"}, **extra},
                         "required": ["text", "quote", *extra]}
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "instructions": {"type": "array", "items": _item()},
        "retire": {"type": "array", "items": {"type": "integer"}},
        "decisions": {"type": "array", "items": _item(question={"type": "string"})},
        "claims": {"type": "array", "items": _item(kind={"type": "string", "enum": CLAIM_KINDS})},
        "asks": {"type": "array", "items": _item()},
        "goal_changed": {"type": "boolean"},
        "purpose": {"type": "string"},
    },
    "required": ["instructions", "retire", "decisions", "claims", "asks", "goal_changed", "purpose"],
}


def _norm(s: str | None) -> str:
    s = (s or "").lower()
    # markdown emphasis/code marks: the model copies `q` detaches as q detaches, **Tests:** as Tests:
    for a, b in (("‘", "'"), ("’", "'"), ("“", '"'), ("”", '"'), ("…", "..."), ("`", ""), ("*", "")):
        s = s.replace(a, b)
    return " ".join(s.split())


def quoted(quote: str, source: str) -> bool:
    """The quote is really in the source (case, whitespace and curly quotes aside), and says something."""
    q = _norm(quote).strip(" \"'`.…")
    return len(q) >= 8 and q in _norm(source)


def _clip(text: str | None, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


is_yours = typed

def build_prompt(s: dict[str, Any], turn: dict[str, Any], prev: dict[str, Any] | None, active: list[dict[str, Any]],
                 purpose: str | None) -> str:
    lines = [f"Session: {s.get('title') or '-'} (in {s.get('cwd') or '-'})",
             f"Known purpose: {purpose or '(none yet)'}"]
    first = (s.get("meta") or {}).get("first_prompt")
    if first and not purpose:
        lines.append(f"First prompt of the session: {_clip(first, 500)}")
    lines.append("Standing instructions so far:" if active else "Standing instructions so far: (none)")
    lines += [f"- [{r['id']}] {r['text']}" for r in active]
    recorded = [a for a in s.get("asks") or [] if a.get("turn_idx") in (turn["idx"], (prev or {}).get("idx"))]
    if recorded:
        lines.append("Already recorded from the agent's question dialogs (don't extract again):")
        lines += [f"- {_clip(a['text'], 200)}" + (f" -> {_clip(a.get('answer'), 120)}" if a.get("answered") else "") for a in recorded]
    if prev and prev.get("assistant_text"):
        lines.append(f"\nAgent's previous reply (context only):\n{_clip(prev['assistant_text'], TEXT_CHARS // 2)}")
    lines.append(f"\nOperator's prompt:\n{_clip(turn.get('user_text'), TEXT_CHARS)}")
    lines.append(f"\nAgent's reply:\n{_clip(turn.get('assistant_text'), TEXT_CHARS) or '(no text reply)'}")
    return "\n".join(lines)


def validate(out: dict[str, Any], turn: dict[str, Any], prev: dict[str, Any] | None,
             active: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[int], list[dict[str, Any]], int]:
    """(ledger rows, ids to retire, asks, dropped count). Rows must quote the text they claim to come from."""
    you = turn.get("user_text") or ""
    agent = turn.get("assistant_text") or ""
    rows, asks, dropped = [], [], 0
    have = {_norm(r["text"]) for r in active}

    def keep(item: dict, source: str) -> bool:
        nonlocal dropped
        ok = isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip() \
            and isinstance(item.get("quote"), str) and quoted(item["quote"], source)
        dropped += not ok
        return bool(ok)

    for i in (out.get("instructions") or [])[:CAPS["instruction"]]:
        if keep(i, you) and _norm(i["text"]) not in have:
            have.add(_norm(i["text"]))
            rows.append({"kind": "instruction", "text": _clip(i["text"], ROW_CHARS), "quote": i["quote"].strip()})
    for d in (out.get("decisions") or [])[:CAPS["decision"]]:
        if keep(d, you):
            rows.append({"kind": "decision", "text": _clip(d["text"], ROW_CHARS), "quote": d["quote"].strip(),
                         "extra": {"question": _clip(d.get("question"), ROW_CHARS)}})
    for c in (out.get("claims") or [])[:CAPS["claim"]]:
        if keep(c, agent):
            kind = c.get("kind") if c.get("kind") in CLAIM_KINDS else "other"
            rows.append({"kind": "claim", "text": _clip(c["text"], ROW_CHARS), "quote": c["quote"].strip(), "extra": {"claim": kind}})
    for a in (out.get("asks") or [])[:CAPS["ask"]]:
        if keep(a, agent):
            asks.append({"text": _clip(a["text"], ROW_CHARS)})
    if turn.get("asks"):                            # the collector already holds a hard ask for this reply
        asks = []
    ids = {r["id"] for r in active}
    retire = [i for i in out.get("retire") or [] if isinstance(i, int) and i in ids]
    return rows, retire, asks, dropped


class Extractor:
    def __init__(self, store: Store, seat: SeatConfig, notify: Callable[[], None] = lambda: None, days: int = 7,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.store, self.seat, self.notify, self.days = store, seat, notify, days
        self.transport = transport
        self._backoff_until = 0.0
        self._bad: dict[tuple[str, int], int] = {}
        self.status: dict[str, Any] = {"extracted": 0, "dropped": 0, "skipped": 0, "last_ok": None, "last_error": None, "error_at": None, "backlog": 0}

    def cutoff(self) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=self.days)).isoformat().replace("+00:00", "Z")

    async def call_seat(self, prompt: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.seat.model, "temperature": 0.1, "max_tokens": 900,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "ledger", "strict": True, "schema": SCHEMA}},
        }
        if self.seat.disable_thinking:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        async with httpx.AsyncClient(transport=self.transport, timeout=90) as c:
            r = await c.post(self.seat.url.rstrip("/") + "/chat/completions", json=body,
                             headers={"Authorization": f"Bearer {self.seat.api_key}"})
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"] or ""
        out = json.loads(content)
        if not isinstance(out, dict):
            raise ValueError("seat reply is not an object")
        return out

    async def extract_one(self, key: str) -> bool:
        """Read the oldest unread exchange of one session. True when a seat call was made."""
        turn, prev = self.store.next_untracked(key)
        if turn is None:
            return False
        if not is_yours(turn):
            self.store.apply_extraction(key, turn, [], [], [], None)      # nothing of yours to extract
            return False
        s = self.store.session(key, turns=1) or {}
        st = self.store.tracker_state(key)
        active = [r for r in self.store.ledger(key, active_only=True) if r["kind"] == "instruction"]
        try:
            out = await self.call_seat(build_prompt(s, turn, prev, active, st["purpose"]))
        except (ValueError, KeyError, TypeError) as e:     # the seat answered, but not with the schema
            n = self._bad[(key, turn["idx"])] = self._bad.get((key, turn["idx"]), 0) + 1
            if n < MAX_BAD:
                raise
            log.warning("%s #%d: %d bad replies, skipping the exchange: %s", key, turn["idx"], n, e)
            self._bad.pop((key, turn["idx"]), None)
            self.status["skipped"] += 1
            self.store.apply_extraction(key, turn, [], [], [], None)
            return True
        self._bad.pop((key, turn["idx"]), None)
        rows, retire, asks, dropped = validate(out, turn, prev, active)
        purpose = _clip(out.get("purpose"), 120) if (out.get("goal_changed") or not st["purpose"]) else None
        if self.store.apply_extraction(key, turn, rows, retire, asks, purpose or None):
            self.status["extracted"] += 1
            self.status["dropped"] += dropped
            if dropped:
                log.info("%s #%d: dropped %d row(s) whose quote isn't in the text", key, turn["idx"], dropped)
            self.notify()
        self.status["last_ok"] = time.time()
        return True

    async def step(self, calls: int = 1) -> int:
        """Up to ``calls`` seat calls (non-human turns are read past for free)."""
        if time.time() < self._backoff_until:
            return 0
        n = 0
        due = self.store.tracker_due(self.cutoff())
        self.status["backlog"] = len(due)
        for key in due:
            try:
                while n < calls:
                    turn, _ = self.store.next_untracked(key)
                    if turn is None:
                        break
                    n += await self.extract_one(key)
            except Exception as e:                          # noqa: BLE001 - seat down / bad output: retry later
                log.warning("extract %s failed: %s", key, e)
                self.status.update(last_error=f"{e.__class__.__name__}: {e}"[:200], error_at=time.time())
                self._backoff_until = time.time() + 30
                break
            if n >= calls:
                break
        return n
