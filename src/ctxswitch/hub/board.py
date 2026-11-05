"""The board's VIEW MODEL: the hub does the board's thinking, so any front end renders the same board.

The rules live here and nowhere else. They were the Textual board's (client/tui/app.py's ``_visible``, ``_nest``,
``waiting_on``, ``summary_line``, ``render_detail``) and client/fmt.py's; each port below cites the function it came
from, with the same inputs and the same outputs. What stays in fmt is pure text — ages, money, one_line, the glyph
tables — and this module calls it for exactly that, so a JS mod, a TUI plugin and the Textual board all say the same
words about the same session. A front end adds only what the hub cannot know: tmux (which pane is where, how to
reach it) and its own colors beyond the styles named here.

Two answers: :func:`board` (the whole list plus its header) and :func:`detail` (one session's card as sections).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from ctxswitch.client import fmt
from ctxswitch.client.tui import app as tui
from ctxswitch.common.schema import LIVE
from ctxswitch.hub.spend import spend_report
from ctxswitch.hub.tracker.recap import anchor_of
from ctxswitch.hub.store import Store

DEFAULT_TURNS = 8                                      # detail's exchange window (app.load_detail's turns=8)
EARLIER = 6                                            # app.render_detail's earlier exchanges: turns[-7:-1]
CLAIMS = 4                                             # fmt.claims' default in the detail's AGENT SAYS section
DECISIONS = 6                                          # fmt.decisions' default in its DECISIONS section


# -- the rules that were the client's -------------------------------------------------------------------------------

def harness_label(s: dict, profiles_matter: bool) -> str:
    """app.render_table's ``fmt.harness_label(s, self.cfg.claude_profiles)``. The hub has no [[claude_profiles]] table
    (that is the host's config), so the only account name it can know is the one that host's collector put in the
    session's meta — which is what fmt.harness_label uses first anyway. ``profiles_matter`` decides whether a Claude
    session with no name of its own is still a named account rather than plain ``cc``, the same way
    config.profiles_matter does for a board that has the table."""
    if s.get("harness") == "claude":
        named = (s.get("meta") or {}).get("profile")
        if named:
            return str(named)
        if profiles_matter:
            return "default"
    return fmt.HARNESS.get(s.get("harness", ""), s.get("harness", ""))


def profiles_matter(rows: list[dict]) -> bool:
    """Whether a Claude session with no account of its own is still a named one (``default``) rather than plain
    ``cc`` — config.profiles_matter for a board that has the [[claude_profiles]] table. The hub has no such table
    (that is one host's config), so its stand-in is the same fact one level down: that host's collector named the
    account some session on that host ran under. Per host, because it is the host's config that decides, and a
    filtered board must label a session the same way an unfiltered one does."""
    return any((r.get("meta") or {}).get("profile") for r in rows)


def matters_on(store: Store, host: str) -> bool:
    """``profiles_matter`` for one host's rows. A detail card doesn't have the board's list in hand, so this asks
    the question straight of that host's rows: does any of them carry an account its collector named?"""
    return store.db.execute("SELECT 1 FROM sessions WHERE host_id=? "
                             "AND COALESCE(json_extract(meta,'$.profile'),'')!='' LIMIT 1", (host,)).fetchone() is not None


def visible(rows: list[dict], ended: bool, q: str) -> list[dict]:
    """app._visible (over app._pin's list, which is the store's order — pinning is one board's memory of who joined
    when, not a rule about the data): drop ended rows unless asked for them, then keep the ones whose title, key, repo
    or last prompt contains ``q``, casefolded."""
    needle = q.casefold()
    out = []
    for s in rows:
        if not ended and fmt.session_state(s) == "ended":
            continue
        if needle:
            hay = " ".join([fmt.display_title(s), s.get("key", ""), fmt.repo_label(s),
                            (s.get("last_turn") or {}).get("user_text") or ""]).casefold()
            if needle not in hay:
                continue
        out.append(s)
    return out


def nest(rows: list[dict]) -> tuple[list[dict], dict[str, list[dict]]]:
    """app._nest: each session followed by the pool jobs it launched (by ``started_at``). A child whose parent isn't on
    the list (filtered out, ended, unknown) is top level like any other session; only one level nests, so a
    grandchild stays at top level too. Returns the top-level rows in the order they show (the store's, with the
    children left out — they ride along in each row's ``children``) and parent key -> its children."""
    by_key = {s["key"]: s for s in rows}
    kids: dict[str, list[dict]] = {}
    top: list[dict] = []
    for s in rows:
        p = fmt.link(s, "parent_key")
        if p in by_key and p != s["key"] and not fmt.link(by_key[p], "parent_key"):
            kids.setdefault(p, []).append(s)
        else:
            top.append(s)
    return top, {k: sorted(v, key=lambda c: c.get("started_at") or "") for k, v in kids.items()}


def age_at(s: dict) -> str | None:
    """app.render_table's ``when``: how long in this state while it's live, else since it was last active."""
    return s.get("status_since") if s.get("live_status") in LIVE else s.get("last_activity")


def pool(s: dict, host_pools: dict | None) -> dict[str, Any]:
    """fmt.pool_mark / fmt.pool_stale / fmt.pool_line's data behind one badge: what the session believes, what its
    host's switch says, whether those disagree, and the badge's text and style."""
    text, style = fmt.pool_mark(s, host_pools)
    return {"state": fmt._pool_state(s), "host_state": fmt._host_state(s, host_pools),
            "stale": fmt.pool_stale(s, host_pools), "mark": text, "style": style}


def job(s: dict) -> str:
    """app.render_table's ``job``: a nested pool job, or anything that says what job it ran."""
    return fmt.link(s, "job") or fmt.link(s, "job_brief")


def row(s: dict, *, host_pools: dict, profiles_matter: bool, nested: bool, now: datetime,
        kids: list[dict]) -> dict[str, Any]:
    """One board row: everything the board decides about a session, none of how it is painted.

    ``nested`` is whether this row sits under another one (app._nest's ``nested`` set), which is what silences its own
    pool badge, its ◐-age and +N marks and its slot number, and ``kids`` are the children rendered under it."""
    st = fmt.session_state(s)
    ask, why = fmt.reply_target(s)
    why = why or fmt.can_message(s)                    # app.render_detail's "a = answer it here" / why-not line
    target = None if ask is None else {"ref": ask.get("ref"), "kind": ask.get("kind"), "text": ask.get("text")}
    mark, mark_style = ("", "") if nested else fmt.pool_mark(s, host_pools)
    return {
        "key": s.get("key", ""),
        "host_id": s.get("host_id", ""),
        "harness": s.get("harness", ""),
        "session_id": s.get("session_id", ""),
        "title": fmt.display_title(s),                 # app.render_table's title column
        "state": st,
        "glyph": fmt.state_glyph(s),
        "label": harness_label(s, profiles_matter),    # the row's harness·host says the harness this way
        "age_at": age_at(s),                           # the row's age column
        "waiting_on": tui.waiting_on(s),               # the └ line under a waiting row
        "open_asks": len(s.get("open_asks") or []),
        "ask_age": None if nested else _ask_age(s, now),   # the row's ◐<age>: how long its ask has waited
        "unseen": s.get("unseen"),                     # the row's +N: exchanges since you last looked
        "workstream": s.get("workstream"),
        "repo": fmt.repo_label(s),
        "cwd": s.get("cwd"),
        "pid": s.get("pid"),
        "via_pid": s.get("via_pid"),
        "tmux_at": s.get("tmux_at"),
        "job": fmt.link(s, "job"),
        "job_label": (fmt.job_label(s) or None) if (nested or job(s)) else None,
        "cost_mark": fmt.cost_mark(s).strip(),         # the row's trailing ~$0.03 / 17k tok
        "spend": s.get("spend") or {},
        "pool": pool(s, host_pools),
        "pool_mark": mark, "pool_mark_style": mark_style,   # a nested job's badge would say nothing: it inherits
        "nested": nested,
        "can_message": fmt.can_message(s),
        "reply_target": target,
        "reply_why": why,
        "children": [row(c, host_pools=host_pools, profiles_matter=profiles_matter, nested=True, now=now,
                         kids=[]) for c in kids],
        "last_turn": s.get("last_turn"),
        "started_at": s.get("started_at"),
        "last_activity": s.get("last_activity"),
        "live_status": s.get("live_status"),
        "status_since": s.get("status_since"),
        "ended": bool(s.get("ended")),
    }


def _ask_age(s: dict, now: datetime) -> str | None:
    """The row's ◐<age>: how long its oldest open ask has waited (app.render_table's ``ask_mark``)."""
    asks = s.get("open_asks") or []
    return fmt.age(asks[0].get("asked_at"), now) if asks else None


def header(store: Store, top: list[dict], host: str | None, tz: str | None,
           screen_host: str | None, prices: dict[str, dict[str, float]], saint: Any = None,
           price_as: str = "claude-opus-5-5") -> dict[str, Any]:
    """The header's data: app.summary_line's counts (over the top-level rows, as the board's list is), the screen
    host's pool switch, and what has gone since midnight where the operator is."""
    host_pools = store.host_pools()
    budgets = store.budgets()
    left = [b.get("remaining_usd") for b in budgets.values() if b.get("remaining_usd") is not None]
    pct = max([b.get("pct_used") or 0 for b in budgets.values()], default=0)
    # window() is the header's whole time story — local midnight to now — and the one thing a test wants to watch
    frm, to = window(tz) or _today(timezone.utc)
    rep = spend_report(store, frm, to, host, prices, saint, price_as)
    pool_state = None
    if screen_host:
        p = host_pools.get(screen_host) or {}
        pool_state = (str(p.get("state") or "") or None) if isinstance(p, dict) else None
    return {
        "hosts": [r["host_id"] for r in store.hosts()],
        "live": sum(1 for x in top if fmt.session_state(x) != "ended"),      # app.summary_line
        "waiting": sum(1 for x in top if fmt.session_state(x) == "waiting"),
        "pool": pool_state,                                                # fmt.header_pool
        "spend_today": {
            "total_usd": rep.get("total_usd"),
            "cloud_usd": (rep.get("cloud") or {}).get("usd"),
            "local_usd": (rep.get("local") or {}).get("energy_usd"),
            "by_account": [{"account": a.get("account"), "usd": a.get("usd")}
                           for a in (rep.get("cloud") or {}).get("by_account") or []],
            "budget_left_usd": (int(min(left)) if left else None),          # fmt.header_budget, whole dollars
            "budget_pct": pct,
            "budget_style": fmt.budget_style(budgets),
            "from": frm, "to": to,
        },
        "change": store.change,
    }


# -- the two answers ------------------------------------------------------------------------------------------------

def board(store: Store, *, host: str | None = None, ended: bool = False, q: str = "", tz: str | None = None,
          screen_host: str | None = None, prices: dict[str, dict[str, float]] | None = None,
          saint: Any = None, price_as: str = "claude-opus-5-5", price_jobs: Any = None) -> dict[str, Any]:
    """GET /v1/board's body: the rows in the store's order (pool jobs nested one level under whoever launched them),
    filtered as the board filters, and the header's counts, pool switch and today's cost.

    ``price_jobs`` is the hub's own ``_price_jobs`` (a pool job's local estimate needs SAINT's $/token): one call for
    the whole list, so a job's ``~$0.03`` says the same thing here as it does on ``GET /v1/sessions``."""
    prices = prices or {}
    now = datetime.now(timezone.utc)
    rows = store.sessions(host, include_ended=True)
    if price_jobs:
        price_jobs(rows)
    host_pools = store.host_pools()
    # per host, because it is the host's own config that decides, and a filtered board has to label a session
    # exactly as an unfiltered one does
    by_host: dict[str, list[dict]] = {}
    for r in rows:
        by_host.setdefault(r.get("host_id") or "", []).append(r)
    matters = {h: profiles_matter(rs) for h, rs in by_host.items()}
    vis = visible(rows, ended, q)
    top, kids = nest(vis)
    nested = {c["key"] for v in kids.values() for c in v}
    out = [row(s, host_pools=host_pools, profiles_matter=matters.get(s.get("host_id") or "", False),
               nested=s.get("key") in nested, now=now,
               kids=kids.get(s.get("key") or "", [])) for s in top]
    head = header(store, top, host, tz, screen_host, prices, saint, price_as)
    return {"change": store.change, "header": head, "rows": out}


def detail(store: Store, key: str, turns: int = DEFAULT_TURNS, tz: str | None = None,
           prices: dict[str, dict[str, float]] | None = None, saint: Any = None,
           price_as: str = "claude-opus-5-5", price_jobs: Any = None) -> dict[str, Any] | None:
    """GET /v1/board/{key}'s body: app.render_detail's data choices, section by section, none of its text. None for a
    key the store doesn't know (the route turns that into a 404)."""
    s = store.session(key, turns=turns)
    if s is None:
        return None
    prices = prices or {}
    now = datetime.now(timezone.utc)
    host_pools = store.host_pools()
    known: dict[str, bool] = {}

    def label(x: dict) -> str:
        """The harness·host label, from the account naming of the host that session runs on — the same answer the
        board's row for it gives."""
        h = x.get("host_id") or ""
        if h not in known:
            known[h] = matters_on(store, h)
        return harness_label(x, known[h])

    kids = _kids(store, s)
    if price_jobs:
        price_jobs(kids)                               # their ~$ estimates, as the board's rows show them
    trs = s.get("turns") or []
    opens = [x for x in s.get("asks", []) if not x["answered"]]   # app.render_detail's WAITING ON YOU list
    ask, why = fmt.reply_target(s)
    line = fmt.spend_line(s, kids or None)
    last = trs[-1] if trs else {}
    you = next((x for x in reversed(trs) if x.get("user_origin") in ("human", "peer", "shell")), None)
    said = next((x for x in reversed(trs) if x.get("assistant_text")), None)
    new = fmt.since_viewed(s)
    asked = {a["ref"]: a for a in s.get("asks") or []}
    return {
        "key": s.get("key", ""),
        "title": fmt.display_title(s),
        "state": fmt.session_state(s),
        "glyph": fmt.state_glyph(s),
        "recap": {"text": s.get("recap") or "",
                  "reentry_lines": (s.get("reentry") or "").splitlines(),
                  "stale": bool(s.get("recap")) and s.get("recap_anchor") != anchor_of(s)},
        "waiting_on": [{"ref": x.get("ref"), "kind": x.get("kind"), "text": x.get("text"),
                        "asked_at": x.get("asked_at")} for x in opens],
        "reply_target": None if ask is None else {"ref": ask.get("ref"), "kind": ask.get("kind"), "text": ask.get("text")},
        "reply_why": why or fmt.can_message(s),
        "spend": {"line": line, "local_usd": (s.get("spend") or {}).get("local_usd"),
                  "usd": (s.get("spend") or {}).get("usd"), "tokens": (s.get("spend") or {}).get("tokens"),
                  "model": (s.get("spend") or {}).get("model"), "account": (s.get("spend") or {}).get("account")},
        "pool_jobs": [{"key": j.get("key", ""), "label": fmt.job_label(j), "cost_mark": fmt.cost_mark(j).strip(),
                       "status": j.get("job_status") or ""} for j in kids[:6]],
        # render_detail's own age line: how long in this state, falling back to when it last moved (app.py:1101)
        "info": {"age_at": s.get("status_since") or s.get("last_activity"), "turn_count": s.get("turn_count"),
                 "started_at": s.get("started_at"),
                 "last_viewed_at": s.get("last_viewed_at"),
                 "reach_hint": None,                    # reach is the client's: which pane, and how to get there
                 "short_key": fmt.short_key(s), "repo": fmt.repo_label(s), "cwd": s.get("cwd"),
                 "model": ((s.get("meta") or {}).get("model") or "").rsplit("/", 1)[-1],
                 "permission_mode": (s.get("meta") or {}).get("permission_mode"),
                 "pool_line": fmt.pool_line(s, host_pools, now), "label": label(s)},
        "purpose": s.get("purpose"),
        "last_exchange": {
            "you": None if you is None else {"who": fmt.who(you), "at": you.get("started_at"),
                                             "text": you.get("user_text")},
            "agent": None if said is None else {"who": fmt.replied(said),
                                                "at": said.get("ended_at") or said.get("started_at"),
                                                "text": said.get("assistant_text")},
            "tally": {k: v for k, v in (last.get("tally") or {}).items() if v},
            "events": last.get("events") or [],
            "in_progress": bool(trs) and not last.get("complete"),
            "origin": last.get("user_origin"),
        },
        "since_viewed": new,
        "since_line": fmt.since_line(new) if new else "",
        "messages": [{"id": m.get("id"), "status": m.get("status"), "glyph": fmt.MSG_GLYPH.get(m.get("status"), "?"),
                      "body": m.get("body"), "note": fmt.message_note(s, m),
                      "sender": fmt.sender(m) or None,
                      "re": (fmt.one_line(asked[m["ask_ref"]]["text"], 60) if m.get("ask_ref") in asked else None)}
                     for m in fmt.messages(s)],
        "workstream_messages": [{"id": m.get("id"), "body": m.get("body")} for m in s.get("workstream_messages") or []],
        "instructions": [{"n": n, "id": r.get("id"), "text": r.get("text"), "turn_idx": r.get("turn_idx")}
                         for n, r in enumerate(fmt.instructions(s), 1)],
        "claims": [{"glyph": fmt.CLAIM_GLYPH.get((r.get("extra") or {}).get("claim", "other"), "·"),
                    "kind": (r.get("extra") or {}).get("claim", "other"), "text": r.get("text"),
                    "turn_idx": r.get("turn_idx")} for r in fmt.claims(s, CLAIMS)],
        "decisions": [{"question": q_, "answer": a} for _, q_, a in fmt.decisions(s, DECISIONS)],
        "siblings": [{"key": x.get("key", ""), "host_id": x.get("host_id", ""), "harness": x.get("harness", ""),
                      "label": label(x), "title": x.get("title"), "state": _sib_state(x),
                      "glyph": fmt.GLYPH.get(_sib_state(x), "?"),
                      "age_at": x.get("status_since") if _sib_state(x) in LIVE else x.get("last_activity"),
                      "instructions": list(x.get("instructions") or [])} for x in s.get("siblings") or []],
        "earlier": [{"idx": t.get("idx"), "started_at": t.get("started_at"), "origin": t.get("user_origin"),
                     "user_text": t.get("user_text"), "assistant_text": t.get("assistant_text")}
                    for t in (trs[-(EARLIER + 1):-1][::-1] if len(trs) > 1 else [])],
        "workstream": s.get("workstream"),
        "workstream_label": fmt.workstream_label(s.get("workstream")),
        "change": store.change,
    }


def _sib_state(x: dict) -> str:
    """app.render_detail's sibling state: an ended row with no live status left is ``ended``, else its live status."""
    if x.get("ended") and x.get("live_status") not in LIVE:
        return "ended"
    return x.get("live_status") or "unknown"


def _kids(store: Store, s: dict) -> list[dict]:
    """app._kids: the pool jobs ``s`` launched, by when they started. The board's own list would do for a client; the
    hub asks the store, which is the same rows."""
    out = []
    for k in store.children(s["key"]):
        c = store.session(k, turns=0)
        if c:
            out.append(c)
    return sorted(out, key=lambda c: c.get("started_at") or "")


# -- time zones -----------------------------------------------------------------------------------------------------

_OFFSET = re.compile(r"^([+-])(\d{2}):(\d{2})$")


def zone(tz: str | None) -> tzinfo | None:
    """An IANA name or a ±HH:MM offset as a tzinfo; None when it is neither (the route answers 400)."""
    s = (tz or "").strip()
    if not s:
        return timezone.utc                             # unset: UTC, the board's own default
    m = _OFFSET.match(s)
    if m:
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        try:
            return timezone(delta if m.group(1) == "+" else -delta)
        except ValueError:                              # an offset no zone runs on: a 400, not a 500
            return None
    try:
        return ZoneInfo(s)
    except Exception:                                   # noqa: BLE001 - a bad name is a 400, not a 500
        return None


def _stamp(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today(zone_: tzinfo) -> tuple[str, str]:
    """(local midnight, now) as ISO instants: the header's 'today' window, the way the board's own
    today_spend takes midnight where it runs (app.today_spend)."""
    now = datetime.now(timezone.utc).astimezone(zone_)
    return _stamp(now.replace(hour=0, minute=0, second=0, microsecond=0)), _stamp(now)


def window(tz: str | None) -> tuple[str, str] | None:
    """The header's spend window for ``tz``: (local midnight, now) as ISO instants, or None when ``tz`` isn't a zone."""
    z = zone(tz)
    return _today(z) if z else None
