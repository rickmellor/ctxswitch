"""Formatting helpers shared by CLI and TUI: ages, status glyphs, one-line session summaries."""
from __future__ import annotations

from datetime import datetime, timezone

GLYPH = {"busy": "●", "waiting": "◐", "idle": "○", "shell": "$", "unknown": "◌", "ended": "–"}
HARNESS = {"claude": "cc", "pi": "pi", "claude-win": "cw", "agy": "agy", "shell": "sh"}


def harness_label(s: dict, profiles: list | None = None) -> str:
    """The short harness label for a row: ``cc``/``pi``/``cw``, or for a Claude session the name of the
    [[claude_profiles]] account it runs under (``bedrock``, ``max``) when profiles are configured."""
    from ctxswitch.common.config import profile_for, profiles_matter
    if s.get("harness") == "claude":
        named = (s.get("meta") or {}).get("profile")        # its own host's collector named the account
        if named:
            return named
        if profiles and profiles_matter(profiles):
            return profile_for(s, profiles).name
    return HARNESS.get(s.get("harness", ""), s.get("harness", ""))


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def age(s: str | None, now: datetime | None = None) -> str:
    d = parse_iso(s)
    if d is None:
        return "?"
    now = now or datetime.now(timezone.utc)
    sec = max(0, int((now - d).total_seconds()))
    if sec < 60: return f"{sec}s"
    if sec < 3600: return f"{sec // 60}m"
    if sec < 86400: return f"{sec // 3600}h{(sec % 3600) // 60:02d}"
    return f"{sec // 86400}d"


def one_line(text: str | None, width: int) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= width else t[: width - 1] + "…"


def display_title(s: dict) -> str:
    """Title, else the last real prompt; command wrappers never make a title."""
    if s.get("title"):
        return s["title"]
    last = (s.get("last_turn") or {}).get("user_text") or ""
    return "" if last.lstrip().startswith("<") else last


def session_state(s: dict) -> str:
    """busy / waiting / idle / shell / unknown / ended."""
    st = s.get("live_status", "unknown")
    if s.get("ended") and st not in ("busy", "waiting", "idle"):
        return "ended"
    if s.get("open_asks"):
        return "waiting"
    return st


def state_glyph(s: dict) -> str:
    st = session_state(s)
    return "◐" if st == "waiting" else GLYPH.get(st, "?")


def short_key(s: dict) -> str:
    return f"{s.get('host_id','?')}/{HARNESS.get(s.get('harness',''), s.get('harness',''))}/{(s.get('session_id') or '')[:8]}"


def repo_label(s: dict) -> str:
    root = s.get("repo_root") or s.get("cwd") or ""
    name = root.rstrip("/").rsplit("/", 1)[-1] if root else ""
    br = s.get("git_branch") or ""
    return f"{name}@{br}" if name and br and br != "HEAD" else name or "?"


CLAIM_GLYPH = {"done": "✓", "tests_pass": "✓", "tests_fail": "✗", "blocked": "!", "other": "·"}


def instructions(s: dict) -> list[dict]:
    """Your standing instructions to this session that are still in force, oldest first."""
    return [r for r in s.get("ledger") or [] if r["kind"] == "instruction" and r["active"]]


def claims(s: dict, n: int = 4) -> list[dict]:
    """The agent's latest claims (done / tests / blocked), newest last."""
    return [r for r in s.get("ledger") or [] if r["kind"] == "claim"][-n:]


def decisions(s: dict, n: int = 6) -> list[tuple[int, str, str]]:
    """(turn, question, answer), oldest first: answered asks (hard or plain-text) and the choices the tracker read
    from your prompts. A choice that answers one ask is shown once: the ask verbatim, then the choice instead of
    your raw prompt."""
    asks = {a["ref"]: a for a in s.get("asks") or [] if a["answered"]}
    out, merged = [], set()
    for r in s.get("ledger") or []:
        if r["kind"] != "decision":
            continue
        extra = r.get("extra") or {}
        linked = [asks[ref] for ref in extra.get("answers") or [] if ref in asks]
        merged.update(a["ref"] for a in linked)
        q = linked[0]["text"] if len(linked) == 1 else extra.get("question") or ""
        out.append((r["turn_idx"], q, r["text"]))
    out += [(a.get("turn_idx") or 0, a["text"], a.get("answer") or "") for ref, a in asks.items() if ref not in merged]
    return sorted(out, key=lambda d: d[0])[-n:]


