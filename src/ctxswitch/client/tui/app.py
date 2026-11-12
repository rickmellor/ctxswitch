"""ctx tui — the operator board.

Built as a narrow sidebar (a tmux pane of ~45–60 columns next to the agent sessions), stacked vertically:

  ┌ ctx · <host filter> · N live · N waiting on you ┐
  │ today $4.12 · cloud 3.64 (bedrock 3.1 · max .5) · local .48 │
  ├ sessions, numbered, in the order they joined ─┤
  │ 1 ◐  3m cc·laptop ctxswitch@main  title…      │
  │   └ what it's waiting on                      │
  │   ↳ ◆ cc·laptop job1.md · ok · 72 turns ~$0.03│
  ├ ledger of the selected session ──────────────┤
  │ state / open asks / last exchange / decisions│
  └──────────────────────────────────────────────┘
Keys: ? opens the full list (Help). h host filter · e toggle ended · v mark viewed · enter show/resume · c/p new claude/pi session (asks for a
directory, default = selected session's) · s the start screen (a key per profile, and the ended sessions to resume;
opens by itself on a cold board) · m message the selected session · M send it the tmux paste buffer (after
a y/n) · a answer its open question (a quick-reply message) · t send a prompt that looks misplaced where it looks
like it belongs · o keep such a prompt here · w group by workstream · r refresh · / filter · q quit
Opening a session (enter) marks it viewed; the detail shows what changed since you last looked. New questions pop
up on the tmux status line (client/notify.py; config notify / notify_idle).
Inside tmux, enter swaps the session's pane into pane 0 of this window (see client/tmux.py); `ctx tmux` builds
the layout. Outside tmux it opens a Windows Terminal tab.
Live: WebSocket change notices from the hub; falls back to polling every 10 s.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.screen import ModalScreen, Screen
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import DataTable, Input, Static

from ctxswitch.common.schema import LIVE
from ctxswitch.client import fmt
from ctxswitch.client.hubclient import HubClient
from ctxswitch.client import tmux
from ctxswitch.client import sound
from ctxswitch.client.notify import BOX_ITEMS, MIS_RC_KEEP, MIS_RC_MOVE, OPEN_RC, SETTLE_S, Notifier, item_id, log as popup_log, text as popup_text, tmux_box, tmux_misplaced_box, tmux_popup
from ctxswitch.client.launch import open_tab
from ctxswitch.client import move as mv
from ctxswitch.client.tui.metrics_panel import (METRICS_HUB_MSG, METRICS_NONE_MSG, HostPicker, MetricsPanel,
                                                gpu_count, hosts_text)
from ctxswitch.common.config import Config
from ctxswitch.hub.tracker.recap import anchor_of as _anchor

STATE_STYLE = {"busy": "bold green", "waiting": "bold yellow", "idle": "cyan", "shell": "magenta", "unknown": "dim", "ended": "dim"}
CLAIM_STYLE = {"done": "green", "tests_pass": "green", "tests_fail": "red", "blocked": "yellow"}
MSG_STYLE = {"queued": "yellow", "sent": "yellow", "delivered": "green", "replied": "green", "failed": "red"}
REACH_STYLE = {"pane": "bold", "resume": "", "ssh": "blue", "attach": "bold magenta", "foreign": "red",
               "remote": "blue", "none": "dim"}
SLOTS = 9                                          # Alt+1…9 open the session with that number
METRICS_FILE = Path.home() / ".local/state/ctxswitch/metrics-panel"   # whether g's panel starts shown, and which hosts
METRICS_EVERY = 2                          # seconds between panel fetches (each host's collector samples just as fast)


def slot_key(n: int) -> str:
    """The key Textual reports for Alt+n, asked of its own input parser. It maps ESC+digit to the character macOS's
    Option+digit types (a WezTerm workaround), so Alt+1 arrives as "inverted_exclamation_mark" on every terminal; if
    it ever stops, this is alt+n."""
    from textual import events
    from textual._xterm_parser import XTermParser
    p = XTermParser()
    keys = [e.key for e in [*p.feed(f"\x1b{n}"), *p.feed("")] if isinstance(e, events.Key)]
    return keys[0] if len(keys) == 1 else f"alt+{n}"


STATE_HELP = {"busy": "busy: the agent is working", "waiting": "waiting on you: a question, plan, permission prompt or dialog",
              "idle": "idle: at the prompt, your turn", "shell": "Claude Code reports a shell state",
              "unknown": "live status unknown", "ended": "ended (shown with e)"}
HARNESS_NAME = {"claude": "Claude Code", "pi": "pi", "claude-win": "Windows-native Claude Code", "agy": "Google Antigravity",
                "shell": "shell"}
MSG_HELP = {"queued": "queued: waiting for its host to pick it up", "sent": "sent: written into the session",
            "delivered": "delivered: the agent is working on it", "replied": "replied: that exchange finished",
            "failed": "failed: the reason is shown; send it again", "cancelled": "cancelled"}
CLAIM_HELP = {"done": "the agent says it's done", "tests_pass": "it says tests pass", "tests_fail": "it says tests fail",
              "blocked": "it says it's blocked"}
SUMMARY_TIP = ("The host filter (h), how many sessions are live, and how many are waiting on you, and what today has cost"
               " ($ for the rest), with what's left of the month's budget (the line turns yellow as it runs down, red"
               " near the limit). A waiting session's row says what it's waiting on (└); ○ idle ones finished their turn:"
               " your move. A pool job it launched is nested under it (↳), with its local cost estimate.")


MISPLACE_STYLE = "bold yellow"                         # the ⇄ on a row, and the detail's MISPLACED? heading
MISPLACE_HELP = ("a prompt here may be in the wrong session: the detail says where it looks like it belongs "
                 "(t moves it, o keeps it)")
MISPLACE_TIP = ("A prompt typed into the wrong session's window is caught before the agent sees it: ctxswitch's hook "
                "asks the hub which live session it fits, and blocks it with the reason and where it looks like it "
                "belongs — ↑ Enter re-sends it there, which keeps it here. Subtler ones — a prompt that fits no session, "
                "or one that drifts off what this session is for — are flagged after they land, with ⇄ on the row. The "
                "detail says where it looks like it belongs; t moves it there (or starts a session for it, when it's a "
                "drift) and o keeps it here.")


def misplaced_mark(s: dict) -> str:
    """The ⇄ after a row's marks: it has prompts the hub flagged as belonging to another session. Only the list's
    count is fetched for a session whose detail isn't open, so a detail that did come back decides."""
    return fmt.MISPLACE_GLYPH if (fmt.misopens(s) or int(s.get("misplaced_open") or 0)) else ""


def mis_target_key(row: dict, sessions: list[dict]) -> str:
    """The session a flagged prompt looks like it belongs to, when that session is still on the board ("" for a
    drift, or for a target that has left the list)."""
    t = row.get("target_key") or ""
    return t if row.get("verdict") == "other" and any(s["key"] == t for s in sessions) else ""


def mis_target_title(row: dict, sessions: list[dict]) -> str:
    """That session's name in a sentence: what the hub recorded, else the board's title for it, else its key."""
    t = row.get("target_title") or ""
    if not t:
        t = fmt.display_title(next((x for x in sessions if x["key"] == row.get("target_key")), {})) or row.get("target_key") or ""
    return fmt.one_line(t, 40)


def popup_line(i: dict) -> str:
    """One attention item as a pop-up line: notify's wording for what it knows how to say, and for a misplaced prompt
    ``⇄ <session title>: <what it says>`` — the mark its row carries, then the session the prompt went into."""
    if i.get("kind") != "misplaced":
        return popup_text([i])                          # "ctx · <host · title: what>"
    text = str(i.get("text") or "").removeprefix(fmt.MISPLACE_GLYPH).strip()   # popup_items marks it; say it once
    return f"ctx · {fmt.MISPLACE_GLYPH} {fmt.one_line(i.get('title') or i.get('key'), 30)}: {fmt.one_line(text, 90)}"


def popup_status(items: list[dict]) -> str:
    """The status line for the first item to pop up, the rest counted."""
    return popup_line(items[0]) + (f"  (+{len(items) - 1} more)" if len(items) > 1 else "")


def popup_items(items: list[dict]) -> list[dict]:
    """The same items as the box should print them: a misplaced prompt has no ask or dialog behind it, so it goes as
    one carrying its own text, marked with the ⇄ its row has (notify's own line names the session it went into)."""
    return [{**i, "item": "ask", "text": f"{fmt.MISPLACE_GLYPH} {i.get('text') or ''}"}
            if i.get("kind") == "misplaced" else i for i in items]


def mis_disregard(row: dict) -> str:
    """What the session that got the prompt is told when it moves: drop it, and touch nothing it already did."""
    return ("Disregard my last prompt (\"" + fmt.one_line(row.get("prompt"), 80) +
            "\"): it was meant for another session. Stop any work on it and don't undo anything unless I ask.")


def waiting_on(s: dict) -> str | None:
    """What a waiting session waits on, for the line under its row: its open question, or the dialog it has up.
    None unless it's waiting (an ended one's stale question isn't waiting on you)."""
    if fmt.session_state(s) != "waiting":
        return None
    asks = s.get("open_asks") or []
    if asks:
        a = asks[0]
        return a.get("text") if a.get("kind") in ("plain", "text") else f"{a.get('kind')}: {a.get('text')}"
    return "a dialog is open in it" if s.get("live_status") == "waiting" else None


def summary_line(sessions: list[dict], host: str | None, host_pools: dict | None = None,
                 pool_host: str | None = None) -> Text:
    """The header's first line, with the local-pool switch of ``pool_host`` (the host the header is about: the board's
    own, or the one it's filtered to) on the end when that host reported one."""
    live = sum(1 for x in sessions if fmt.session_state(x) != "ended")
    waiting = sum(1 for x in sessions if fmt.session_state(x) == "waiting")
    t = Text.assemble(("ctx", "bold"), f" · {host or 'all'} · {live} live")
    t.append(f" · {waiting} waiting on you" if waiting else " · nothing waiting", style="bold yellow" if waiting else "dim")
    t.append(fmt.header_pool(host_pools, host if pool_host is None else pool_host), style="green")
    return t


def summary_text(sessions: list[dict], host: str | None, spend: dict | None = None, width: int = 10_000,
                 host_pools: dict | None = None, pool_host: str | None = None) -> Text:
    """The header's two lines: who's on the board (the host's local-pool switch riding along when it reported one),
    and what today has cost (fitted to the pane: the per-account brackets are the first thing to go, the month's
    budget tag next). The second line goes yellow as the month's budget runs down and red once it's nearly gone."""
    t = summary_line(sessions, host, host_pools, pool_host)
    t.append("\n" + fmt.header_spend(spend, width), style=fmt.budget_style((spend or {}).get("budget")))
    return t


def legend_sections() -> list[tuple[str, list[tuple[str, str, str]]]]:
    """Every glyph on the board as (section, [(glyph, style, words)]), from the same tables the board draws with."""
    return [
        ("SESSION STATE (first column)", [(g, STATE_STYLE[st], STATE_HELP[st]) for st, g in fmt.GLYPH.items()]),
        ("ENTER (before the host)", [("▶", "bold", "it's on the left now")] +
         [(g, REACH_STYLE.get(r, ""), tmux.REACH_HELP[r]) for r, g in tmux.REACH_GLYPH.items() if r != "none"]),
        ("ROW", [("1", "dim", "its number: 1…9 selects it here, Alt+1…9 opens it from any pane of the ctx session"),
                 ("3m", "dim", "time in this state (live), or since it was last active (ended)")] +
         [(h, "dim", f"{HARNESS_NAME.get(k, k)}, then its host ({h}·devbox)") for k, h in fmt.HARNESS.items()] +
         [("◐4m", "bold yellow", "a question has waited this long"), ("+2", "bold cyan", "exchanges since you last looked"),
          ("▾", "dim", "a workstream header (w): this repo + branch on any host; m messages the work"),
          ("↳", "dim", ("a pool job launched by the session above (indented under it): its brief, how it ended, its "
                        "turns. It has no number: Alt+N never opens one.")),
          ("~$0.03", "dim", "a job's estimated local cost (energy); the Spend screen ($) has the rest"),
          ("⌂", "bold green", ("local pool on in this session (what it was last told); dim: on but SAINT was "
                               "unreachable when it was told")),
          ("⌂", fmt.POOL_OFF_STYLE, "local pool off in this session (no mark: it was never told, as with pi or a pool job)"),
          ("⌂", "bold yellow", ("stale (with its account in yellow): the session's pool state no longer matches its host's switch — run /local-pool "
                                 "in it, or resume it")),
          (fmt.MISPLACE_GLYPH, MISPLACE_STYLE, MISPLACE_HELP)]),
        ("WAITING ON YOU", [("└", "yellow", ("under a waiting row: what it's waiting on — the question it asked, or the "
                                             "dialog it has up")),
                            ("2 waiting", "bold yellow", "the header: how many sessions wait on you (and how many are live)")]),
        ("SPEND", [("today $4.12", "dim", ("the header's second line: what has gone since midnight here — cloud (with the "
                                           "accounts in brackets), and SAINT's local energy estimate (— when it has none)")),
                   ("cloud $1.23", "dim", ("the detail pane's SPEND section: what this session's own turns cost at Bedrock "
                                           "on-demand rates (`bedrock-spend` has the bill)")),
                   ("local $0.48", "dim", "what it ran on the local box, estimated from its energy use (SAINT)"),
                   ("$244 left", "yellow", ("the header: what's left of the month's budget (the real bill, from "
                                            "bedrock-spend); the line turns yellow at 75% used, red at 90%")),
                   ("$", "bold cyan", "the Spend screen: today, 7 and 30 days by account and model, then every pool job")]),
        ("MESSAGES", [(fmt.MSG_GLYPH[k], MSG_STYLE.get(k, "dim"), MSG_HELP[k]) for k in fmt.MSG_GLYPH]),
        ("AGENT SAYS", [(fmt.CLAIM_GLYPH[k], CLAIM_STYLE.get(k, ""), CLAIM_HELP[k]) for k in CLAIM_HELP]),
    ]


