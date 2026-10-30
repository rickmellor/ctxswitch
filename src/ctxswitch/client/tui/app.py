"""ctx tui — the operator board.

Built as a narrow sidebar (a tmux pane of ~45–60 columns next to the agent sessions), stacked vertically:

  ┌ ctx · <host filter> · N live · N waiting on you ┐
  ├ sessions, numbered, in the order they joined ─┤
  │ 1 ◐  3m cc·laptop ctxswitch@main  title…      │
  │   ↳ what it's waiting on                      │
  ├ ledger of the selected session ──────────────┤
  │ state / open asks / last exchange / decisions│
  └──────────────────────────────────────────────┘
Keys: ? opens the full list (Help). h host filter · e toggle ended · v mark viewed · enter show/resume · c/p new claude/pi session (asks for a
directory, default = selected session's) · m message the selected session · M send it the tmux paste buffer (after
a y/n) · a answer its open question (a quick-reply message) · w group by workstream · r refresh · / filter · q quit
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
from datetime import datetime, timezone

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
from ctxswitch.client.notify import BOX_ITEMS, OPEN_RC, SETTLE_S, Notifier, item_id, log as popup_log, text as popup_text, tmux_box, tmux_popup
from ctxswitch.client.launch import open_tab
from ctxswitch.client import move as mv
from ctxswitch.common.config import Config
from ctxswitch.hub.tracker.recap import anchor_of as _anchor

STATE_STYLE = {"busy": "bold green", "waiting": "bold yellow", "idle": "cyan", "shell": "magenta", "unknown": "dim", "ended": "dim"}
CLAIM_STYLE = {"done": "green", "tests_pass": "green", "tests_fail": "red", "blocked": "yellow"}
MSG_STYLE = {"queued": "yellow", "sent": "yellow", "delivered": "green", "replied": "green", "failed": "red"}
REACH_STYLE = {"pane": "bold", "resume": "", "ssh": "blue", "foreign": "red", "remote": "blue", "none": "dim"}
SLOTS = 9                                          # Alt+1…9 open the session with that number


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
HARNESS_NAME = {"claude": "Claude Code", "pi": "pi", "claude-win": "Windows-native Claude Code", "agy": "Google Antigravity"}
MSG_HELP = {"queued": "queued: waiting for its host to pick it up", "sent": "sent: written into the session",
            "delivered": "delivered: the agent is working on it", "replied": "replied: that exchange finished",
            "failed": "failed: the reason is shown; send it again", "cancelled": "cancelled"}
CLAIM_HELP = {"done": "the agent says it's done", "tests_pass": "it says tests pass", "tests_fail": "it says tests fail",
              "blocked": "it says it's blocked"}
SUMMARY_TIP = ("The host filter (h), how many sessions are live, and how many are waiting on you. A waiting session's row "
               "says what it's waiting on (↳); ○ idle ones finished their turn: your move.")


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


def summary_line(sessions: list[dict], host: str | None) -> Text:
    live = sum(1 for x in sessions if fmt.session_state(x) != "ended")
    waiting = sum(1 for x in sessions if fmt.session_state(x) == "waiting")
    t = Text.assemble(("ctx", "bold"), f" · {host or 'all'} · {live} live")
    t.append(f" · {waiting} waiting on you" if waiting else " · nothing waiting", style="bold yellow" if waiting else "dim")
    return t


def legend_sections() -> list[tuple[str, list[tuple[str, str, str]]]]:
    """Every glyph on the board as (section, [(glyph, style, words)]), from the same tables the board draws with."""
    return [
        ("SESSION STATE (first column)", [(g, STATE_STYLE[st], STATE_HELP[st]) for st, g in fmt.GLYPH.items()]),
        ("ENTER (before the host)", [("▶", "bold", "it's on the left now")] +
         [(g, REACH_STYLE.get(r, ""), tmux.REACH_HELP[r]) for r, g in tmux.REACH_GLYPH.items() if r != "none"]),
        ("ROW", [("1", "dim", "its number: Alt+1…9 opens it from any pane of the ctx session"),
                 ("3m", "dim", "time in this state (live), or since it was last active (ended)")] +
         [(h, "dim", f"{HARNESS_NAME.get(k, k)}, then its host ({h}·devbox)") for k, h in fmt.HARNESS.items()] +
         [("◐4m", "bold yellow", "a question has waited this long"), ("+2", "bold cyan", "exchanges since you last looked"),
          ("▾", "dim", "a workstream header (w): this repo + branch on any host; m messages the work")]),
        ("WAITING ON YOU", [("↳", "yellow", "under a waiting row: the question it asked, or the dialog it has up"),
                            ("2 waiting", "bold yellow", "the header: how many sessions wait on you (and how many are live)")]),
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
    (("n",), "n", "new session: pick Claude Code (one entry per account), pi or Antigravity, then a directory "
                  "(default: the selected session's). For Claude, 'max: directory' also picks the account"),
    (("m",), "m", "message the selected session (Enter sends, Esc cancels). On a workstream header: message the work"),
    (("W",), "W", "message the selected session's workstream: whichever session is, or next is, live on it"),
    (("M",), "M", "send it the tmux paste buffer (what you last copied in copy mode), after a y"),
    (("k",), "k", "end the selected session's agent, after a y (it stays resumable: Enter brings it back)"),
    (("a",), "a", "answer the question it's waiting on (plain-text questions; dialogs are answered in the session)"),
    (("x",), "x", "exchange: move the selected Claude session to the other account ([[claude_profiles]]: bedrock ⇄ max) "
                  "and resume it there with that profile's model. A running one in ctx's tmux is ended first, after a y. "
                  "With three or more profiles it asks which"),
    (("i",), "i", "retire one of YOUR INSTRUCTIONS that no longer holds: type its number"),
    (("w",), "w", "group the list by workstream (repo@branch across hosts), or back to a flat list"),
    (("h",), "h", "host filter: this host, all hosts, each other host"),
    (("slash", "escape"), "/  Esc", "filter by text (title, repo, key, last prompt); Esc clears it, or cancels an input"),
    (("e",), "e", "show or hide ended sessions"),
    (("v",), "v", "mark the selected session viewed (opening it does too, on any machine)"),
    (("r",), "r", "refresh now (the board also updates live)"),
    (("p",), "p", "pop-ups off / on for this run"),
    (("question_mark",), "?", "this help (Esc, ? or q closes it)"),
    (("q",), "q", "detach: leave tmux, everything keeps running; `ctx tmux` brings it back"),
    (("Q",), "Q", "quit all: end the tmux session and every agent in it, after a y"),
]
HELP_TIPS = [
    ("Where things are", ("The header: how many sessions are live and how many wait on you. The list: every session, "
     "in the order it joined (so its number stays put); a waiting one says what it's waiting on (↳). Below it: the "
     "selected session's detail (recap, what you told it, its questions and answers, messages, the last exchange).")),
    ("Sending a message", ("m types a message into the session as a new prompt: now if it's live, else when it next runs. Its "
     "state shows under MESSAGES (… → ✓ ↩ ✗). `ctx send --cancel <id>` withdraws a queued one.")),
    ("Answering", ("a answers a question the agent asked in its reply. A dialog (permission, plan, AskUserQuestion, pi "
     "prompt) waits on a keypress in its own terminal: Enter opens it.")),
    ("Agents", ("Agents can read this board and message each other through the ctxswitch MCP tools. Their messages "
     "say which session and host sent them, and never count as yours.")),
    ("Ending a session", ("k ends the selected session's agent after a y (the transcript stays: Enter resumes it). "
     "Quitting it yourself (/exit in Claude Code) does the same. Either way the next agent takes its place on the "
     "left and the ended one drops off the list (e shows ended ones). Q ends everything in the ctx session.")),
    ("Restarting the board", ("`ctx tmux --restart` restarts it on new code; your agents keep running. If the board's "
     "pane is ever gone, `ctx tmux` puts it back.")),
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
    lines.append(f"{HARNESS_NAME.get(s.get('harness', ''), s.get('harness', ''))} on {s.get('host_id', '?')} · {fmt.repo_label(s)}")
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


KEY_NAMES = {"enter": "↵", "slash": "/", "question_mark": "?"}


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

    def __init__(self, options: list[tuple[str, str, str | None]]):
        super().__init__()
        self.options = options                          # (label, harness, profile)

    def compose(self) -> ComposeResult:
        t = Text("new session: ", style="bold")
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


class CtxApp(App):
    CSS = """
    #summary { height: 1; padding: 0 1; background: $panel; }
    #body { height: 1fr; }
    #table { height: auto; max-height: 50%; }
    #detail { height: 1fr; border-top: solid $surface; padding: 0 1; }
    #filter { display: none; }                 /* in the flow above the footer, so the footer never covers it */
    #filter.visible { display: block; }
    .h { color: $text-muted; }
    #keys { height: auto; background: $panel; padding: 0 1; }
    """
    # the key bar (key_bar) wraps in the narrow pane: most-used first, short labels
    BINDINGS = [
        Binding("enter", "resume", "open", show=True), Binding("n", "new_session", "new"),
        Binding("m", "message", "msg"), Binding("a", "reply", "answer"), Binding("M", "message_buffer", show=False),
        Binding("k", "end_session", "end"), Binding("q", "detach", "detach"), Binding("Q", "kill_all", "quit all"),
        Binding("h", "cycle_host", "filter host"), Binding("slash", "filter", "find"), Binding("e", "toggle_ended", "ended"),
        Binding("v", "mark_viewed", "viewed"), Binding("w", "toggle_ws", "group"), Binding("r", "refresh", "refresh"),
        Binding("p", "toggle_popups", "pop-ups"), Binding("x", "exchange", "exchange"), Binding("i", "retire", "retire"), Binding("question_mark", "help", "help"),
        Binding("W", "message_work", "msg work", show=False),
        Binding("escape", "clear_filter", show=False),
        *(Binding(f"{slot_key(n)},alt+{n}", f"slot({n})", show=False) for n in range(1, SLOTS + 1)),
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
        self.selected: str | None = None
        self.change = -1
        self.visible_key: str | None = None          # session shown in the tmux viewer pane
        self.on_screen: set[str] = set()             # sessions in the tmux window being looked at (no pop-ups for them)
        self.reach: dict[str, str] = {}             # key -> tmux.REACH_GLYPH key (how Enter can open it)
        self.input_mode = "filter"                   # what the bottom Input is for: "filter" | "new:<harness>" | "msg:<key>" | …
        self.pending_reply: tuple[str, str] | None = None   # (session key, ask ref) the reply input answers
        self.pending_rules: tuple[str, list[int]] | None = None   # (session key, row ids by number) x retires one of
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

    def compose(self) -> ComposeResult:
        yield Static("", id="summary")
        with Vertical(id="body"):
            yield Board(id="table", cursor_type="row", zebra_stripes=True, show_header=False)
            yield VerticalScroll(Static("", id="detail_text"), id="detail")
        inp = Input(placeholder="filter sessions (title, repo, key)…", id="filter")
        inp.can_focus = False                     # only focusable while shown via "/"
        yield inp
        yield KeyBar(id="keys")

    def on_mount(self) -> None:
        t = self.query_one("#table", DataTable)
        t.add_columns("#", " ", "age", "session")
        self.title = "ctxswitch"
        self.query_one("#summary", Static).tooltip = SUMMARY_TIP
        self.join_tmux()
        t.focus()
        self.refresh_data()
        self.set_interval(10, self.refresh_data)
        self.listen_ws()

    # -- data -----------------------------------------------------------------------------------------------------
    @work(exclusive=True, thread=True)
    def refresh_data(self) -> None:
        try:
            data = self.client.sessions(self.host_filter, live_only=False)
            att = self.client.attention(self.host_filter)["items"]
            hosts = [h["host_id"] for h in self.client.hosts()]
        except Exception as e:                          # noqa: BLE001
            self.call_from_thread(self.sub_title_set, f"hub unreachable: {e.__class__.__name__}")
            return
        try:
            tmux.reap_viewer()                          # an agent exited to a bare shell in the viewer: move on
            ps = tmux.panes()
            self.visible_key = tmux.visible_key(ps, sessions=data["sessions"])
            self.on_screen = tmux.on_screen_keys(ps, sessions=data["sessions"])
            self.reach = tmux.reach_all(data["sessions"], self.this_host, self.cfg.ssh)
        except Exception:                               # noqa: BLE001 - tmux is optional
            self.visible_key = None
            self.on_screen = set()
            self.reach = {}
        self.call_from_thread(self.apply_data, data["sessions"], att, hosts, data["change"])

    def sub_title_set(self, s: str) -> None:
        self.sub_title = s

    def apply_data(self, sessions: list[dict], attention: list[dict], hosts: list[str], change: int) -> None:
        self.change = change
        self.hosts = hosts
        self.sessions = sessions
        live = sum(1 for x in sessions if fmt.session_state(x) != "ended")
        self.sub_title = f"{self.host_filter or 'all'} · {live} live"
        self.query_one("#summary", Static).update(summary_line(sessions, self.host_filter))
        self.popup(attention)
        self.render_table()
        self.render_detail()

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
        new = self.notifier.fresh(self.attention, skip=self.on_screen)
        if not new or not self.popups:                  # off: what turns up meanwhile is on the board, not popped later
            return
        msg, ids = popup_text(new), [item_id(i) for i in new]
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

    def action_slot(self, n: int) -> None:
        """Alt+N: open the session numbered N on the board, as Enter on its row does."""
        if not 1 <= n <= len(self.slots):
            self.notify(f"no session {n} on the board", severity="warning"); return
        self.open_key(self.slots[n - 1])

    def open_key(self, key: str) -> None:
        """Select a session on the board and open it, as Enter on its row does."""
        if not any(x["key"] == key for x in self.sessions):
            self.notify(f"{key} isn't on the board any more", timeout=6)
            return
        self.selected = key
        t = self.query_one("#table", DataTable)
        try:
            t.move_cursor(row=t.get_row_index(key))
        except Exception:                               # noqa: BLE001 - filtered out of the table: open it anyway
            pass
        self.action_resume()

    def action_toggle_popups(self) -> None:
        self.popups = not self.popups
        self.notify(f"pop-ups {'on' if self.popups else 'off'}", timeout=3)

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
        for s in visible:
            st = fmt.session_state(s)
            last = s.get("last_turn") or {}
            asks = s.get("open_asks") or []
            when = s.get("status_since") if s.get("live_status") in LIVE else s.get("last_activity")
            style = STATE_STYLE.get(st, "")
            harness = fmt.harness_label(s, self.cfg.claude_profiles)
            where = harness if self.host_filter else f"{harness}·{s.get('host_id', '?')}"   # a host filter implies the host
            label = Text()
            reach = self.reach.get(s["key"], "")
            if s["key"] == self.visible_key:
                label.append("▶ ", style="bold")
            elif reach:
                label.append(tmux.REACH_GLYPH[reach] + " ", style=REACH_STYLE.get(reach, "dim"))
            if reach in ("foreign", "remote"):
                style = (style + " dim").strip()          # can't be opened from here
            ask_mark = f" ◐{fmt.age(asks[0].get('asked_at'), now)}" if asks else ""
            seen_mark = f" +{s['unseen']}" if s.get("unseen") else ""   # exchanges since you last looked
            # the title keeps 8 columns and the row never overflows (a scrollbar would hide the last row): the repo
            # gives way first, then the host, then the +N, then the ◐ age (the age column and the ↳ line say it too)
            fixed = width - len(label.plain) - 1 - 8      # after the glyph, the space after `where` and the title's 8
            if fixed - len(where) - len(ask_mark) - len(seen_mark) < 0 and where != harness:
                where = harness
            if fixed - len(where) - len(ask_mark) - len(seen_mark) < 0:
                seen_mark = ""
            if fixed - len(where) - len(ask_mark) - len(seen_mark) < 0:
                ask_mark = ""
            label.append(f"{where} ", style="dim")
            repo_w = min(18 if width >= 40 else 10 if width >= 28 else 0,
                         width - len(label.plain) - len(ask_mark) - len(seen_mark) - 8 - 1)
            if repo_w >= 4:
                label.append(fmt.one_line(fmt.repo_label(s), repo_w) + " ", style="dim" if st in ("ended", "unknown") else "")
            rest = width - len(label.plain) - len(ask_mark) - len(seen_mark)
            label.append(fmt.one_line(fmt.display_title(s) or "(no prompt yet)", max(8, rest)), style=style)
            label.append(ask_mark, style="bold yellow"); label.append(seen_mark, style="bold cyan")
            self.slots.append(s["key"])
            n = len(self.slots)
            on = waiting_on(s)
            if on:                                     # what it's waiting on, under its own row
                label.append("\n↳ " + fmt.one_line(on, max(8, width - 2)), style="yellow")
            t.add_row(Text(str(n) if n <= SLOTS else "", style="dim"), Text(fmt.state_glyph(s), style=style),
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
        except Exception:                               # noqa: BLE001
            return
        if s:
            self.call_from_thread(self.render_detail, s)

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

    def render_detail(self, s: dict | None = None) -> None:
        w = self.query_one("#detail_text", Static)
        if s is None:
            if not self.selected:
                w.update(Text("select a session", style="dim")); return
            self.load_detail(); return
        if s.get("key") != self.selected:               # a slow load for a row you've moved off
            return
        self.detail = s
        now = datetime.now(timezone.utc)
        st = fmt.session_state(s)
        t = Text()
        t.append(f"{fmt.state_glyph(s)} {fmt.display_title(s) or '-'}\n", style="bold")
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
        if s.get("purpose"):
            t.append("for: ", style="bold")
            t.append(s["purpose"] + "\n")
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
        opens = [x for x in s.get("asks", []) if not x["answered"]]
        if opens:
            t.append("WAITING ON YOU\n", style="bold yellow")
            for x in opens:
                t.append(f"◐ {fmt.age(x.get('asked_at'), now)} [{x['kind']}] ", style="yellow")
                t.append(x['text'] + "\n")
            ask, why = fmt.reply_target(s)
            t.append("  a = answer it here\n" if ask else f"  {why}\n", style="dim")
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
        turns = s.get("turns", [])
        if s.get("recap"):
            stale = s.get("recap_anchor") != _anchor(s)
            t.append("RECAP" + (" (updating…)" if stale else "") + "\n", style="bold")
            t.append(s["recap"] + "\n", style="cyan")
            for line in (s.get("reentry") or "").splitlines():
                t.append(f"› {line}\n")
            t.append("\n")
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
        if turns:
            last = turns[-1]
            # "you" = the operator's last real prompt; "agent" = the newest agent text, even if a task notification
            # or peer message started the turn that produced it
            you = next((x for x in reversed(turns) if x.get("user_origin") in ("human", "peer")), None)
            said = next((x for x in reversed(turns) if x.get("assistant_text")), None)
            t.append("LAST EXCHANGE\n", style="bold")
            if you is not None:
                t.append(f"{fmt.who(you)} ({fmt.age(you.get('started_at'), now)}): ", style="bold")
                t.append(fmt.one_line(you.get("user_text"), 400) + "\n")
            if said is not None:
                t.append(f"agent ({fmt.age(said.get('ended_at') or said.get('started_at'), now)}): ", style="bold")
                t.append(fmt.one_line(said.get("assistant_text"), 700) + "\n")
            tally = {k: v for k, v in (last.get("tally") or {}).items() if v}
            if tally or last.get("events"):
                t.append("  " + ", ".join(f"{v} {k}" for k, v in tally.items()) + ("  " + " ".join(last.get("events", [])) if last.get("events") else "") + "\n", style="dim")
            if not last.get("complete"):
                t.append(f"  working on a {last.get('user_origin')} turn…\n" if last.get("user_origin") != "human" else "  (in progress)\n", style="green")
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

    def action_new_session(self) -> None:
        """n: pick the agent (Claude per account, pi, Antigravity), then the directory."""
        if not tmux.in_tmux():
            self.notify("new sessions need the tmux manager (ctx tmux)", severity="warning"); return
        options = new_session_options(self.cfg)
        if not options:
            self.notify("no agent found on PATH (claude, pi, agy)", severity="warning"); return
        self.push_screen(Picker(options), self._picked)

    def _picked(self, choice: tuple[str, str | None] | None) -> None:
        if not choice:
            return
        harness, profile = choice
        what = f"{harness} · {profile}" if profile else {"agy": "antigravity"}.get(harness, harness)
        hint = ""
        if harness == "claude":
            others = [p.name for p in self.cfg.claude_profiles if p.name != profile]
            if others:
                hint = f", or '{others[0]}: [path]' for that account"
        self._show_input(f"new:{harness}:{profile or ''}", f"directory for the new {what} session, enter to start{hint}", self._selected_cwd())

    @work(exclusive=True, group="tmux", thread=True)
    def start_agent(self, harness: str, cwd: str, profile: str | None = None) -> None:
        try:
            if harness == "claude":
                typed, cwd = split_profile(cwd, [p.name for p in self.cfg.claude_profiles])
                profile = typed or profile
                msg = tmux.new_agent(harness, cwd, profile=profile, profiles=self.cfg.claude_profiles)
            else:
                msg = tmux.new_agent(harness, cwd)
        except Exception as e:                          # noqa: BLE001
            msg = f"tmux: {e}"
        popup_log(f"new {harness}{f'[{profile}]' if profile else ''} in {cwd}: {msg}")
        self.call_from_thread(self.notify, msg, timeout=6)

    def action_help(self) -> None:
        self.push_screen(Help())

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

    def action_message_work(self) -> None:
        """W, or m on a workstream header: a message for whichever session is (or next is) live on that work."""
        ws = self.selected[3:] if (self.selected or "").startswith("ws:") else (self._selected_session() or {}).get("workstream")
        if not ws:
            self.notify("not in a repo: no workstream to message", severity="warning"); return
        self._show_input(f"work:{ws}", f"message to {fmt.workstream_label(ws)} (the first session live on it, any host), enter to send")

    def action_exchange(self) -> None:
        """x: move the selected Claude session to another account (profile) and resume it there."""
        s = self._selected_session()
        if not s:
            return
        profiles = self.cfg.claude_profiles
        if len(profiles) < 2:
            self.notify("one claude profile only: add [[claude_profiles]] to config.toml to exchange", severity="warning"); return
        live = s.get("live_status") in LIVE
        try:                                            # everything but liveness: that's handled by ending it
            mv.check({**s, "live_status": "ended"}, mv.other_profiles(s, profiles)[0].name, profiles, self.this_host)
        except mv.CannotMove as e:
            self.notify(str(e), severity="warning"); return
        if live and not mv.can_end_here(s):
            self.notify("it's running outside ctx's tmux (another terminal, or another host): quit it there, then x",
                        severity="warning", timeout=6); return
        others = mv.other_profiles(s, profiles)
        if len(others) > 1:
            self.pending_exchange = (s, live)
            self._show_input("exchange", f"move to which profile ({', '.join(p.name for p in others)})? enter"); return
        to = others[0].name
        if live:
            busy = " (busy: it's working)" if s.get("live_status") == "busy" else ""
            self.confirm(f"end {fmt.one_line(fmt.display_title(s), 30)}{busy} and move it to {to}?",
                         lambda: self.exchange_session(s, to, end_first=True))
        else:
            self.exchange_session(s, to)

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
            self.input_mode, self.pending_reply = "filter", None
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
        """k: end the selected session's agent, after a y."""
        s = self._selected_session()
        if not s:
            return
        if not tmux.in_tmux():
            self.notify("k ends sessions in ctx's tmux (ctx tmux); quit this one where it runs", severity="warning"); return
        p = tmux.agent_pane(s["key"])
        if p is None:
            reach = self.reach.get(s["key"], "")
            why = ("it runs in another terminal: quit it there" if reach == "foreign" else
                   tmux.reach_help(s, reach, self.cfg.ssh) if reach == "remote" else "it isn't running")
            self.notify(f"can't end it from here: {why}", severity="warning", timeout=6); return
        busy = " (busy: it's working)" if s.get("live_status") == "busy" else ""
        self.confirm(f"end {fmt.one_line(fmt.display_title(s), 30)} ({fmt.HARNESS.get(s['harness'], s['harness'])}·{s['host_id']}){busy}?",
                     lambda: self.notify(f"ending {fmt.one_line(fmt.display_title(s), 30)}" if tmux.end_agent(p) else "its agent had already exited", timeout=4))

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
        if mode == "exchange":
            pend, self.pending_exchange = getattr(self, "pending_exchange", None), None
            self.input_mode = "filter"; self.query_one("#filter", Input).value = ""
            if pend and value.strip():
                s, live = pend
                self.exchange_session(s, value.strip(), end_first=live)
            return
        if mode.startswith("new:"):
            _, harness, profile = (mode.split(":", 2) + [""])[:3]
            self.start_agent(harness, value, profile or None)
            self.input_mode = "filter"; self.query_one("#filter", Input).value = ""


def run(cfg: Config, host: str | None = None) -> None:
    CtxApp(cfg, host).run()