MSG_GLYPH = {"queued": "…", "sent": "→", "delivered": "✓", "replied": "↩", "failed": "✗", "cancelled": "–"}
HELD_MODES = ("plan", "bypassPermissions")          # Claude holds an unenveloped peer message for approval in these


def can_message(s: dict) -> str | None:
    """None if a message can be queued for this session, else why not."""
    if s.get("harness") == "claude-win":
        return "Windows-native Claude sessions can't take messages"
    if s.get("harness") == "agy":
        return "Antigravity sessions can't take messages (no input channel yet)"
    if s.get("harness") == "shell":
        return "shells don't take messages"
    return None


def messages(s: dict, n: int = 4) -> list[dict]:
    """The newest messages to this session, yours and other agents' (cancelled ones left out), oldest first."""
    return [m for m in s.get("messages") or [] if m["status"] != "cancelled"][-n:]


def who(t: dict) -> str:
    """Who started a turn: you, the agent session that messaged it, or a peer / task notification."""
    o = t.get("user_origin")
    if o == "shell":
        return "ran"                                   # a shell session: the command you ran
    return "you" if o in ("human", None) else f"from {t['from_key']}" if t.get("from_key") else o


def replied(t: dict) -> str:
    """What answered a turn: the agent, or for a shell command its result (exit code and output)."""
    return "result" if t.get("user_origin") == "shell" else "agent"


def sender(m: dict) -> str:
    """"from <session>: " for another agent's message; yours need no label."""
    return f"from {m['from_key']}: " if m.get("from_key") else ""


def money(x: float | None) -> str:
    """"$1.23", or "—" when it has no price (an unpriced model, or no SAINT for a local estimate)."""
    return f"${x:.2f}" if x is not None else "—"


def _num(x: float | None) -> str:
    """The same without the sign, for where one "$" already covers the line."""
    return f"{x:.2f}" if x is not None else "—"


def tokens_short(n: int | None) -> str:
    """"17.3M" · "262k" · "1.2k" · "41"."""
    n = int(n or 0)
    if n >= 1_000_000: return f"{n / 1e6:.1f}M"
    if n >= 100_000: return f"{n / 1e3:.0f}k"
    if n >= 1_000: return f"{n / 1e3:.1f}k"
    return str(n)


def link(s: dict, key: str):
    """A session's pool-job link (``parent_key`` / ``job`` / ``job_status`` / ``job_brief`` / ``job_turns`` …): the
    hub's own column when it has one, else what the collector put in the session's meta (an older hub)."""
    return s.get(key) or (s.get("meta") or {}).get(key) or ""


def job_label(s: dict) -> str:
    """What a pool job row says instead of its (noisy) title: ``job1-runtime.md · ok · 72 turns``. A session that
    isn't a pool job keeps its title."""
    job, brief = link(s, "job"), link(s, "job_brief")
    if not (job or brief):
        return display_title(s) or ""
    parts = [brief or job, link(s, "job_status"),
             f"{link(s, 'job_turns')} turns" if link(s, "job_turns") else None]
    return " · ".join(str(p) for p in parts if p)


def tok_total(u: dict | None) -> int:
    """input + output + cache_read + cache_creation of a usage dict (the hub's ``tokens`` when it sends one)."""
    return sum(int(v or 0) for v in (u or {}).values())


def cost_mark(s: dict) -> str:
    """The cost at the end of a row: `` ~$0.03`` when it has an estimate — a pool job's local estimate (its tokens at
    SAINT's energy per token), else a priced cloud figure — else the tokens it used."""
    sp = s.get("spend") or {}
    for k in ("local_usd", "usd"):
        if sp.get(k) is not None:
            return f" ~${sp[k]:.2f}"
    tok = sp.get("tokens") or tok_total(s.get("job_tokens") or link(s, "job_tokens"))
    return f" {tokens_short(tok)} tok" if tok else ""