def legend() -> Text:
    t = Text()
    for title, rows in legend_sections():
        t.append(title + "\n", style="bold")
        for glyph, style, words in rows:
            t.append(f"  {glyph:<8}", style=style); t.append(words + "\n")
        t.append("\n")
    return t


def _here(board) -> str | None:
    """Which host a board runs on, or None for a board that never got as far as naming it (a stub in a test): on
    such a board nothing can be called remote, and there is no "here" to name in a picker."""
    return getattr(board, "this_host", None)


def _metrics_state() -> tuple[bool, list[str]]:
    """What the last run left in METRICS_FILE: whether g's panel starts shown, and which hosts it shows. Off and
    showing the automatic host until the file says otherwise — and a file this code never wrote (an older board's
    bare "on"/"off") still says whether it starts shown, with no hosts picked."""
    try:
        raw = METRICS_FILE.read_text().strip()
    except OSError:
        return False, []
    if raw[:1] != "{":
        return raw == "on", []
    try:
        body = json.loads(raw)
    except ValueError:
        return False, []
    hosts = [str(h) for h in (body.get("hosts") or []) if str(h)]
    return bool(body.get("shown")), sorted(dict.fromkeys(hosts))


def _metrics_save(shown: bool, hosts: list[str]) -> None:
    """The panel's two choices, as JSON: whether it starts shown and which hosts it shows next time."""
    try:
        METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
        METRICS_FILE.write_text(json.dumps({"shown": bool(shown), "hosts": sorted(hosts or [])}) + "\n")
    except OSError:
        pass


def split_profile(value: str, names: list[str]) -> tuple[str | None, str]:
    """``"max: ~/repos/x"`` -> ("max", "~/repos/x") when ``max`` is a profile name; else (None, value). A bare
    profile name (no directory) keeps the directory empty, which new_agent turns into ``~``."""
    head, sep, rest = value.partition(":")
    if sep and head.strip() in names:
        return head.strip(), rest.strip()
    if value.strip() in names:
        return value.strip(), ""
    return None, value


# the board's keys, in words: (binding keys, as shown, what it does). A test checks every binding is here.
HELP_KEYS = [
    (("enter",), "Enter", "open the selected session: bring it to the left pane, resume it, or say why not"),
    (("slot",), "Alt+1…9", "open the session with that number, from any pane of the ctx session"),
    (("select_slot",), "1…9", "on the board: select the session with that number, without opening it (Enter, k, m… then act on it)"),
    (("n",), "n", "new session, on this host or another (ssh): pick Claude Code (one entry per account), pi or "
                  "Antigravity or a plain shell, then a directory (default: the selected session's). For Claude, "
                  "'max: directory' also picks the account"),
    (("s",), "s", "start: one key per profile to start new, and the recent sessions to resume (Space marks several, "
                  "Enter resumes them all). Opens by itself when the board starts with nothing live"),
    (("m",), "m", "message the selected session (Enter sends, Esc cancels). On a workstream header: message the work"),
    (("W",), "W", "message the selected session's workstream: whichever session is, or next is, live on it"),
    (("M",), "M", "send it the tmux paste buffer (what you last copied in copy mode), after a y"),
    (("k",), "k", "end the selected session's agent, after a y (it stays resumable: Enter brings it back)"),
    (("a",), "a", "answer the question it's waiting on (plain-text questions; dialogs are answered in the session)"),
    (("t",), "t", "a prompt marked ⇄: move it to the session it looks like it belongs to (a drift: to a new session), after a y"),
    (("o",), "o", "keep the ⇄ prompt where it is (the agent was meant to get it)"),
    (("x",), "x", "exchange: send the selected session elsewhere, picked from a list. To another Claude account: it moves "
                  "(same transcript, resumed with that profile's model). To another agent (pi, Antigravity, Claude): a handoff, "
                  "a document of what it's for, your instructions, decisions and the conversation, which the new agent reads "
                  "first. A running one in ctx's tmux is ended first, after a y"),
    (("i",), "i", "retire one of YOUR INSTRUCTIONS that no longer holds: type its number"),
    (("w",), "w", "group the list by workstream (repo@branch across hosts), or back to a flat list"),
    (("less_than_sign", "greater_than_sign"), "< >", "wider / narrower board (5 columns a press; remembered for the next ctx tmux)"),
    (("h",), "h", "host filter: this host, all hosts, each other host"),
    (("slash", "escape"), "/  Esc", "filter by text (title, repo, key, last prompt); Esc clears it, or cancels an input"),
    (("e",), "e", "show or hide ended sessions"),
    (("v",), "v", "mark the selected session viewed (opening it does too, on any machine)"),
    (("r",), "r", "refresh now (the board also updates live)"),
    (("p",), "p", "pop-ups off / on for this run"),
    (("dollar_sign",), "$", "spend: today's cost, cloud and local, per account, per pool job (Esc, $ or q closes it)"),
    (("g",), "g", "CPU and GPU: a panel of the hosts doing the work (opt-in: a host reports once its collector has "
                  "metrics_every set) — utilization per core, each card's memory, temperature and power; remembered"),
    (("G",), "G", "pick the hosts the CPU/GPU panel shows, several at once (remembered)"),
    (("question_mark",), "?", "this help (Esc, ? or q closes it)"),
    (("q",), "q", "detach: leave tmux, everything keeps running; `ctx tmux` brings it back"),
    (("Q",), "Q", "quit all: end the tmux session and every agent in it, after a y"),
]
HELP_TIPS = [
    ("Where things are", ("The header: how many sessions are live, how many wait on you, and what today has cost. The "
     "list: every session, in the order it joined (so its number stays put); a waiting one says what it's waiting on "
     "(└), and the pool jobs it launched (saint-agent) sit nested under it (↳). Below it: the selected session's detail "
     "(recap, what you told it, its questions and answers, messages, what it cost, the last exchange).")),
    ("Starting cold", ("`ctx tmux` with nothing live opens the Start screen (s) by itself: a digit starts that "
     "profile in a window, and the ended sessions it can bring back are listed under it, newest first. Space marks "
     "several of them and Enter resumes every one, each in its own window.")),
    ("Pool jobs", ("A session started with `saint-agent` records its job id, the session that launched it, its brief, "
     "how it ended and the tokens it used. The board nests it under that session with an estimate of what it cost to "
     "run here; `$` lists the jobs of the last 30 days with what they would have cost in the cloud.")),
    ("Local pool", ("A Claude row with a green ⌂ was told its local pool (SAINT) is on — dim when SAINT couldn't be "
     "reached when it was told — a grey ⌂ that it's off, and a yellow ⌂, its account yellow too, when the switch on its host has since moved the other way, so run /local-pool in it "
     "or resume it. That state comes from the session's own transcript (the SessionStart hook, or a `saint-pool` "
     "command run in it), while the header's `pool on|off|unreachable` is what the switch file on this host says now.")),
    ("Sending a message", ("m types a message into the session as a new prompt: now if it's live, else when it next runs. Its "
     "state shows under MESSAGES (… → ✓ ↩ ✗). `ctx send --cancel <id>` withdraws a queued one.")),
    ("Answering", ("a answers a question the agent asked in its reply. A dialog (permission, plan, AskUserQuestion, pi "
     "prompt) waits on a keypress in its own terminal: Enter opens it.")),
    ("Wrong window?", MISPLACE_TIP),
    ("Agents", ("Agents can read this board and message each other through the ctxswitch MCP tools. Their messages "
     "say which session and host sent them, and never count as yours.")),
    ("Shell sessions", ("n → shell opens a bash that records each command, its exit code and the last lines it "
     "printed, so it's on the list (sh) with a recap like any session. Start a command with a space to keep it out. "
     "Shells don't take messages; an ended one resumes with its history (↻).")),
    ("Ending a session", ("k ends the selected session's agent after a y (the transcript stays: Enter resumes it). "
     "Quitting it yourself (/exit in Claude Code) does the same. Either way the next agent takes its place on the "
     "left and the ended one drops off the list (e shows ended ones). Q ends everything in the ctx session.")),
    ("Other hosts", ("`n` and `s` can start on another host the config's [ssh] table reaches: it runs in a tmux named "
     "ctx on that host, as its own user, so it keeps running when the ssh drops or the laptop sleeps — the window "
     "here is only a view of it. A ⇄ row is live over there: Enter shows it here over ssh (detach and it stays put), "
     "and `k` ends it there, its transcript left resumable. A host ssh can't reach is still on the board and takes "
     "messages; it just can't be opened from here.")),
    ("CPU and GPU", ("`g` puts a panel between the list and the detail showing what the hosts doing the work are made "
     "of: their CPU by core, and each card's utilization, memory, temperature and power. It's opt-in per host — a "
     "host only reports once its collector has `metrics_every` set (default 0, sampling nothing), and the panel says "
     "so when nobody does. `G` picks which hosts the panel stacks, several at once, remembered for next time; "
     "picked, a host that stopped reporting still gets one line saying it has no samples. Left unpicked it shows the "
     "one host `metrics_host` names, or else the one with the most GPUs, which is the one doing the work. Each "
     "collector samples every 2 s and the hub keeps the last sample and the last minute of history per host.")),
    ("Restarting the board", ("`ctx tmux --restart` restarts it on new code; your agents keep running. If the board's "
     "pane is ever gone, `ctx tmux` puts it back; if the left pane is (the board alone in its window), the board "
     "puts the most recent session back there, or a shell.")),
    ("More", "`ctx --help`, and the README (User guide)."),
]


def help_sections() -> list[tuple[str, list[tuple[str, str, str]]]]:
    """The ? screen as (section, [(key or glyph, style, words)]): keys, then every glyph, then how things work."""
    keys = [(shown, "bold cyan", words) for _, shown, words in HELP_KEYS]
    tips = [(title.upper(), [("", "", words)]) for title, words in HELP_TIPS]
    return [("KEYS", keys), *legend_sections(), *tips]


def help_renderable():
    """Each section a two-column grid, so a wrapped line stays under its words in a narrow pane."""
    from rich.console import Group
    from rich.table import Table
    parts: list = [Text.assemble(("ctxswitch help", "bold"), ("   ↑↓ PgUp PgDn scroll · Esc closes\n", "dim"))]
    for title, rows in help_sections():
        parts.append(Text(title, style="bold"))
        g = Table.grid(padding=(0, 1), pad_edge=True)
        g.add_column(width=10 if any(k for k, _, _ in rows) else 0, no_wrap=True); g.add_column(ratio=1)
        for k, style, words in rows:
            g.add_row(Text(k, style=style), words)
        parts += [g, Text("")]
    return Group(*parts)


def help_text() -> str:
    return "\n".join(f"{title}\n" + "\n".join(f"  {k}  {w}" for k, _, w in rows) for title, rows in help_sections())


def row_tip(s: dict, reach: str, on_left: bool, ssh: dict, now: datetime) -> str:
    """One row of the list in words, for its hover tooltip."""
    st = fmt.session_state(s)
    live = s.get("live_status") in LIVE
    when = fmt.age(s.get("status_since") if live else s.get("last_activity"), now)
    lines = [f"{fmt.state_glyph(s)} {STATE_HELP.get(st, st)}, {when}" if live else f"{fmt.state_glyph(s)} {STATE_HELP.get(st, st)}, last active {when} ago"]
    asks = s.get("open_asks") or []
    if asks:
        lines.append(f"◐ [{asks[0].get('kind')}] waiting {fmt.age(asks[0].get('asked_at'), now)}: {fmt.one_line(asks[0].get('text'), 80)}")
    if on_left:
        lines.append("▶ on the left now")
    elif reach and reach != "none":
        lines.append(f"{tmux.REACH_GLYPH[reach]} {tmux.reach_help(s, reach, ssh)}")
    if s.get("unseen"):
        lines.append(f"+{s['unseen']} exchange{'s' if s['unseen'] != 1 else ''} since you last looked")
    if misplaced_mark(s):
        lines.append(f"{fmt.MISPLACE_GLYPH} a prompt here may belong to another session: the detail says where (t, o)")
    lines.append(f"{HARNESS_NAME.get(s.get('harness', ''), s.get('harness', ''))} on {s.get('host_id', '?')} · {fmt.repo_label(s)}")
    if s.get("continues"):
        lines.append(f"continues {s['continues']} (handoff)")
    for k in s.get("continued_by") or []:
        lines.append(f"handed off to {k}")
    if s.get("handed_off"):
        lines.append(f"handoff to {s['handed_off'].get('to')} started, waiting for the new session")
    return "\n".join(lines)


class Board(DataTable):
    """The session list, with a tooltip that follows the row under the mouse (a DataTable has one tooltip, not one
    per row)."""

    def watch_hover_coordinate(self, old, value) -> None:
        super().watch_hover_coordinate(old, value)
        try:
            key = self.coordinate_to_cell_key(value).row_key.value
        except Exception:                               # noqa: BLE001 - off the rows
            key = None
        self.tooltip = self.app.tip_for(key) if key else None


KEY_NAMES = {"enter": "↵", "slash": "/", "question_mark": "?", "dollar_sign": "$"}