def spend_line(s: dict, children: list | None = None) -> str:
    """One line of what a session cost: its priced cloud use, what it ran locally (SAINT's energy estimate), and how
    many pool jobs it launched. "" when the hub says nothing about cost (an older hub)."""
    sp = s.get("spend") or {}
    parts: list[str] = []
    if sp.get("unpriced"):
        parts.append(f"unpriced model {sp.get('model') or '?'}: {tokens_short(sp.get('tokens'))} tokens")
    elif sp.get("usd") is not None:
        parts.append("cloud " + " · ".join([money(sp["usd"]), f"{tokens_short(sp.get('tokens'))} tokens",
                                            sp.get("model") or "", sp.get("account") or ""]).rstrip(" ·"))
    loc = sp.get("local") or {}
    if loc.get("energy_usd") is not None or loc.get("tokens"):
        parts.append(f"local {money(loc.get('energy_usd'))} energy · {tokens_short(loc.get('tokens'))} tokens"
                     + (f" · {loc['requests']} requests" if loc.get("requests") is not None else ""))
    jobs = children if children is not None else [{} for _ in s.get("children_keys") or []]
    if jobs:
        est = [j.get("spend", {}).get("usd") for j in jobs if isinstance(j, dict)] if children is not None else []
        est = [x for x in est if x is not None]
        parts.append(f"{len(jobs)} pool job{'s' if len(jobs) != 1 else ''}"
                     + (f": {money(sum(est))} local est." if est else ""))
    return " · ".join(parts)


def budget_line(b: dict, now: datetime | None = None) -> str:
    """One account's month against its real limit, as its own quota API states it: the bill, how far in, what's left,
    how long the month has, what happens at the limit, and how old the number is."""
    now = now or datetime.now(timezone.utc)
    acct, cur = b.get("account") or "?", b.get("currency") or "USD"
    spent, limit, left = b.get("spent_usd"), b.get("limit_usd"), b.get("remaining_usd")
    pct = b.get("pct_used")
    head = f"{acct}: {money(spent)} of {money(limit)} this month" + (f" ({pct:.1f}% used)" if pct is not None else "")
    as_of = parse_iso(b.get("as_of"))
    seen = f"as of {age(b.get('as_of'), now)} ago" + (" (stale)" if as_of and (now - as_of).total_seconds() > 2 * 3600 else "")
    parts = [head, f"{money(left)} left" if left is not None else "no limit set", _month_left(b.get("expires_at"), now),
             "blocks at the limit" if b.get("enforcement") == "block" else "alerts at the limit", seen]
    return " · ".join(p for p in parts if p) + (f" ({cur})" if cur != "USD" else "")


def _month_left(expires_at: str | None, now: datetime) -> str:
    """How much month the limit counts: "3 days left", or "ends today" inside a day."""
    d = parse_iso(expires_at)
    if d is None:
        return ""
    days = (d - now).days if d > now else 0
    return "ends today" if days < 1 else f"{days} day{'s' if days != 1 else ''} left"


def budget_style(budgets: dict | None) -> str:
    """How the header's second line is painted: the worst account decides it — dim under 75% used, yellow from 75,
    red from 90 (the limit is a wall there, so the board should look like one)."""
    pct = max([b.get("pct_used") or 0 for b in (budgets or {}).values()], default=0)
    return "bold red" if pct >= 90 else "yellow" if pct >= 75 else "dim"


def header_budget(budgets: dict | None) -> str:
    """The header's short budget tag — the account with the least left, whole dollars: " · $244 left". "" when no
    host has pushed a budget."""
    left = [(b.get("remaining_usd"), b) for b in (budgets or {}).values() if b.get("remaining_usd") is not None]
    if not left:
        return ""
    return f" · ${int(min(left, key=lambda x: x[0])[0])} left"


def header_spend(rep: dict | None, width: int = 10_000) -> str:
    """The header's second line: what today has cost, cloud and local, the accounts in brackets when there's more than
    one and they fit in ``width`` (the pane's); the brackets go first, then the month's budget tag, so the total,
    cloud and local always show."""
    if not rep:
        return "today —"
    cloud, loc = rep.get("cloud") or {}, rep.get("local") or {}
    head, tail = f"today {money(rep.get('total_usd'))} · cloud {_num(cloud.get('usd'))}", f" · local {_num(loc.get('energy_usd'))}"
    accts = cloud.get("by_account") or []
    brackets = " (" + " · ".join(f"{a.get('account') or 'default'} {_num(a.get('usd'))}" for a in accts) + ")" if len(accts) > 1 else ""
    tag = header_budget(rep.get("budget"))
    if len(head) + len(tail) + len(tag) > width:            # too tight even with nothing extra: the month's tag goes too
        return head + tail
    if brackets and len(head) + len(brackets) + len(tail) + len(tag) <= width:
        return head + brackets + tail + tag
    return head + tail + tag


def pool_job_lines(children: list, n: int = 6) -> list[str]:
    """The pool jobs a session launched: what each was for, how it ended, what it cost."""
    out = [f"    ↳ {job_label(j) or j.get('job') or '?'}{cost_mark(j)}" for j in children[:n]]
    if len(children) > n:
        out.append(f"    … {len(children) - n} more")
    return out


def spend_lines(reports: dict[str, dict | None]) -> list[str]:
    """What things cost, per window: totals and accounts, then the pool jobs and sessions of the widest one, then
    the caveats. ``reports`` maps a window's name to its /v1/spend report (None: it couldn't be fetched)."""
    out: list[str] = []
    for title, rep in reports.items():
        if not rep:
            out.append(f"{title:<9} —")
            continue
        cloud, loc = rep.get("cloud") or {}, rep.get("local") or {}
        line = f"{title:<9} {money(rep.get('total_usd'))}   cloud {_num(cloud.get('usd'))}"
        accts = cloud.get("by_account") or []
        if len(accts) > 1:
            line += " (" + " · ".join(f"{a.get('account') or 'default'} {_num(a.get('usd'))}" for a in accts) + ")"
        out.append(line.rstrip())
        out.append(f"{'':<9} local {_num(loc.get('energy_usd'))} energy · {tokens_short(loc.get('tokens'))} tok"
                   f" · {loc.get('requests') or 0} requests" + (f"  ({loc['error']})" if loc.get("error") else ""))
        models = [m for m in cloud.get("by_model") or [] if not m.get("priced", True)]
        if models:
            out.append(f"{'':<9} unpriced: " + " · ".join(f"{m['model']} {tokens_short(m.get('tokens'))} tok" for m in models))
        out.append("")
    budgets = next((r.get("budget") or {} for r in reports.values() if r and r.get("budget")), {})
    if budgets:                                        # the real bill and limit, from the account's own quota API
        out.append("BUDGET")
        now = datetime.now(timezone.utc)
        out += [f"  {budget_line(b, now)}" for _, b in sorted(budgets.items())]
        out.append("")
    wide = max(reports.values(), key=lambda r: len((r or {}).get("jobs") or []), default=None) or {}
    jobs = wide.get("jobs") or []
    out.append(f"POOL JOBS ({len(jobs)})" if jobs else "POOL JOBS")
    for j in jobs[:15]:                              # two lines each: who and how it ended, then what it cost (a 60-column pane)
        out.append(f"  {j.get('job', '?')}  {one_line(j.get('parent_title'), 24)}  {one_line(j.get('status') or '?', 22)}".rstrip())
        turns = j.get("turns") if j.get("turns") is not None else "-"
        out.append(f"      {turns} turns · {_dur(j.get('dur_s'))} · {tokens_short(j.get('tokens'))} tok · "
                   f"~{money(j.get('local_usd'))} local · {money(j.get('if_cloud_usd'))} if cloud")
    wide2 = max(reports.values(), key=lambda r: len((r or {}).get("sessions") or []), default=None) or {}
    sess = wide2.get("sessions") or []
    out.append(f"\nSESSIONS ({len(sess)})" if sess else "\nSESSIONS")
    for x in sess[:15]:
        out.append(f"  {money(x.get('usd')):>7}  {x['key']:<28} {x.get('harness',''):<7} {tokens_short(x.get('tokens')):>7} tok"
                   f"  {one_line(x.get('title'), 40)}")
    unp = sorted({m for r in reports.values() for m in (r or {}).get("unpriced_models") or []}
                 | {x["model"] for r in reports.values() if r for x in (r.get("cloud") or {}).get("by_model") or []
                    if not x.get("priced", True) and x.get("model")})
    rates = next((r.get("rates") for r in reports.values() if r), "default")
    out.append("\n" + (f"unpriced models (tokens only, never $0): {' '.join(unp)}\n" if unp else "")
               + f"rates: {rates} · local = energy estimate from SAINT\n"
                 "estimates at Bedrock on-demand rates; `bedrock-spend` has the bill")
    return out