def key_bar(bindings, width: int) -> Text:
    """The board's shown keys, wrapped onto as many lines as ``width`` needs (Textual's Footer is one line and cuts
    off what doesn't fit). A key is never split from its label."""
    t, used = Text(), 0
    for b in bindings:
        if not b.show:
            continue
        key, label = KEY_NAMES.get(b.key, b.key), b.description
        n = len(key) + 1 + len(label)
        if used and used + 1 + n > width:
            t.append("\n"); used = 0
        elif used:
            t.append(" "); used += 1
        t.append(key, style="bold cyan"); t.append(" " + label)
        used += n
    return t


class KeyBar(Static):
    def render(self) -> Text:
        return key_bar(self.app.BINDINGS, max(10, self.content_size.width))


class Confirm(ModalScreen):
    """A y/n that takes one key: y (or Y) says yes at once, any other key no."""
    DEFAULT_CSS = """
    Confirm { align: center bottom; background: $background 30%; }
    Confirm > Static { width: 100%; height: auto; border: round $warning; padding: 0 1; background: $surface; }
    """

    def __init__(self, prompt: str):
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        yield Static(Text.assemble((self.prompt, "bold"), ("  y = yes, any other key = no", "dim")))

    def on_key(self, ev) -> None:
        ev.stop(); ev.prevent_default()
        self.dismiss(ev.key in ("y", "Y"))


class Picker(ModalScreen):
    """n: which agent to start. One key: the option's number; Esc cancels. Dismisses (harness, profile) or None."""
    DEFAULT_CSS = """
    Picker { align: center bottom; background: $background 30%; }
    Picker > Static { width: 100%; height: auto; border: round $accent; padding: 0 1; background: $surface; }
    """

    def __init__(self, options: list[tuple[str, str, str | None]], title: str = "new session: "):
        super().__init__()
        self.options = options                          # (label, harness, profile)
        self.title_text = title

    def compose(self) -> ComposeResult:
        t = Text(self.title_text, style="bold")
        for n, (label, _, _) in enumerate(self.options, 1):
            t.append(f"{n} ", style="bold"); t.append(f"{label}   ")
        t.append("Esc cancels", style="dim")
        yield Static(t)

    def on_key(self, ev) -> None:
        ev.stop(); ev.prevent_default()
        if ev.key.isdigit() and 1 <= int(ev.key) <= len(self.options):
            _, harness, profile = self.options[int(ev.key) - 1]
            self.dismiss((harness, profile))
        elif ev.key == "escape":
            self.dismiss(None)


def new_session_options(cfg: Config, which=None) -> list[tuple[str, str, str | None]]:
    """What n offers: Claude Code per [[claude_profiles]] account, then pi and Antigravity when installed."""
    import shutil
    from ctxswitch.common.config import profiles_matter
    which = which or shutil.which
    out: list[tuple[str, str, str | None]] = []
    named = profiles_matter(cfg.claude_profiles)
    for p in cfg.claude_profiles:
        cmd = p.command[0] if p.command else "claude"
        if which(cmd) or os.path.isabs(cmd):
            out.append((f"claude · {p.name}" if named else "claude", "claude", p.name if named else None))
    if which("pi"):
        out.append(("pi", "pi", None))
    if which("agy"):
        out.append(("antigravity", "agy", None))
    return out


class Help(Screen):
    """? : the whole pane, scrollable; Esc (or ? or q) closes it."""
    BINDINGS = [Binding("escape", "close", show=False), Binding("question_mark", "close", show=False),
                Binding("q", "close", show=False)]
    DEFAULT_CSS = """
    Help > VerticalScroll { padding: 0 1; }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help"):            # no footer: it would list the board's keys behind this screen
            yield Static(help_renderable())

    def action_close(self) -> None:
        self.app.pop_screen()


def spend_windows(client, host: str | None) -> dict[str, dict | None]:
    """Today, 7 days and 30 days of /v1/spend, in one call from a worker thread: a window that fails is None (an
    older hub has no such route at all: all three are)."""
    now = datetime.now(timezone.utc)
    local = now.astimezone()
    out: dict[str, dict | None] = {}
    for title, frm in (("Today", local.replace(hour=0, minute=0, second=0, microsecond=0)),
                       ("7 days", now - timedelta(days=7)), ("30 days", now - timedelta(days=30))):
        try:
            out[title] = client.spend(frm.isoformat(), now.isoformat(), host)
        except Exception:                               # noqa: BLE001 - the screen says "—" for what has no report
            out[title] = None
    return out


class SpendScreen(Screen):
    """$ : what things cost — three windows by account and model, then the pool jobs and dearest sessions. Esc (or
    $ or q) closes it. The reports arrive from a worker thread; until then it says it's loading."""
    BINDINGS = [Binding("escape", "close", show=False), Binding("dollar_sign", "close", show=False),
                Binding("q", "close", show=False)]
    DEFAULT_CSS = """
    SpendScreen > VerticalScroll { padding: 0 1; }
    """

    def __init__(self, reports: dict[str, dict | None] | None = None):
        super().__init__()
        self.reports = reports

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="spend"):
            yield Static(self.body(), id="spend_text")

    def body(self) -> Text:
        if self.reports is None:
            return Text("loading…", style="dim")
        t = Text.assemble(("spend", "bold"), ("   Esc closes\n", "dim"))
        t.append("\n".join(fmt.spend_lines(self.reports)))
        return t

    def loaded(self, reports: dict[str, dict | None]) -> None:
        self.reports = reports
        try:
            self.query_one("#spend_text", Static).update(self.body())
        except Exception:                               # noqa: BLE001 - closed while the thread was out
            pass

    def action_close(self) -> None:
        self.app.pop_screen()


START_KEYS = "↑↓ move · Space mark · Enter resume marked (or this one) · 1-9 start new · Esc close"


REMOTE_START_OPTIONS = [("claude", "claude", None), ("pi", "pi", None), ("shell", "shell", None)]


class StartScreen(Screen):
    """s, and on its own when the board starts with nothing live: one key to start each profile you have, and the
    ended sessions this board can bring back, several at a time (Space marks, Enter resumes them all). Dismisses
    ("new", harness, profile, cwd) — with a fifth element, the host, when it's to start on another one — or
    ("resume", [keys]), or None. Every key is handled here: none of the board's (s, q, the arrows, k) reaches
    through this screen."""
    DEFAULT_CSS = """
    StartScreen > VerticalScroll { padding: 0 1; }
    """

    def __init__(self, options: list[tuple[str, str, str | None]], recents: list[dict], reach: dict[str, str],
                 now: datetime | None = None, hosts: list[str] | None = None):
        super().__init__()
        self.options = options                          # (label, harness, profile), as n's picker has them
        self.recents = recents                          # ended sessions, newest activity first
        self.reach = reach                              # key -> how Enter can reach it
        self.now = now or datetime.now(timezone.utc)
        self.hosts = list(hosts or [])                  # this host first, then the others an ssh route reaches
        self.cursor = 0
        self.marked: set[str] = set()
        self.host_i = 0                                 # which of those a digit starts on

    @property
    def host(self) -> str | None:
        """None while the screen starts on this host (its own profiles); another host's id once `h` has cycled onto
        it, which is all a start there can pick from: claude, pi, shell."""
        return self.hosts[self.host_i] if self.host_i else None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="start"):
            yield Static(self.body(), id="start_text")

    def _cwd(self) -> str | None:
        """The directory a new session starts in: the cursor row's, so a start lands where that work was."""
        return (self.recents[self.cursor].get("cwd") or None) if self.recents else None

    def _opts(self) -> list[tuple[str, str, str | None]]:
        """What a digit starts: this host's profiles, or the harnesses the other host can run (its profiles are its
        own, so this side can't name them)."""
        return REMOTE_START_OPTIONS if self.host else self.options

    def body(self) -> Text:
        opts = self._opts()
        t = Text("START")
        if len(self.hosts) > 1:                             # other hosts an ssh route reaches: say which one a digit hits
            t = Text(f"START on {self.hosts[self.host_i]}")
            t.append("   h: another host", style="dim")
        t.append("\n")
        o = Text()
        for n, (label, _, _) in enumerate(opts, 1):
            if n > 1:
                o.append("   ")
            o.append(f"{n} ", style="bold").append(label)
        t.append(o)
        t.append("\n")
        t.append(f"RECENT ({len(self.recents)})\n" if self.recents else "RECENT — nothing ended to resume\n", style="bold")
        width = max(20, (self.size.width or 60) - 8)    # the pane's columns, less the "> " and the "[x] " mark
        for i, s in enumerate(self.recents):
            mark = s["key"] in self.marked
            t.append("> " if i == self.cursor else "  ", style="reverse" if i == self.cursor else "")
            t.append("[x] " if mark else "[ ] ", style="bold cyan" if mark else "dim")
            t.append(fmt.recent_line(s, self.reach, self.now, width) + "\n",
                     style=STATE_STYLE.get("ended", "dim"))
        t.append(START_KEYS + "\n", style="dim")
        return t

    def redraw(self) -> None:
        try:
            self.query_one("#start_text", Static).update(self.body())
        except Exception:                               # noqa: BLE001 - closed mid-key
            pass

    def on_key(self, ev) -> None:
        ev.stop(); ev.prevent_default()                # nothing here is the board's key, not even s or q
        if ev.key in ("up", "k"):
            self.cursor = max(0, self.cursor - 1)
        elif ev.key in ("down", "j"):
            self.cursor = min(max(0, len(self.recents) - 1), self.cursor + 1)
        elif ev.key == "space":
            if self.recents:
                key = self.recents[self.cursor]["key"]
                self.marked ^= {key}
        elif ev.key == "enter":
            keys = [s["key"] for s in self.recents if s["key"] in self.marked] or \
                   ([self.recents[self.cursor]["key"]] if self.recents else [])
            self.dismiss(("resume", keys) if keys else None)
            return
        elif ev.key in ("escape", "q", "s"):
            self.dismiss(None)
            return
        elif ev.key == "h" and len(self.hosts) > 1:
            self.host_i = (self.host_i + 1) % len(self.hosts)            # this host, then each one ssh reaches
        elif ev.key.isdigit() and 1 <= int(ev.key) <= len(self._opts()):
            _, harness, profile = self._opts()[int(ev.key) - 1]
            # another host: no profile and no directory of ours to carry over (its own filesystem, its own accounts)
            self.dismiss(("new", harness, profile, None, self.host) if self.host
                         else ("new", harness, profile, self._cwd()))
            return
        else:
            return
        self.redraw()

    def action_close(self) -> None:
        self.dismiss(None)