def _dur(sec: int | None) -> str:
    if sec is None:
        return "-"
    return f"{sec // 60}m" if sec < 3600 else f"{sec / 3600:.1f}h"


WORK_NOTE = "the first session live on this work (any host) takes it"


def message_note(s: dict, m: dict) -> str:
    """What's happening to a message, in words."""
    st = m["status"]
    if st == "queued":
        return "delivering…" if s.get("live_status") in ("busy", "waiting", "idle", "shell") else "queued: delivered when it next runs"
    if st == "sent":
        mode = (s.get("meta") or {}).get("permission_mode")
        if s.get("harness") == "claude" and mode in HELD_MODES:
            return f"sent; Claude holds it for your approval in {mode} mode"
        return "sent; not in the transcript yet"
    if st == "delivered":
        return f"delivered as #{m.get('turn_idx')}, working on it"
    if st == "replied":
        return f"#{m.get('turn_idx')}: " + (m.get("reply") or "(no text reply)")
    if st == "failed":
        return f"failed: {m.get('error') or '?'}"
    return st


REPLYABLE = ("plain", "text")                       # hub/store.py: the asks a message can answer


def reply_target(s: dict) -> tuple[dict | None, str | None]:
    """(the open ask a quick-reply would answer, None) or (None, why there isn't one). The oldest replyable ask; a
    dialog (permission, plan, AskUserQuestion, pi prompt) can only be answered in the session itself."""
    why = can_message(s)
    if why:
        return None, why
    opens = [a for a in s.get("asks") or s.get("open_asks") or [] if not a.get("answered")]
    for a in opens:
        if a["kind"] in REPLYABLE:
            return a, None
    if opens:
        return None, f"it's waiting on a {opens[0]['kind']} dialog: answer it in the session (enter opens it)"
    return None, "nothing to answer: it isn't asking you anything"


def since_viewed(s: dict) -> dict | None:
    """What happened after your last look, from a full session (store.session): None if you never looked or
    nothing has. Ledger rows and asks from exchanges after ``seen_through``, plus the replies to your messages."""
    through = s.get("seen_through")
    if through is None or not s.get("unseen"):
        return None
    rows = [r for r in s.get("ledger") or [] if r["turn_idx"] > through and r["kind"] in ("decision", "claim", "instruction")]
    asks = [a for a in s.get("asks") or [] if (a.get("turn_idx") or 0) > through]
    replies = [m for m in s.get("messages") or [] if m["status"] == "replied" and (m.get("turn_idx") or -1) > through]
    return {"exchanges": s["unseen"], "rows": rows, "asks": asks, "replies": replies}


def since_line(d: dict) -> str:
    """One line: "3 exchanges · 1 question · 2 decisions · 1 claim"."""
    def n(k: int, word: str, plural: str = "") -> str:
        return f"{k} {word if k == 1 else plural or word + 's'}"
    kinds = [r["kind"] for r in d["rows"]]
    parts = [n(d["exchanges"], "exchange")]
    parts += [n(len(d["asks"]), "question")] if d["asks"] else []
    parts += [n(kinds.count(k), k) for k in ("decision", "claim", "instruction") if k in kinds]
    parts += [n(len(d["replies"]), "reply", "replies")] if d["replies"] else []
    return " · ".join(parts)


def workstream_label(ws: str | None) -> str:
    """"owner/name@branch" -> "name@branch"; host:/path/x@b -> "x@b"."""
    if not ws:
        return "(no repo)"
    repo, _, br = ws.rpartition("@")
    name = repo.rstrip("/").rsplit("/", 1)[-1]
    return f"{name}@{br}" if br else name


def show_lines(s: dict, turns: int = 6, n_decisions: int = 8, now: datetime | None = None,
               children: list | None = None) -> list[str]:
    """`ctx show` (and the MCP get_session): meta, what it's for, your instructions, open asks, decisions, messages,
    the same work elsewhere, then the last ``turns`` exchanges. ``children``: the pool jobs it launched (the CLI
    fetches them; without them only their number shows)."""
    now = now or datetime.now(timezone.utc)
    out: list[str] = []
    out.append(f"{state_glyph(s)} {s['key']}   [{session_state(s)} since {age(s.get('status_since') or s.get('last_activity'), now)}]")
    out.append(f"  title : {s.get('title') or '-'}")
    out.append(f"  where : {s.get('cwd') or '-'}  ({repo_label(s)})   model: {s['meta'].get('model') or '-'}  perm: {s['meta'].get('permission_mode') or '-'}")
    out.append(f"  turns : {s.get('turn_count')}   started {age(s.get('started_at'), now)} ago, last activity {age(s.get('last_activity'), now)} ago"
          + (f", viewed {age(s.get('last_viewed_at'), now)} ago" if s.get('last_viewed_at') else ""))
    line = spend_line(s)
    if line:
        out.append(f"  spend : {line}")
    if children:
        out.append(f"  pool jobs ({len(children)}):")
        out += pool_job_lines(children)
    new = since_viewed(s)
    if new:
        out.append(f"  since you looked ({age(s.get('last_viewed_at'), now)} ago): {since_line(new)}")
        for r in new["rows"][-5:]:
            out.append(f"        {r['kind']}: {one_line(r['text'], 100)}")
    if s.get("purpose"):
        out.append(f"  for   : {s['purpose']}")
    if s.get("recap"):
        out.append(f"  recap : {s['recap']}")
    for line in (s.get("reentry") or "").splitlines():
        out.append(f"        › {line}")
    rules = instructions(s)
    if rules:
        out.append("  your instructions:")
        for r in rules:
            out.append(f"    • {one_line(r['text'], 100)}   (#{r['turn_idx']}, ledger id {r['id']})")
    said = claims(s)
    if said:
        out.append("  agent says:")
        for r in said:
            out.append(f"    {CLAIM_GLYPH.get(r['extra'].get('claim'), '·')} {one_line(r['text'], 100)}   (#{r['turn_idx']})")
    opens = [x for x in s.get("asks", []) if not x["answered"]]
    if opens:
        out.append("  OPEN ASKS:")
        for x in opens:
            out.append(f"    ◐ [{x['kind']}] {one_line(x['text'], 100)}   (waiting {age(x.get('asked_at'), now)})")
    decided = decisions(s, n_decisions)
    if decided:
        out.append("  decisions:")
        for _, q, ans in decided:
            out.append(f"    ✓ {one_line(q, 70) + '  →  ' if q else ''}{one_line(ans, 60)}")
    msgs = messages(s, 6)
    if msgs:
        out.append("  messages:")
        for m in msgs:
            out.append(f"    {MSG_GLYPH.get(m['status'], '?')} [{m['id']}] {sender(m)}{one_line(m['body'], 70)}")
            out.append(f"        {one_line(message_note(s, m), 100)}")
    work = s.get("workstream_messages") or []
    if work:
        out.append(f"  for this work ({workstream_label(s.get('workstream'))}), not taken yet:")
        for m in work:
            out.append(f"    {MSG_GLYPH['queued']} [{m['id']}] {one_line(m['body'], 70)}")
            out.append(f"        {WORK_NOTE}")
    sibs = s.get("siblings") or []
    if sibs:
        out.append(f"  same work elsewhere ({workstream_label(s.get('workstream'))}):")
        for x in sibs:
            out.append(f"    {x['key']}  {x.get('live_status')}  {one_line(x.get('title'), 60)}")
            for r in x.get("instructions") or []:
                out.append(f"        told it: {one_line(r, 90)}")
    out.append("  exchanges:")
    for t in s.get("turns", [])[-turns:]:
        flag = "" if t.get("complete") else " (in progress)"
        out.append(f"    #{t['idx']} {age(t.get('started_at'), now):>5} ago [{t.get('user_origin')}]{flag}")
        out.append(f"       {who(t):<5}: {one_line(t.get('user_text'), 110)}")
        if t.get("assistant_text"):
            out.append(f"       {replied(t):<5}: {one_line(t.get('assistant_text'), 110)}")
        tally = t.get("tally") or {}
        ev = t.get("events") or []
        extras = ", ".join(f"{v} {k}" for k, v in tally.items() if v) + ("; " + ", ".join(ev) if ev else "")
        if extras:
            out.append(f"       tools: {extras}")
    return out