class CtxApp(App):
    CSS = """
    #summary { height: 2; padding: 0 1; background: $panel; }
    #body { height: 1fr; }
    #table { height: auto; max-height: 50%; }
    #detail { height: 1fr; border-top: solid $surface; padding: 0 1; }
    #filter { display: none; }                 /* in the flow above the footer, so the footer never covers it */
    #filter.visible { display: block; }
    #metrics { display: none; height: auto; max-height: 50%; padding: 0 1; border-top: solid $surface; }
    #metrics.visible { display: block; }
    .h { color: $text-muted; }
    #keys { height: auto; background: $panel; padding: 0 1; }
    """
    # the key bar (key_bar) wraps in the narrow pane: most-used first, short labels
    BINDINGS = [
        Binding("enter", "resume", "open", show=True), Binding("n", "new_session", "new"),
        Binding("s", "start", "start"),
        Binding("m", "message", "msg"), Binding("a", "reply", "answer"), Binding("M", "message_buffer", show=False),
        Binding("t", "misplace_move", "move it", show=False), Binding("o", "misplace_keep", "keep it", show=False),
        Binding("k", "end_session", "end"), Binding("q", "detach", "detach"), Binding("Q", "kill_all", "quit all"),
        Binding("h", "cycle_host", "filter host"), Binding("slash", "filter", "find"), Binding("e", "toggle_ended", "ended"),
        Binding("v", "mark_viewed", "viewed"), Binding("w", "toggle_ws", "group"), Binding("r", "refresh", "refresh"),
        Binding("p", "toggle_popups", "pop-ups"), Binding("x", "exchange", "exchange"), Binding("i", "retire", "retire"),
        Binding("dollar_sign", "spend", "spend"), Binding("g", "metrics", "gpu"),
        Binding("question_mark", "help", "help"),
        Binding("G", "metrics_host", show=False),                      # G: pick the hosts the CPU/GPU panel stacks
        Binding("W", "message_work", "msg work", show=False),
        Binding("less_than_sign", "wider", show=False), Binding("greater_than_sign", "narrower", show=False),   # the edge moves the arrow's way
        Binding("escape", "clear_filter", show=False),
        *(Binding(f"{slot_key(n)},alt+{n}", f"slot({n})", show=False) for n in range(1, SLOTS + 1)),
        *(Binding(str(n), f"select_slot({n})", show=False) for n in range(1, SLOTS + 1)),   # a plain digit only selects
    ]

    def __init__(self, cfg: Config, host: str | None):
        super().__init__()
        self.cfg = cfg
        self.client = HubClient(cfg)
        self.this_host = cfg.host_id or socket.gethostname()
        self.host_filter: str | None = host          # None = all
        self.show_ended = False
        self.text_filter = ""
        self.sessions: list[dict] = []
        self.hosts: list[str] = []
        self.host_pools: dict[str, dict] = {}          # host -> its latest local-pool switch report ({state, url, …})
        self.selected: str | None = None
        self.change = -1
        self.visible_key: str | None = None          # session shown in the tmux viewer pane
        self.on_screen: set[str] = set()             # sessions in the tmux window being looked at (no pop-ups for them)
        self.reach: dict[str, str] = {}             # key -> tmux.REACH_GLYPH key (how Enter can open it)
        self.input_mode = "filter"                   # what the bottom Input is for: "filter" | "new:<harness>" | "msg:<key>" | …
        self.pending_reply: tuple[str, str] | None = None   # (session key, ask ref) the reply input answers
        self.pending_rules: tuple[str, list[int]] | None = None   # (session key, row ids by number) x retires one of
        self.pending_prompt: tuple[dict, dict] | None = None      # t: (the session a drifted prompt landed in, the row)
        self.slots: list[str] = []                   # session keys by their row number, for Alt+N
        self.pins: dict[str, int] = {}               # session key -> its place in the list, fixed when it joined
        self.next_pin = 0
        self.detail: dict | None = None              # the session shown in the detail pane
        self.group_ws = False                        # w: rows grouped under workstream headers
        self.notifier = Notifier(cfg.notify_idle)
        self.popups = cfg.notify                     # n toggles it for this run; off still tracks what's been seen
        self.box = None                              # our open tmux popup box (a display-popup process), if any
        self.attention: list[dict] = []              # the latest attention list, for the pop-up settle timer
        self.settling = False
        self.spend: dict | None = None               # /v1/spend since local midnight; None = the hub can't say
        self.start_shown = False                     # the Start screen has opened itself once this run (s is yours)
        self.metrics_on, self.metrics_picked = _metrics_state()   # g: shown or not, and which hosts, as the last run left them
        self.metrics_timer = None                    # the 2 s poll, running only while the panel is shown
        self.metrics_names: list[str] = []           # the hosts that reported, sorted: what G lists to pick from
        self.metrics_counts: dict[str, int] = {}     # host -> cards in its last sample: what G counts after its name
        self.metrics_picking = False                 # G pressed before the hub had answered: open the picker on it

    def compose(self) -> ComposeResult:
        yield Static("", id="summary")
        with Vertical(id="body"):
            yield Board(id="table", cursor_type="row", zebra_stripes=True, show_header=False)
            yield VerticalScroll(Static("", id="detail_text"), id="detail")
        inp = Input(placeholder="filter sessions (title, repo, key)…", id="filter")
        inp.can_focus = False                     # only focusable while shown via "/"
        yield inp
        # g's CPU/GPU panel, pinned to the bottom right above the keys: the body above keeps 1fr, so the detail
        # pane stays a scrolling region that simply ends where the panel starts
        yield MetricsPanel("", id="metrics")
        yield KeyBar(id="keys")

    def on_mount(self) -> None:
        t = self.query_one("#table", DataTable)
        t.add_columns("#", " ", "age", "session")
        self.title = "ctxswitch"
        self.query_one("#summary", Static).tooltip = SUMMARY_TIP
        self.join_tmux()
        t.focus()
        if self.metrics_on:
            self.show_metrics()                             # the last run left the panel up: it comes back up
        self.refresh_data()
        self.set_interval(10, self.refresh_data)
        self.listen_ws()

    # -- data -----------------------------------------------------------------------------------------------------
    @work(exclusive=True, thread=True)
    def refresh_data(self) -> None:
        try:
            data = self.client.sessions(self.host_filter, live_only=False)
            if self.host_filter and self.host_filter == self.this_host:
                data["sessions"] += self.driven_from_here(data["sessions"])
            att = self.client.attention(self.host_filter)["items"]
            rows = self.client.hosts()
            hosts = [h["host_id"] for h in rows]
            pools = {h["host_id"]: h["pool"] for h in rows if h.get("pool")}   # a hub without pool events: {}
            spend = self.today_spend()                  # never raises: an older hub simply has no spend line
        except Exception as e:                          # noqa: BLE001
            self.call_from_thread(self.sub_title_set, f"hub unreachable: {e.__class__.__name__}")
            return
        try:
            said = tmux.restore_viewer()                # the left pane is gone (closed, its wrapper killed): put one back
            if said:
                popup_log(f"viewer: {said}")
            tmux.reap_viewer()                          # an agent exited to a bare shell in the viewer: move on
            ps = tmux.panes()
            self.visible_key = tmux.visible_key(ps, sessions=data["sessions"])
            self.on_screen = tmux.on_screen_keys(ps, sessions=data["sessions"])
            self.reach = tmux.reach_all(data["sessions"], self.this_host, self.cfg.ssh)
        except Exception:                               # noqa: BLE001 - tmux is optional
            self.visible_key = None
            self.on_screen = set()
            self.reach = {}
        self.call_from_thread(self.apply_data, data["sessions"], att, hosts, data["change"], spend, True, pools)

    def driven_from_here(self, have: list[dict]) -> list[dict]:
        """With the board on this host: the live sessions on other hosts that are run from here too — in that host's
        ctx tmux (what n/s and resume start there), or with a window in this tmux. Other tmux sessions over there
        stay under that host's own filter."""
        try:
            others = self.client.sessions(None, live_only=True)["sessions"]
            mine = {p.key for p in tmux.panes() if p.key}
        except Exception:                               # noqa: BLE001 - extras, never worth failing a refresh for
            return []
        seen = {s["key"] for s in have}
        return [s for s in others if s["key"] not in seen and s.get("host_id") != self.this_host
                and ((s.get("tmux_at") or "").split(":", 1)[0] == tmux.REMOTE_SESSION or s["key"] in mine)]

    def today_spend(self) -> dict | None:
        """What has gone since midnight here, for the header. A hub without /v1/spend (or a SAINT that's down) just
        leaves the header at \"today —\"."""
        now = datetime.now(timezone.utc)
        try:
            return self.client.spend(now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
                                     now.isoformat(), self.host_filter)
        except Exception:                               # noqa: BLE001 - cost is never worth breaking a refresh for
            return None

    def sub_title_set(self, s: str) -> None:
        self.sub_title = s

    def apply_data(self, sessions: list[dict], attention: list[dict], hosts: list[str], change: int,
                   spend: dict | None = None, first_load: bool = False, host_pools: dict | None = None) -> None:
        self.change = change
        self.hosts = hosts
        self.host_pools = host_pools or {}
        self.sessions = sessions
        self.spend = spend
        live = sum(1 for x in sessions if fmt.session_state(x) != "ended")
        self.sub_title = f"{self.host_filter or 'all'} · {live} live"
        head = self.query_one("#summary", Static)
        head.update(summary_text(sessions, self.host_filter, spend, max(20, (head.size.width or self.size.width) - 2),
                                 self.host_pools, self.this_host))
        self.popup(attention)
        self.render_table()
        self.render_detail()
        if first_load and not self.start_shown:           # once, on the board's first load: a cold start says what to start
            self.start_shown = True
            if tmux.in_tmux() and not live and len(self.screen_stack) == 1:
                self.action_start()

    @work(exclusive=True, group="ws")
    async def listen_ws(self) -> None:
        try:
            import websockets
        except ImportError:
            return
        url = self.client.ws_url()
        while True:
            try:
                async with websockets.connect(url, open_timeout=5) as ws:
                    async for msg in ws:
                        d = json.loads(msg)
                        if "change" in d and d["change"] != self.change:
                            self.refresh_data()
            except Exception:                           # noqa: BLE001 - reconnect quietly
                await asyncio.sleep(5)

    # -- rendering ------------------------------------------------------------------------------------------------
    def popup(self, attention: list[dict]) -> None:
        """Pops up SETTLE_S after a change, from the attention list as it is then: a session reports "waiting" a moment
        before its question's text arrives, and the box should show the question."""
        self.attention = attention
        if not self.settling:
            self.settling = True
            self.set_timer(SETTLE_S, self.popup_now)

    def popup_now(self) -> None:
        self.settling = False
        new = self.notifier.fresh(popup_items(self.attention), skip=self.on_screen)
        if not new or not self.popups:                  # off: what turns up meanwhile is on the board, not popped later
            return
        msg, ids = popup_status(new), [item_id(i) for i in new]
        mis = [i for i in new if i.get("kind") == "misplaced"]
        if mis and tmux.in_tmux() and self.cfg.notify_style == "popup" and (self.box is None or self.box.poll() is not None):
            # a wrong-window prompt gets its own box, with t and o; anything else that turned up pops next time
            self.notifier.seen -= {item_id(i) for i in new if i.get("kind") != "misplaced"}
            self.misplaced_popup(mis[0])
            return
        if tmux.in_tmux() and self.cfg.notify_style == "popup" and (self.box is None or self.box.poll() is not None):
            try:
                self.box = tmux_box(new)
                popup_log(f"box skip={sorted(self.on_screen)} {ids} {msg}")
                self.wait_box(self.box, [i["key"] for i in new], msg)
                return
            except OSError as e:
                popup_log(f"box failed: {e}")
        self.status_popup(msg, ids)                     # status style, or a box of ours is still open

    @work(group="box", thread=True)
    def wait_box(self, box, keys: list[str], msg: str) -> None:
        rc = box.wait()
        err = (box.stderr.read() if box.stderr else "").strip()
        self.call_from_thread(self.box_closed, rc, keys, msg, err)

    @work(group="box", thread=True)
    def misplaced_popup(self, item: dict) -> None:
        """The wrong-window box for one flagged prompt: its row from the hub (the prompt, the why), then the box, then
        whatever key closed it. A row the hub can't find any more is left to the board's ⇄."""
        key, rid = item.get("key"), str(item.get("ref") or "").partition(":")[2]
        try:
            s = self.client.session(key, turns=0) or {}
        except Exception as e:                          # noqa: BLE001 - the board's ⇄ still says it
            popup_log(f"misplaced box: no row for {key}: {e}"); return
        row = next((r for r in fmt.misopens(s) if str(r.get("id")) == rid), None)
        if row is None:
            return
        title = s.get("title") or item.get("title") or key
        try:
            box = tmux_misplaced_box(title, row)
        except OSError as e:
            popup_log(f"misplaced box failed: {e}"); return
        self.box = box
        sound.play(self.cfg.misplace_sound)            # the box is up: the eep, so it's heard as well as seen
        popup_log(f"misplaced box {key} row {rid}: {fmt.one_line(row.get('prompt'), 60)}")
        rc = box.wait()
        popup_log(f"misplaced box closed rc={rc} {key} row {rid}")
        if rc in (MIS_RC_MOVE, MIS_RC_KEEP):
            self.call_from_thread(self.misplaced_chosen, key, row, rc == MIS_RC_MOVE)

    def misplaced_chosen(self, key: str, row: dict, move: bool) -> None:
        """t or o in the wrong-window box: the same as on the board, with no second y (the key was the answer)."""
        s = next((x for x in self.sessions if x["key"] == key), None)
        if s is None:
            self.notify(f"{key} isn't on the board any more", timeout=6); return
        if not move:
            self.misplace_state(row, "kept"); return
        target = mis_target_key(row, self.sessions)
        if target:
            self.move_prompt(s, row, target); return
        self.select_key(key)                            # a drift: it needs a directory, asked on the board
        self.pending_prompt = (s, row)
        self._misplace_dir(s, row)

    def box_closed(self, rc: int, keys: list[str], msg: str, err: str = "") -> None:
        popup_log(f"box closed rc={rc} {keys} {err}".rstrip())      # 10+n Enter/digit, 3 another key, 0 EOF, 1 no client
        if 0 <= rc - OPEN_RC < len(keys):               # Enter (or 1-6): open what it named
            popup_log(f"box open {keys[rc - OPEN_RC]}")
            self.open_key(keys[rc - OPEN_RC])
        elif rc not in (0, 3) and not OPEN_RC <= rc < OPEN_RC + BOX_ITEMS:   # rc 1: no client to show it on
            self.status_popup(msg, [])

    def join_tmux(self) -> None:
        if tmux.in_tmux():
            tmux.set_role(os.environ.get("TMUX_PANE"), "tui")   # lets an exiting agent recognise the viewer window
            tmux.bind_slot_keys(os.environ.get("TMUX_PANE"))     # Alt+1…9 from any pane of the ctx session
            tmux.pin_board_width(os.environ.get("TMUX_PANE"))    # keep our width when a wider terminal attaches

    def action_slot(self, n: int) -> None:
        """Alt+N: open the session numbered N on the board, as Enter on its row does."""
        if not 1 <= n <= len(self.slots):
            self.notify(f"no session {n} on the board", severity="warning"); return
        self.open_key(self.slots[n - 1])

    def action_select_slot(self, n: int) -> None:
        """1…9 on the board: move to the session numbered N (its detail shows), without opening it. Enter, k, m and
        the rest then act on it; Alt+N is the one that opens."""
        if not 1 <= n <= len(self.slots):
            self.notify(f"no session {n} on the board", severity="warning"); return
        self.select_key(self.slots[n - 1])

    def select_key(self, key: str) -> bool:
        """Put the cursor on a session's row. False (and a note) when it has left the board."""
        if not any(x["key"] == key for x in self.sessions):
            self.notify(f"{key} isn't on the board any more", timeout=6)
            return False
        self.selected = key
        t = self.query_one("#table", DataTable)
        try:
            t.move_cursor(row=t.get_row_index(key))
        except Exception:                               # noqa: BLE001 - filtered out of the table: still selected
            pass
        self.load_detail()                              # now, not on the next refresh: the highlight handler sees no change
        return True

    def open_key(self, key: str) -> None:
        """Select a session on the board and open it, as Enter on its row does."""
        if self.select_key(key):
            self.action_resume()

    def action_toggle_popups(self) -> None:
        self.popups = not self.popups
        self.notify(f"pop-ups {'on' if self.popups else 'off'}", timeout=3)

    def _step_width(self, delta: int) -> None:
        """< / >: WIDTH_STEP columns wider or narrower, held on the session's @ctx_width and in
        ~/.local/state/ctxswitch/board-width, so the next `ctx tmux` starts the board the width you left it."""
        if not tmux.in_tmux():
            self.notify("< > resize the board inside ctx tmux", severity="warning"); return
        w = tmux.step_width(os.environ.get("TMUX_PANE"), delta)
        self.notify(f"board width {w}", timeout=2)

    def action_narrower(self) -> None:
        self._step_width(-tmux.WIDTH_STEP)

    def action_wider(self) -> None:
        self._step_width(tmux.WIDTH_STEP)

    def status_popup(self, msg: str, ids: list[str]) -> None:
        shown = tmux.in_tmux() and tmux_popup(msg)
        popup_log(f"{'tmux' if shown else 'toast'} skip={sorted(self.on_screen)} {ids} {msg}")
        if not shown:
            self.notify(msg, timeout=8)                 # outside tmux (or tmux refused): the TUI's own toast

    def _pin(self) -> list[dict]:
        """The list in pinned order: a session keeps the place it got when it joined (new ones go last), so its
        number only moves when one above it leaves (ended and hidden, gone, or another host on h). A leaver that
        comes back joins at the end."""
        members = [s for s in self.sessions if self.show_ended or fmt.session_state(s) != "ended"]
        keys = {s["key"] for s in members}
        self.pins = {k: v for k, v in self.pins.items() if k in keys}
        for s in members:                               # the hub's order seeds a first load
            if s["key"] not in self.pins:
                self.pins[s["key"]] = self.next_pin; self.next_pin += 1
        return sorted(members, key=lambda s: self.pins[s["key"]])

    def _visible(self) -> list[dict]:
        out = []
        for s in self._pin():
            if self.text_filter:
                hay = " ".join([fmt.display_title(s), s.get("key", ""), fmt.repo_label(s), (s.get("last_turn") or {}).get("user_text") or ""]).lower()
                if self.text_filter.lower() not in hay:
                    continue
            out.append(s)
        return out

    def _label_width(self) -> int:
        return max(16, self.size.width - 17)          # number + glyph + age columns + paddings + scrollbar

    def _nest(self, visible: list[dict]) -> tuple[list[dict], set[str]]:
        """The display order: each top-level session followed by the pool jobs it launched (by ``started_at``), and
        the keys shown that way. A child whose parent isn't on the list (filtered out, ended, unknown) is top level
        like any other session; only one level nests, so a grandchild stays at top level too."""
        by_key = {s["key"]: s for s in visible}
        kids: dict[str, list[dict]] = {}
        nested: set[str] = set()
        top: list[dict] = []
        for s in visible:
            p = fmt.link(s, "parent_key")
            if p in by_key and p != s["key"] and not fmt.link(by_key[p], "parent_key"):
                kids.setdefault(p, []).append(s); nested.add(s["key"])
            else:
                top.append(s)
        out: list[dict] = []
        for s in top:
            out.append(s)
            out += sorted(kids.get(s["key"], []), key=lambda c: c.get("started_at") or "")
        return out, nested

    def render_table(self) -> None:
        t = self.query_one("#table", DataTable)
        now = datetime.now(timezone.utc)
        prev = self.selected
        width = self._label_width()
        t.clear()
        visible = self._visible()
        rows = 0
        self.slots = []                                 # session keys by their number (Alt+N), in the order shown
        if self.group_ws:                              # groups in the order of their first member (pinned order)
            order: dict = {}
            for s in visible:
                order.setdefault(s.get("workstream"), []).append(s)
            visible = []
            for ws, members in order.items():
                hosts = sorted({m.get("host_id", "?") for m in members})
                head = Text(f"{fmt.one_line(fmt.workstream_label(ws), width - 8)} · {len(members)}", style="bold")
                if len(hosts) > 1:
                    head.append(f" · {' '.join(hosts)}", style="dim")
                t.add_row(Text(""), Text("▾", style="dim"), Text(""), head, key=f"ws:{ws}"); rows += 1
                visible += members
        visible, nested = self._nest(visible)            # pool jobs sit under the session that launched them
        for s in visible:
            st = fmt.session_state(s)
            last = s.get("last_turn") or {}
            asks = s.get("open_asks") or []
            when = s.get("status_since") if s.get("live_status") in LIVE else s.get("last_activity")
            style = STATE_STYLE.get(st, "")
            harness = fmt.harness_label(s, self.cfg.claude_profiles)
            # a host filter implies the host, except for another host's session run from here
            where = harness if self.host_filter == s.get("host_id") else f"{harness}·{s.get('host_id', '?')}"
            kid = s["key"] in nested                     # nested under the session that launched it
            job = kid or bool(fmt.link(s, "job") or fmt.link(s, "job_brief"))
            label = Text()
            reach = self.reach.get(s["key"], "")
            if kid:                                      # a pool job: indented under its parent row
                label.append("  ↳ ", style="dim")
            if s["key"] == self.visible_key:
                label.append("▶ ", style="bold")
            elif reach:
                label.append(tmux.REACH_GLYPH[reach] + " ", style=REACH_STYLE.get(reach, "dim"))
            if reach in ("foreign", "remote"):
                style = (style + " dim").strip()          # can't be opened from here
            ask_mark = f" ◐{fmt.age(asks[0].get('asked_at'), now)}" if asks and not kid else ""
            seen_mark = f" +{s['unseen']}" if s.get("unseen") and not kid else ""   # exchanges since you last looked
            # a prompt here that looks like it belongs elsewhere: the detail's rows when it has them, else the list's count
            glyph = "" if kid else self._mis_mark(s)              # a prompt here that looks like it's for elsewhere
            mis_mark = f" {glyph}" if glyph else ""
            badge, badge_style = ("", "") if kid else fmt.pool_mark(s, self.host_pools)   # a job's own badge would
            mark = f"{badge} " if badge else ""                                     # say nothing: it inherits the switch
            # the title keeps 8 columns and the row never overflows (a scrollbar would hide the last row): the repo
            # gives way first, then the host, then the +N, then the ⇄, then the ◐ age (the age column and the └ line
            # say it too)
            fixed = width - len(label.plain) - 1 - 8 - len(mark)   # after the glyph, the space after `where` and the title's 8
            if fixed - len(where) - len(ask_mark) - len(seen_mark) - len(mis_mark) < 0 and where != harness:
                where = harness
            if fixed - len(where) - len(ask_mark) - len(seen_mark) - len(mis_mark) < 0:
                seen_mark = ""
            if fixed - len(where) - len(ask_mark) - len(seen_mark) - len(mis_mark) < 0:
                mis_mark = ""
            if fixed - len(where) - len(ask_mark) - len(seen_mark) - len(mis_mark) < 0:
                ask_mark = ""
            # stale: the account (harness·host) turns yellow with its ⌂, so it reads at a glance with no extra column
            label.append(f"{where} ", style="yellow" if badge and fmt.pool_stale(s, self.host_pools) else "dim")
            if mark:
                label.append(mark, style=badge_style)
            if job:                                      # a job says what it ran, not the session's noisy title
                label.append(fmt.one_line(fmt.job_label(s) or fmt.display_title(s) or "(no prompt yet)",
                                          max(8, width - len(label.plain) - len(fmt.cost_mark(s)))), style=style)
                label.append(fmt.cost_mark(s), style="dim")
            else:
                repo_w = min(18 if width >= 40 else 10 if width >= 28 else 0,
                             width - len(label.plain) - len(ask_mark) - len(seen_mark) - len(mis_mark) - 8 - 1)
                if repo_w >= 4:
                    label.append(fmt.one_line(fmt.repo_label(s), repo_w) + " ",
                                 style="dim" if st in ("ended", "unknown") else "")
                rest = width - len(label.plain) - len(ask_mark) - len(seen_mark) - len(mis_mark)
                label.append(fmt.one_line(fmt.display_title(s) or "(no prompt yet)", max(8, rest)), style=style)
                label.append(ask_mark, style="bold yellow"); label.append(seen_mark, style="bold cyan")
                if mis_mark:
                    label.append(mis_mark, style=MISPLACE_STYLE)
            on = None if kid else waiting_on(s)
            if on:                                       # what it's waiting on, under its own row
                label.append("\n└ " + fmt.one_line(on, max(8, width - 2)), style="yellow")
            if not kid:
                self.slots.append(s["key"])              # Alt+N is for real sessions, never a pool job
            n = len(self.slots)
            t.add_row(Text(str(n) if n <= SLOTS and not kid else "", style="dim"), Text(fmt.state_glyph(s), style=style),
                      Text(f"{fmt.age(when, now):>4} ", style="dim"), label, key=s["key"], height=2 if on else 1)
            rows += 1 if on else 0
            rows += 1
        t.styles.height = max(1, rows)                # grow with the list, up to the max-height cap
        if prev and (prev in [s["key"] for s in visible] or prev.startswith("ws:")):
            try:
                t.move_cursor(row=t.get_row_index(prev))
            except Exception:                           # noqa: BLE001
                pass
        elif visible:
            self.selected = visible[0]["key"]

    def on_resize(self) -> None:
        self.render_table()

    def on_data_table_row_selected(self, ev: DataTable.RowSelected) -> None:
        # the table consumes Enter itself; route it to the same action as the footer binding
        if ev.row_key is not None:
            self.selected = ev.row_key.value
        self.action_resume()

    def on_data_table_row_highlighted(self, ev: DataTable.RowHighlighted) -> None:
        if ev.row_key is not None and ev.row_key.value != self.selected:
            self.selected = ev.row_key.value
            self.load_detail()

    @work(exclusive=True, group="detail", thread=True)
    def load_detail(self) -> None:
        key = self.selected
        if not key:
            return
        if key.startswith("ws:"):
            self.call_from_thread(self.render_group, key[3:]); return
        try:
            s = self.client.session(key, turns=8)
            kids = self._kids(s) if s else []           # the pool jobs it launched (a hub with no links: [])
        except Exception:                               # noqa: BLE001
            return
        if s:
            self.call_from_thread(self.render_detail, s, kids)

    def render_group(self, ws: str) -> None:
        now = datetime.now(timezone.utc)
        members = [s for s in self._visible() if str(s.get("workstream")) == ws]
        t = Text(f"{fmt.workstream_label(None if ws == 'None' else ws)}\n", style="bold")
        t.append(f"{ws}\n\n", style="dim")
        for s in members:
            st = fmt.session_state(s)
            t.append(f"{fmt.state_glyph(s)} {s.get('host_id')}/{fmt.harness_label(s, self.cfg.claude_profiles)} ", style=STATE_STYLE.get(st, ""))
            t.append(f"{fmt.one_line(fmt.display_title(s), 60)}", style=STATE_STYLE.get(st, ""))
            t.append(f"  {fmt.age(s.get('status_since') or s.get('last_activity'), now)}\n", style="dim")
        self.query_one("#detail_text", Static).update(t)

    def _kids(self, s: dict) -> list[dict]:
        """The pool jobs ``s`` launched, by when they started: the ones on the board, else the hub's children_keys
        (newest first, so re-sorted by their own started_at). A hub that stores no links says []. """
        kids = [x for x in self.sessions if fmt.link(x, "parent_key") == s["key"]]
        if kids:
            return sorted(kids, key=lambda c: c.get("started_at") or "")
        out = []
        for k in s.get("children_keys") or []:
            try:
                c = self.client.session(k, turns=0)
            except Exception:                           # noqa: BLE001 - the detail pane must still show the session
                c = None
            if c:
                out.append(c)
        return sorted(out, key=lambda c: c.get("started_at") or "")

    def render_detail(self, s: dict | None = None, kids: list[dict] | None = None) -> None:
        w = self.query_one("#detail_text", Static)
        if s is None:
            if not self.selected:
                w.update(Text("select a session", style="dim")); return
            self.load_detail(); return
        if s.get("key") != self.selected:               # a slow load for a row you've moved off
            return
        self.detail = s
        row = self._selected_session() or {}
        if misplaced_mark(s) != misplaced_mark(row):    # its rows resolved (or appeared) since the list refreshed:
            self.render_table()                         # the row's ⇄ has to catch up with what the card says
        now = datetime.now(timezone.utc)
        st = fmt.session_state(s)
        t = Text()
        t.append(f"{fmt.state_glyph(s)} {fmt.display_title(s) or '-'}\n", style="bold")
        turns = s.get("turns", [])
        # the card reads top-down by what you need on coming back: what's going on, what it waits on, what it costs,
        # where it is, the last exchange, then everything else
        if s.get("recap"):
            stale = s.get("recap_anchor") != _anchor(s)
            t.append("RECAP" + (" (updating…)" if stale else "") + "\n", style="bold")
            t.append(s["recap"] + "\n", style="cyan")
            for line in (s.get("reentry") or "").splitlines():
                t.append(f"› {line}\n")
            t.append("\n")
        opens = [x for x in s.get("asks", []) if not x["answered"]]
        if opens:
            t.append("WAITING ON YOU\n", style="bold yellow")
            for x in opens:
                t.append(f"◐ {fmt.age(x.get('asked_at'), now)} [{x['kind']}] ", style="yellow")
                t.append(x['text'] + "\n")
            ask, why = fmt.reply_target(s)
            t.append("  a = answer it here\n" if ask else f"  {why}\n", style="dim")
            t.append("\n")
        misrows = fmt.misopens(s)
        if misrows:
            t.append("MISPLACED?\n", style=MISPLACE_STYLE)
            for r in misrows:
                t.append(f"{fmt.MISPLACE_GLYPH} {fmt.age(r.get('at'), now)}  ", style=MISPLACE_STYLE)
                t.append(f"\"{fmt.one_line(r.get('prompt'), 120)}\"\n")
                t.append(f"   {fmt.mis_why(r)}\n")
                t.append(f"   {'blocked before the agent saw it' if r.get('source') == 'pre' else 'the agent may have started on it'}\n",
                         style="dim")
                where = ("t move it there" if mis_target_key(r, self.sessions) else
                         "t start a new session with it")
                t.append(f"   {where} · o keep it here\n\n", style="dim")
        kids = kids if kids is not None else self._kids(s)   # load_detail fetched them with the session
        line = fmt.spend_line(s, kids or None)
        if line or kids:
            t.append("SPEND\n", style="bold")
            t.append((line or f"{len(kids)} pool jobs") + "\n", style="dim")
        if kids:
            t.append("POOL JOBS\n", style="bold")
            for j in kids[:6]:
                t.append(f"↳ {fmt.one_line(fmt.job_label(j) or j.get('job') or '?', 110)}", style="dim")
                t.append(f" {fmt.cost_mark(j).strip() or '—'}\n")
            if len(kids) > 6:
                t.append(f"… {len(kids) - 6} more\n", style="dim")
        if line or kids:
            t.append("\n")
        t.append(f"{st} {fmt.age(s.get('status_since') or s.get('last_activity'), now)}", style=STATE_STYLE.get(st, ""))
        t.append(f" · {s.get('turn_count')} exch · {fmt.age(s.get('started_at'), now)} old", style="dim")
        if s.get("last_viewed_at"):
            t.append(f" · seen {fmt.age(s['last_viewed_at'], now)}", style="dim")
        t.append("\n")
        reach = "pane" if s["key"] == self.visible_key else self.reach.get(s["key"])
        if reach:
            help_ = "on the left now" if s["key"] == self.visible_key else tmux.reach_help(s, reach, self.cfg.ssh)
            if reach == "foreign" and s.get("pid"):
                help_ += f" (pid {s['pid']})"
            t.append(f"{'▶' if s['key'] == self.visible_key else tmux.REACH_GLYPH[reach]} {help_}\n", style=REACH_STYLE.get(reach, "dim"))
        t.append(f"{fmt.short_key(s)} {fmt.repo_label(s)}\n", style="dim")
        t.append(f"{s.get('cwd') or '-'}\n", style="dim")
        model = (s['meta'].get('model') or '').rsplit('/', 1)[-1]
        t.append(f"{model} {s['meta'].get('permission_mode') or ''}\n", style="dim")
        pline = fmt.pool_line(s, self.host_pools, now)
        if pline:
            t.append(pline + "\n", style="bold yellow" if fmt.pool_stale(s, self.host_pools) else "dim")
        if s.get("purpose"):
            t.append("for: ", style="bold")
            t.append(s["purpose"] + "\n")
        t.append("\n")
        if turns:
            last = turns[-1]
            # "you" = the operator's last real prompt; "agent" = the newest agent text, even if a task notification
            # or peer message started the turn that produced it
            you = next((x for x in reversed(turns) if x.get("user_origin") in ("human", "peer", "shell")), None)
            said = next((x for x in reversed(turns) if x.get("assistant_text")), None)
            t.append("LAST EXCHANGE\n", style="bold")
            if you is not None:
                t.append(f"{fmt.who(you)} ({fmt.age(you.get('started_at'), now)}): ", style="bold")
                t.append(fmt.one_line(you.get("user_text"), 400) + "\n")
            if said is not None:
                t.append(f"{fmt.replied(said)} ({fmt.age(said.get('ended_at') or said.get('started_at'), now)}): ", style="bold")
                t.append(fmt.one_line(said.get("assistant_text"), 700) + "\n")
            tally = {k: v for k, v in (last.get("tally") or {}).items() if v}
            if tally or last.get("events"):
                t.append("  " + ", ".join(f"{v} {k}" for k, v in tally.items()) + ("  " + " ".join(last.get("events", [])) if last.get("events") else "") + "\n", style="dim")
            if not last.get("complete"):
                t.append(f"  working on a {last.get('user_origin')} turn…\n" if last.get("user_origin") != "human" else "  (in progress)\n", style="green")
            t.append("\n")
        new = fmt.since_viewed(s)
        if new:
            t.append(f"SINCE YOU LOOKED ({fmt.age(s.get('last_viewed_at'), now)} ago)\n", style="bold cyan")
            t.append(fmt.since_line(new) + "\n", style="cyan")
            for r in new["rows"][-5:]:
                t.append(f"  {r['kind']}: ", style="dim")
                t.append(f"{fmt.one_line(r['text'], 140)}\n")
            for m in new["replies"][-2:]:
                t.append("  reply: ", style="dim")
                t.append(f"{fmt.one_line(m.get('reply'), 140)}\n")
            t.append("\n")
        msgs = fmt.messages(s)
        if msgs:
            t.append("MESSAGES\n", style="bold")
            asked = {a["ref"]: a for a in s.get("asks") or []}
            for m in msgs:
                t.append(f"{fmt.MSG_GLYPH.get(m['status'], '?')} ", style=MSG_STYLE.get(m["status"], "dim"))
                if m.get("ask_ref") in asked:
                    t.append(f"re \"{fmt.one_line(asked[m['ask_ref']]['text'], 60)}\": ", style="dim")
                if m.get("from_key"):
                    t.append(fmt.sender(m), style="magenta")
                t.append(fmt.one_line(m["body"], 160) + "\n")
                t.append(f"  {fmt.one_line(fmt.message_note(s, m), 300)}\n", style="dim" if m["status"] != "failed" else "red")
            t.append("\n")
        work = s.get("workstream_messages") or []
        if work:
            t.append(f"FOR THIS WORK ({fmt.workstream_label(s.get('workstream'))})\n", style="bold")
            for m in work:
                t.append(f"{fmt.MSG_GLYPH['queued']} ", style=MSG_STYLE.get("queued", "dim"))
                t.append(fmt.one_line(m["body"], 160) + "\n")
            t.append(f"  {fmt.WORK_NOTE}\n\n", style="dim")
        rules = fmt.instructions(s)
        if rules:
            t.append("YOUR INSTRUCTIONS\n", style="bold")
            for n, r in enumerate(rules, 1):
                t.append(f"{n} ", style="dim")
                t.append(f"• {fmt.one_line(r['text'], 160)}")
                t.append(f"  #{r['turn_idx']}\n", style="dim")
            t.append("\n")
        claimed = fmt.claims(s)
        if claimed:
            t.append("AGENT SAYS\n", style="bold")
            for r in claimed:
                kind = r["extra"].get("claim", "other")
                t.append(f"{fmt.CLAIM_GLYPH.get(kind, '·')} {fmt.one_line(r['text'], 160)}", style=CLAIM_STYLE.get(kind, ""))
                t.append(f"  #{r['turn_idx']}\n", style="dim")
            t.append("\n")
        decided = fmt.decisions(s)
        if decided:
            t.append("DECISIONS\n", style="bold")
            for _, q, a in decided:
                if q:
                    t.append(f"✓ {fmt.one_line(q, 120)}\n", style="dim")
                t.append(f"  → {fmt.one_line(a, 160)}\n")
            t.append("\n")
        sibs = s.get("siblings") or []
        if sibs:
            t.append(f"SAME WORK ELSEWHERE ({fmt.workstream_label(s.get('workstream'))})\n", style="bold")
            for x in sibs:
                st = "ended" if x.get("ended") and x.get("live_status") not in LIVE else x.get("live_status") or "unknown"
                t.append(f"{fmt.GLYPH.get(st, '?')} {x['host_id']}/{fmt.harness_label(x, self.cfg.claude_profiles)} ", style=STATE_STYLE.get(st, ""))
                t.append(f"{fmt.one_line(x.get('title'), 60)}")
                t.append(f"  {fmt.age(x.get('status_since') if st in LIVE else x.get('last_activity'), now)}\n", style="dim")
                for r in x.get("instructions") or []:      # its instructions, for context: not this session's
                    t.append(f"    told it: {fmt.one_line(r, 120)}\n", style="dim")
            t.append("\n")
        if len(turns) > 1:
            t.append("EARLIER\n", style="bold")
            for tr in turns[-7:-1][::-1]:
                t.append(f"#{tr['idx']} {fmt.age(tr.get('started_at'), now)} ", style="dim")
                if tr.get("user_origin") not in ("human", None):
                    t.append(f"[{tr['user_origin']}] ", style="dim")
                t.append(f"{fmt.one_line(tr.get('user_text'), 120)}\n")
                if tr.get("assistant_text"):
                    t.append(f"  ↳ {fmt.one_line(tr.get('assistant_text'), 120)}\n", style="dim")
        w.update(t)

    # -- actions --------------------------------------------------------------------------------------------------
    def action_cycle_host(self) -> None:
        opts: list[str | None] = [self.this_host, None, *[h for h in self.hosts if h != self.this_host]]
        try:
            i = opts.index(self.host_filter)
        except ValueError:
            i = -1
        self.host_filter = opts[(i + 1) % len(opts)]
        self.refresh_data()

    def action_toggle_ws(self) -> None:
        self.group_ws = not self.group_ws
        if not self.group_ws and self.selected and self.selected.startswith("ws:"):
            self.selected = None
        self.render_table()

    def action_toggle_ended(self) -> None:
        self.show_ended = not self.show_ended; self.render_table()

    def action_refresh(self) -> None:
        self.refresh_data()

    # -- the CPU/GPU panel ----------------------------------------------------------------------------------------
    def action_metrics(self) -> None:
        """g: the CPU/GPU panel on or off, starting as the last run left it, on the hosts it was told to show.
        Hidden, nothing is fetched at all."""
        self.metrics_on = not self.metrics_on
        _metrics_save(self.metrics_on, self.metrics_picked)
        self.show_metrics()

    def action_metrics_host(self) -> None:
        """G: pick which hosts the CPU/GPU panel shows, several at once — remembered for the next run. Closing the
        picker without touching it changes nothing, so the panel keeps ticking either way. A board that hasn't asked
        the hub yet (its panel hidden) asks first, and the picker opens on the answer."""
        self.metrics_picking = True
        if self.metrics_names:
            self._open_picker()
        else:
            self.fetch_metrics()

    def _open_picker(self) -> None:
        self.metrics_picking = False
        self.push_screen(HostPicker(self.metrics_names, self.metrics_counts, self.metrics_picked),
                        self._hosts_picked)

    def _hosts_picked(self, hosts: list[str] | None) -> None:
        self.metrics_picked = sorted(hosts or [])
        _metrics_save(self.metrics_on, self.metrics_picked)
        if self.metrics_on:
            self.fetch_metrics()

    def show_metrics(self) -> None:
        w = self.query_one("#metrics", MetricsPanel)
        w.add_class("visible") if self.metrics_on else w.remove_class("visible")
        if self.metrics_timer:
            self.metrics_timer.stop()
            self.metrics_timer = None
        if self.metrics_on:
            self.metrics_timer = self.set_interval(METRICS_EVERY, self.fetch_metrics)
            self.fetch_metrics()

    @work(thread=True, exclusive=True, group="metrics")
    def fetch_metrics(self) -> None:
        """What the hosts doing the work are made of, every couple of seconds while the panel is shown. A hub with no
        /v1/metrics simply gets the panel to say so, and the asking goes on in case it comes up."""
        try:
            body = self.client.metrics()
        except Exception:                               # noqa: BLE001 - an older hub has no such route
            self.call_from_thread(self.metrics_said, METRICS_HUB_MSG)
            return
        hosts = (body or {}).get("hosts") or {}
        self.call_from_thread(self.metrics_show, hosts, sorted(hosts))

    def metrics_show(self, hosts: dict, names: list[str]) -> None:
        """The panel for the hosts picked (every one that reports when none were), never more than half the board."""
        self.metrics_names, self.metrics_counts = names, {h: gpu_count(e) for h, e in hosts.items()}
        if self.metrics_picking:                               # G was pressed before the hub had answered
            self._open_picker()
        if not hosts:
            self.metrics_said(METRICS_NONE_MSG)
            return
        w = self.query_one("#metrics", MetricsPanel)
        w.update(hosts_text(hosts, self.metrics_picked, max(20, w.content_size.width - 2), self.size.height,
                            self.cfg.metrics_host, self.this_host))

    def metrics_said(self, msg: str) -> None:
        """One dim line where the panel would be, and if G is waiting on it, an empty picker to say the same."""
        if self.metrics_picking:
            self.metrics_picking = False
            self.push_screen(HostPicker([], {}, self.metrics_picked), self._hosts_picked)
        self.query_one("#metrics", MetricsPanel).update(Text(msg, "dim"))

    def action_mark_viewed(self) -> None:
        if self._selected_session():
            self.client.viewed(self.selected, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
            self.notify("marked viewed"); self.load_detail()

    def action_resume(self) -> None:
        s = next((x for x in self.sessions if x["key"] == self.selected), None)
        if not s:
            return
        self.mark_opened(s["key"])                     # opening it is looking at it
        if tmux.in_tmux():
            self.show_in_tmux(s)
        else:
            self.notify(open_tab(s, self.this_host, self.cfg.ssh), timeout=8)

    @work(group="viewed", thread=True)
    def mark_opened(self, key: str) -> None:
        try:
            self.client.viewed(key, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        except Exception:                               # noqa: BLE001 - opening must not depend on the hub
            pass

    @work(exclusive=True, group="tmux", thread=True)
    def show_in_tmux(self, s: dict) -> None:
        try:
            msg = tmux.show(s, ssh=self.cfg.ssh)
        except Exception as e:                          # noqa: BLE001
            msg = f"tmux: {e}"
        popup_log(f"open {s['key']}: {msg}")            # what Enter (on a row or in the box) did
        self.call_from_thread(self.notify, msg, timeout=6)
        self.refresh_data()

    def _show_input(self, mode: str, placeholder: str, value: str = "") -> None:
        self.input_mode = mode
        f = self.query_one("#filter", Input)
        f.placeholder = placeholder; f.value = value
        f.add_class("visible"); f.can_focus = True; f.focus()
        if value:
            f.cursor_position = len(value)

    def _selected_cwd(self) -> str:
        s = next((x for x in self.sessions if x["key"] == self.selected), None)
        return (s or {}).get("cwd") or os.path.expanduser("~")

    def _remote_hosts(self) -> list[str]:
        """The other hosts n and s can start an agent on: every host the config's [ssh] table reaches from here
        ("" = not reachable), this host left out — it has its own picker, with its own accounts' profiles. A board
        that hasn't loaded its hosts yet has none to offer."""
        return [h for h in dict.fromkeys(getattr(self, "hosts", None) or [])
                if h and h != _here(self) and tmux.ssh_target(h, self.cfg.ssh)]

    def action_new_session(self) -> None:
        """n: pick the host when others are reachable, the agent (Claude per account, pi, Antigravity, or a plain
        shell), then the directory."""
        if not tmux.in_tmux():
            self.notify("new sessions need the tmux manager (ctx tmux)", severity="warning"); return
        here = _here(self)
        hosts = [here, *self._remote_hosts()] if here else []   # no host named yet: nothing to pick between
        if len(hosts) > 1:                            # another host reachable: say up front which one it lands on
            opts = [(f"{h} (here)" if h == here else h, h, None) for h in hosts]
            self.push_screen(Picker(opts, "new session on: "), lambda h: self._picked_host(h))
            return
        options = [*new_session_options(self.cfg), ("shell", "shell", None)]   # the shell is here only, not in x's list
        self.push_screen(Picker(options), self._picked)

    def _picked_host(self, choice: tuple[str, str | None] | None) -> None:
        """n's first pick on a board with other hosts: this host goes on to today's picker; another gets the
        harnesses it can run, listed here because its profiles are its own."""
        if not choice:
            return
        host = choice[0]
        if host == _here(self):
            options = [*new_session_options(self.cfg), ("shell", "shell", None)]
            self.push_screen(Picker(options), self._picked)
            return
        self.push_screen(Picker(REMOTE_START_OPTIONS, f"new session on {host}: "),
                         lambda c: self._picked(c, host))

    def _picked(self, choice: tuple[str, str | None] | None, host: str | None = None) -> None:
        if not choice:
            return
        harness, profile = choice
        self._ask_dir(harness, profile, host=host)

    def _ask_dir(self, harness: str, profile: str | None, cwd: str | None = None,
                 host: str | None = None) -> None:
        """The directory prompt a new session ends with, pre-filled with ``cwd`` (a Start row's) or the selected
        session's. For another host it defaults to ``~`` — that machine's own home, not this one's — and carries the
        host in the input mode, so submitting starts it there."""
        what = {"agy": "antigravity"}.get(harness, harness)
        if host:
            self._show_input(f"new@{host}:{harness}:", f"directory for the new {what} on {host}, enter to start",
                             cwd or "~")
            return
        if profile:
            what = f"{harness} · {profile}"
        hint = ""
        if harness == "claude":
            others = [p.name for p in self.cfg.claude_profiles if p.name != profile]
            if others:
                hint = f", or '{others[0]}: [path]' for that account"
        default = cwd or self._selected_cwd()
        if harness == "shell":
            self._show_input(f"new:{harness}:", "directory for the new shell, enter to start", default); return
        self._show_input(f"new:{harness}:{profile or ''}", f"directory for the new {what} session, enter to start{hint}", default)

    def action_start(self) -> None:
        """s: the Start screen — a key per profile to start new, and the ended sessions to resume."""
        if not tmux.in_tmux():
            self.notify("starting sessions needs the tmux manager (ctx tmux)", severity="warning"); return
        options = [*new_session_options(self.cfg), ("shell", "shell", None)]
        here = _here(self)
        self.push_screen(StartScreen(options, fmt.recent_sessions(self.sessions, self.reach), self.reach,
                                     hosts=([here] if here else []) + self._remote_hosts()), self._started)

    def _started(self, result: tuple | None) -> None:
        if not result:
            return
        if result[0] == "new":
            self._ask_dir(result[1], result[2], result[3], host=result[4] if len(result) > 4 else None)
        elif result[0] == "resume":
            if not tmux.in_tmux():
                self.notify("resuming needs the tmux manager (ctx tmux)", severity="warning"); return
            self.resume_many_sessions(list(result[1]))

    @work(exclusive=True, group="tmux", thread=True)
    def resume_many_sessions(self, keys: list[str]) -> None:
        """Resume each marked session in its own window, the last of them into the viewer pane (see tmux.resume_many)."""
        by_key = {s["key"]: s for s in self.sessions}
        sessions = [by_key[k] for k in keys if k in by_key]
        try:
            msg = tmux.resume_many(sessions, ssh=self.cfg.ssh)
        except Exception as e:                          # noqa: BLE001
            msg = f"tmux: {e}"
        popup_log(f"resume {len(sessions)}: {msg}")
        self.call_from_thread(self.notify, msg, timeout=8)
        for s in sessions:
            self.mark_opened(s["key"])
        self.refresh_data()

    @work(exclusive=True, group="tmux", thread=True)
    def start_agent(self, harness: str, cwd: str, profile: str | None = None, first: str = "") -> None:
        """Start one in a window of ctx's tmux. ``first``: a first prompt to start it with (t, on a drifted one)."""
        extra = {"extra": [first]} if first else {}     # only passed when there is one: a start without it is today's
        try:
            if harness == "claude":
                typed, cwd = split_profile(cwd, [p.name for p in self.cfg.claude_profiles])
                profile = typed or profile
                msg = tmux.new_agent(harness, cwd, profile=profile, profiles=self.cfg.claude_profiles, **extra)
            else:
                msg = tmux.new_agent(harness, cwd, **extra)
        except Exception as e:                          # noqa: BLE001
            msg = f"tmux: {e}"
        popup_log(f"new {harness}{f'[{profile}]' if profile else ''} in {cwd}: {msg}")
        self.call_from_thread(self.notify, msg, timeout=6)

    @work(exclusive=True, group="tmux", thread=True)
    def start_remote_agent(self, host: str, harness: str, cwd: str) -> None:
        """Start an agent on another host: it runs in a tmux named ``ctx`` over there, so it survives the ssh
        dropping; the window this opens is only the view of it (see tmux.new_remote_agent)."""
        try:
            msg = tmux.new_remote_agent(host, harness, cwd, self.cfg.ssh)
        except Exception as e:                          # noqa: BLE001
            msg = f"tmux: {e}"
        popup_log(f"new {harness} on {host} in {cwd}: {msg}")
        self.call_from_thread(self.notify, msg, timeout=6)

    def action_help(self) -> None:
        self.push_screen(Help())

    def action_spend(self) -> None:
        """$: the Spend screen, "loading…" until the three windows come back from the hub."""
        screen = SpendScreen()
        self.push_screen(screen)
        self.fetch_spend(screen)

    @work(exclusive=True, group="spend", thread=True)
    def fetch_spend(self, screen: SpendScreen) -> None:
        reports = spend_windows(self.client, self.host_filter)
        self.call_from_thread(screen.loaded, reports)

    def tip_for(self, key: str) -> str | None:
        if key.startswith("ws:"):
            return f"▾ {fmt.workstream_label(key[3:])}: the sessions on this repo + branch, any host. m messages the work"
        s = next((x for x in self.sessions if x["key"] == key), None)
        return row_tip(s, self.reach.get(key, ""), key == self.visible_key, self.cfg.ssh, datetime.now(timezone.utc)) if s else None

    def _selected_session(self) -> dict | None:
        return next((x for x in self.sessions if x["key"] == self.selected), None)

    def action_message(self) -> None:
        if (self.selected or "").startswith("ws:"):
            self.action_message_work(); return
        s = self._selected_session()
        if not s:
            return
        why = fmt.can_message(s)
        if why:
            self.notify(why, severity="warning"); return
        when = "now" if s.get("live_status") in LIVE else "when it next runs"
        self._show_input(f"msg:{s['key']}", f"message to {fmt.one_line(fmt.display_title(s), 30)} ({when}), enter to send")

    def action_reply(self) -> None:
        s = self._selected_session()
        if not s:
            return
        ask, why = fmt.reply_target(s)
        if ask is None:
            self.notify(why, severity="warning", timeout=6); return
        self.pending_reply = (s["key"], ask["ref"])
        self._show_input("reply", f"answer \"{fmt.one_line(ask['text'], 50)}\", enter to send")

    def _mis_mark(self, s: dict) -> str:
        """A row's ⇄: the detail's rows decide when that detail is up (what it resolved since the list refreshed
        leaves nothing marked), otherwise the list's count does."""
        return misplaced_mark(self.detail if (self.detail or {}).get("key") == s["key"] else s)

    def _misplaced_row(self) -> dict | None:
        """The detail's newest open misplaced-prompt row of the selected session, from the row's own key: the detail
        fetch may still be out, and the board only carries a count for it."""
        s = self._selected_session()
        rows = fmt.misopens(self.detail if self.detail and self.detail.get("key") == self.selected else s or {})
        if not rows and s:                              # the list's rows carry the rows themselves, or just the count
            rows = [r for r in (s.get("misplaced") or []) if isinstance(r, dict) and r.get("state", "open") == "open"]
        if not rows and s and int(s.get("misplaced_open") or 0) and self.detail:
            self.load_detail()                          # the count says yes, the rows haven't arrived yet
        return rows[0] if rows else None

    def action_misplace_keep(self) -> None:
        """o: a prompt the hub thinks belongs elsewhere was meant for this session: say so, and leave it alone."""
        row = self._misplaced_row()
        if not row:
            self.notify("nothing marked ⇄ on the selected session", severity="warning", timeout=5); return
        self.misplace_state(row, "kept")

    def action_misplace_move(self) -> None:
        """t: send the ⇄ prompt where it looks like it belongs — to the session it names, after a y, or to a new one
        when the hub only knows this session has drifted off its purpose."""
        s = self._selected_session()
        row = self._misplaced_row()
        if not (s and row):
            self.notify("nothing marked ⇄ on the selected session", severity="warning", timeout=5); return
        target = mis_target_key(row, self.sessions)
        if target:
            self.confirm(f"move this prompt to {mis_target_title(row, self.sessions)}?",
                         lambda: self.move_prompt(s, row, target))
            return
        # a drift, or the session it names has left the board: it needs somewhere new to go, so the same prompt n ends with
        self.pending_prompt = (s, row)
        self._misplace_dir(s, row)

    @work(group="misplace", thread=True)
    def move_prompt(self, s: dict, row: dict, target: str) -> None:
        """A prompt that landed in ``s`` goes to ``target``: stop this session if it already started on it, tell it to
        disregard that prompt, then send the prompt itself over there."""
        try:
            skipped = ""
            if row.get("source") == "llm":              # the agent has the prompt: stop it, then tell it to drop it
                skipped = self.interrupt_session(s) or ""
                self.client.send(s["key"], mis_disregard(row), self.this_host)
            self.client.send(target, row.get("prompt") or "", self.this_host)
            self.client.misplaced_state(row["id"], "moved")
            msg = f"moved to {mis_target_title(row, self.sessions)}" + skipped
        except Exception as e:                          # noqa: BLE001 - a hub without the route, or a session gone
            msg = f"not moved: {e}"
        popup_log(f"move misplaced {row.get('id')} {s['key']} -> {target}: {msg}")
        self.call_from_thread(self.notify, msg, timeout=6)
        self.call_from_thread(self.load_detail)

    def interrupt_session(self, s: dict) -> str | None:
        """Escape into a session's pane, from a worker thread: "" when it went in, the reason it couldn't otherwise
        (a session outside ctx's tmux, or on another host, has no pane this board can send a key to)."""
        if not tmux.in_tmux():
            return " · interrupt skipped: this board isn't in tmux"
        try:
            p = tmux.agent_pane(s["key"])
        except Exception:                               # noqa: BLE001 - tmux is optional
            return " · interrupt skipped: tmux didn't answer"
        if p is None:
            return " · interrupt skipped: it isn't in a pane of ctx's tmux"
        tmux.interrupt(p.id)
        return ""

    @work(group="misplace", thread=True)
    def misplace_state(self, row: dict, state: str) -> None:
        try:
            self.client.misplaced_state(row["id"], state)
            msg = {"kept": "kept here", "moved": "moved", "resent": "resent"}[state]
        except Exception as e:                          # noqa: BLE001 - an older hub has no such route
            msg = f"not recorded: {e}"
        self.call_from_thread(self.notify, msg, timeout=5)
        self.call_from_thread(self.load_detail)

    def _misplace_dir(self, s: dict, row: dict) -> None:
        """A drifted prompt goes to a session of its own: the directory prompt n ends with, pre-filled with where
        this one runs and carrying the prompt to start it with."""
        what = {"agy": "antigravity"}.get(s.get("harness") or "claude", s.get("harness") or "claude")
        self._show_input(f"newm:{s.get('harness') or 'claude'}:{(s.get('meta') or {}).get('profile') or ''}",
                         f"directory for the new {what} session with this prompt, enter to start",
                         s.get("cwd") or self._selected_cwd())

    def action_message_work(self) -> None:
        """W, or m on a workstream header: a message for whichever session is (or next is) live on that work."""
        ws = self.selected[3:] if (self.selected or "").startswith("ws:") else (self._selected_session() or {}).get("workstream")
        if not ws:
            self.notify("not in a repo: no workstream to message", severity="warning"); return
        self._show_input(f"work:{ws}", f"message to {fmt.workstream_label(ws)} (the first session live on it, any host), enter to send")

    def action_exchange(self) -> None:
        """x: send the selected session elsewhere. To another Claude account: move it (same transcript). To another
        agent (pi, Antigravity, Claude from pi…): hand it off (a document plus an opening prompt). A running
        session in ctx's tmux is ended first, after a y."""
        s = self._selected_session()
        if not s:
            return
        if not tmux.in_tmux():
            self.notify("exchange needs the tmux manager (ctx tmux)", severity="warning"); return
        options = mv.targets(s, new_session_options(self.cfg), self.cfg.claude_profiles)
        if not options:
            self.notify("nowhere to send it: no other agent or claude account is installed here", severity="warning"); return
        live = s.get("live_status") in LIVE
        if live and not mv.can_end_here(s):
            self.notify("it's running outside ctx's tmux (another terminal, or another host): quit it there, then x",
                        severity="warning", timeout=6); return
        if s.get("harness") == "claude" and s.get("host_id") != self.this_host:
            self.notify(f"its transcript is on {s.get('host_id')}: hand off from there, or ctx move there", severity="warning"); return
        self.push_screen(Picker(options, "send to: "), lambda choice: self._exchange_to(s, choice, live))

    def _exchange_to(self, s: dict, choice: tuple[str, str | None] | None, live: bool) -> None:
        if not choice:
            return
        harness, profile = choice
        move = harness == "claude" and s.get("harness") == "claude"
        what = f"move it to {profile}" if move else f"hand it off to {harness}{f' · {profile}' if profile else ''}"
        if move:
            try:
                mv.check({**s, "live_status": "ended"}, profile, self.cfg.claude_profiles, self.this_host)
            except mv.CannotMove as e:
                self.notify(str(e), severity="warning"); return
        if live:
            busy = " (busy: it's working)" if s.get("live_status") == "busy" else ""
            self.confirm(f"end {fmt.one_line(fmt.display_title(s), 30)}{busy} and {what}?",
                         lambda: self.exchange_session(s, profile, True) if move else self.handoff_session(s, harness, profile, True))
        elif move:
            self.exchange_session(s, profile)
        else:
            self.handoff_session(s, harness, profile)

    @work(exclusive=True, group="tmux", thread=True)
    def handoff_session(self, s: dict, harness: str, profile: str | None, end_first: bool = False) -> None:
        try:
            if end_first:
                self.call_from_thread(self.notify, f"ending {fmt.one_line(fmt.display_title(s), 30)}…", timeout=4)
                why = mv.end_and_wait(s, self.cfg)
                if why:
                    raise mv.CannotMove(why)
            start = lambda h, cwd, extra, prof: tmux.new_agent(h, cwd, extra, profile=prof, profiles=self.cfg.claude_profiles)
            msg = mv.handoff(s, harness, profile, self.cfg, self.client, start)
        except Exception as e:                          # noqa: BLE001
            msg = f"handoff: {e}"
        popup_log(f"handoff {s.get('key')} -> {harness}: {msg}")
        self.call_from_thread(self.notify, msg, timeout=8)

    @work(exclusive=True, group="tmux", thread=True)
    def exchange_session(self, s: dict, to: str, end_first: bool = False) -> None:
        try:
            if end_first:
                self.call_from_thread(self.notify, f"ending {fmt.one_line(fmt.display_title(s), 30)}…", timeout=4)
                why = mv.end_and_wait(s, self.cfg)
                if why:
                    raise mv.CannotMove(why)
            target, path = mv.move(s, to, self.cfg.claude_profiles, self.this_host)
            if tmux.in_tmux():
                msg = f"moved to {target.name}: " + tmux.show(s, ssh=self.cfg.ssh, extra=mv.resume_extra(target))
            else:
                msg = f"moved to {target.name}; run: {mv.command_hint(s, target, self.cfg.claude_profiles)}"
        except Exception as e:                          # noqa: BLE001
            msg = f"exchange: {e}"
        popup_log(f"exchange {s.get('key')} -> {to}: {msg}")
        self.call_from_thread(self.notify, msg, timeout=8)

    def action_retire(self) -> None:
        s = self.detail if self.detail and self.detail.get("key") == self.selected else None
        rules = fmt.instructions(s or {})
        if not rules:
            self.notify("no instructions in force to retire", severity="warning"); return
        self.pending_rules = (s["key"], [r["id"] for r in rules])
        self._show_input("retire", f"retire which instruction (1-{len(rules)})? enter")

    @work(group="send", thread=True)
    def send_work(self, workstream: str, body: str) -> None:
        try:
            mid = self.client.send_workstream(workstream, body, self.this_host)
            msg, sev = f"message {mid} queued for {fmt.workstream_label(workstream)}", "information"
        except Exception as e:                          # noqa: BLE001
            msg, sev = f"not sent: {e}", "error"
        self.call_from_thread(self.notify, msg, severity=sev, timeout=6)
        self.call_from_thread(self.load_detail)

    @work(group="send", thread=True)
    def retire_row(self, key: str, row_id: int) -> None:
        try:
            ok = self.client.retire(key, row_id)
            msg, sev = ("retired", "information") if ok else ("already retired", "warning")
        except Exception as e:                          # noqa: BLE001
            msg, sev = f"not retired: {e}", "error"
        self.call_from_thread(self.notify, msg, severity=sev, timeout=6)
        self.call_from_thread(self.load_detail)

    def action_message_buffer(self) -> None:
        s = self._selected_session()
        if not s:
            return
        why = fmt.can_message(s)
        if why:
            self.notify(why, severity="warning"); return
        import subprocess
        r = subprocess.run(["tmux", "show-buffer"], capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            self.notify("no tmux paste buffer (select text in copy mode first)", severity="warning"); return
        body, key, lines = r.stdout, s["key"], r.stdout.count("\n") + 1
        self.confirm(f"send {lines} line(s) \"{fmt.one_line(body, 30)}\" to it?", lambda: self.send_message(key, body))

    @work(group="send", thread=True)
    def send_message(self, key: str, body: str, answers: str | None = None) -> None:
        try:
            mid = self.client.send(key, body, self.this_host, answers)
            msg, sev = f"{'answer' if answers else 'message'} {mid} queued", "information"
        except Exception as e:                          # noqa: BLE001
            msg, sev = f"not sent: {e}", "error"
        self.call_from_thread(self.notify, msg, severity=sev, timeout=6)
        self.call_from_thread(self.load_detail)

    def action_filter(self) -> None:
        self._show_input("filter", "filter sessions (title, repo, key)…", self.text_filter)

    def _hide_input(self) -> None:
        f = self.query_one("#filter", Input); f.remove_class("visible"); f.can_focus = False
        self.query_one("#table").focus()

    def action_clear_filter(self) -> None:
        if self.input_mode == "filter":
            self.text_filter = ""; self.query_one("#filter", Input).value = ""; self.render_table()
        else:                                           # Esc out of a message/answer/confirm: nothing is sent
            self.input_mode, self.pending_reply, self.pending_prompt = "filter", None, None
            self.query_one("#filter", Input).value = ""
        self._hide_input()

    def on_input_changed(self, ev: Input.Changed) -> None:
        if self.input_mode == "filter":
            self.text_filter = ev.value; self.render_table()

    def action_detach(self) -> None:
        """Inside tmux, q detaches (sessions keep running, `ctx tmux` brings them back); outside it just quits."""
        if tmux.in_tmux():
            try:
                tmux.detach(); return
            except Exception as e:                      # noqa: BLE001
                self.notify(f"tmux: {e}", timeout=6); return
        self.exit()

    def action_kill_all(self) -> None:
        if not tmux.in_tmux():
            self.exit(); return
        ps = tmux.panes()
        agents = tmux.agent_panes(ps)
        keys = {p.key for p in agents}
        busy = sum(1 for x in self.sessions if x["key"] in keys and x.get("live_status") == "busy")
        self.confirm(f"end tmux session '{tmux.my_session(ps)}' and all {len(agents)} agents" + (f" ({busy} busy)" if busy else "") + "?",
                     tmux.kill_all)

    def confirm(self, prompt: str, yes) -> None:
        """One key: y runs ``yes``, anything else cancels."""
        self.push_screen(Confirm(prompt), lambda ok: yes() if ok else self.notify("cancelled", timeout=3))

    def action_end_session(self) -> None:
        """k: end the selected session's agent, after a y. One running in a tmux on its own host is ended over there
        (its transcript stays, and the local window showing it closes)."""
        s = self._selected_session()
        if not s:
            return
        if not tmux.in_tmux():
            self.notify("k ends sessions in ctx's tmux (ctx tmux); quit this one where it runs", severity="warning"); return
        reach = self.reach.get(s["key"], "")
        p = tmux.agent_pane(s["key"])
        # a board that hasn't settled which host it itself runs on has nothing to call remote, so it ends what it
        # can see the way it always did.
        here = _here(self)
        # reach "attach", or a window here carrying its tag: either way the agent itself runs over there, so that's
        # where k has to end it.
        if here is not None and tmux._is_remote(s, here) and (reach == "attach" or p is not None):
            self.confirm(f"end {fmt.one_line(fmt.display_title(s), 30)} ({fmt.HARNESS.get(s['harness'], s['harness'])}"
                         f"·{s['host_id']}) on {s['host_id']}?",
                         lambda: self.end_remote_session(s))
            return
        if p is None:
            why = ("it runs in another terminal: quit it there" if reach == "foreign" else
                   tmux.reach_help(s, reach, self.cfg.ssh) if reach == "remote" else "it isn't running")
            self.notify(f"can't end it from here: {why}", severity="warning", timeout=6); return
        busy = " (busy: it's working)" if s.get("live_status") == "busy" else ""
        self.confirm(f"end {fmt.one_line(fmt.display_title(s), 30)} ({fmt.HARNESS.get(s['harness'], s['harness'])}·{s['host_id']}){busy}?",
                     lambda: self.notify(f"ending {fmt.one_line(fmt.display_title(s), 30)}" if tmux.end_agent(p) else "its agent had already exited", timeout=4))

    @work(exclusive=True, group="tmux", thread=True)
    def end_remote_session(self, s: dict) -> None:
        """k on a session running in a tmux on its own host: kill its pane there over ssh."""
        try:
            msg = tmux.end_remote(s, self.cfg.ssh)
        except Exception as e:                          # noqa: BLE001
            msg = f"tmux: {e}"
        popup_log(f"end {s['key']} on {s.get('host_id')}: {msg}")
        self.call_from_thread(self.notify, msg, timeout=6)
        self.refresh_data()

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        mode, value = self.input_mode, ev.value
        self._hide_input()
        if mode == "reply":
            target, self.pending_reply = self.pending_reply, None
            self.input_mode = "filter"; self.query_one("#filter", Input).value = ""
            if target and value.strip():
                self.send_message(target[0], value, target[1])
            return
        if mode == "retire":
            target, self.pending_rules = self.pending_rules, None
            self.input_mode = "filter"; self.query_one("#filter", Input).value = ""
            n = value.strip()
            if target and n.isdigit() and 1 <= int(n) <= len(target[1]):
                self.retire_row(target[0], target[1][int(n) - 1])
            elif target:
                self.notify(f"no instruction {n!r}", severity="warning")
            return
        if mode.startswith("work:"):
            self.input_mode = "filter"; self.query_one("#filter", Input).value = ""
            if value.strip():
                self.send_work(mode.split(":", 1)[1], value)
            return
        if mode.startswith("msg:"):
            self.input_mode = "filter"; self.query_one("#filter", Input).value = ""
            if value.strip():
                self.send_message(mode.split(":", 1)[1], value)
            return
        if mode.startswith("new@"):
            # "new@<host>:<harness>": start it there. Its profiles are its own, so there's no account to carry and
            # the directory typed is a path on that machine (see tmux.new_remote_agent).
            host, harness, _ = (mode[4:].split(":") + ["", ""])[:3]
            self.start_remote_agent(host, harness, value)
            self.input_mode = "filter"; self.query_one("#filter", Input).value = ""
            return
        if mode.startswith("newm:"):
            # t on a drifted prompt: start one with it, then stop the session that got it by mistake
            _, harness, profile = (mode[4:].split(":", 2) + [""])[:3]
            self.input_mode = "filter"; self.query_one("#filter", Input).value = ""
            s, row = self.pending_prompt or (None, {})
            self.pending_prompt = None
            if s:
                self.start_with_prompt(harness, value, profile or None, s, row)
            return
        if mode.startswith("new:"):
            _, harness, profile = (mode.split(":", 2) + [""])[:3]
            self.start_agent(harness, value, profile or None)
            self.input_mode = "filter"; self.query_one("#filter", Input).value = ""

    @work(group="misplace", thread=True)
    def start_with_prompt(self, harness: str, cwd: str, profile: str | None, s: dict, row: dict) -> None:
        """A prompt that drifted off this session's purpose starts a session of its own; if this one's agent already
        had it, that agent is stopped and told to disregard the prompt."""
        if row.get("source") == "llm":
            self.interrupt_session(s)
            try:
                self.client.send(s["key"], mis_disregard(row), self.this_host)
            except Exception:                           # noqa: BLE001 - the new session is what matters here
                pass
        self.start_agent(harness, cwd, profile, row.get("prompt") or "")
        self.misplace_state(row, "moved")


def run(cfg: Config, host: str | None = None) -> None:
    CtxApp(cfg, host).run()
