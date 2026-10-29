"""ctx tui — the operator board.

Built as a narrow sidebar (a tmux pane of ~45–60 columns next to the agent sessions), stacked vertically:

  ┌ attention: what needs you, oldest wait first ┐
  ├ sessions (host filter: this | all | <host>) ─┤
  │ ◐  3m cc·laptop ctxswitch@main  title…       │
  ├ ledger of the selected session ──────────────┤
  │ state / open asks / last exchange / decisions│
  └──────────────────────────────────────────────┘
Keys: h host filter · e toggle ended · v mark viewed · enter show/resume · c/p new claude/pi session (asks for a
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
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, Input, Static

from ctxswitch.common.schema import LIVE
from ctxswitch.client import fmt
from ctxswitch.client.hubclient import HubClient
from ctxswitch.client import tmux
from ctxswitch.client.notify import BOX_ITEMS, OPEN_RC, SETTLE_S, Notifier, item_id, log as popup_log, text as popup_text, tmux_box, tmux_popup
from ctxswitch.client.launch import open_tab
from ctxswitch.common.config import Config
from ctxswitch.hub.tracker.recap import anchor_of as _anchor

STATE_STYLE = {"busy": "bold green", "waiting": "bold yellow", "idle": "cyan", "shell": "magenta", "unknown": "dim", "ended": "dim"}
CLAIM_STYLE = {"done": "green", "tests_pass": "green", "tests_fail": "red", "blocked": "yellow"}
MSG_STYLE = {"queued": "yellow", "sent": "yellow", "delivered": "green", "replied": "green", "failed": "red"}
REACH_STYLE = {"pane": "bold", "resume": "", "ssh": "blue", "foreign": "red", "remote": "blue", "none": "dim"}


class CtxApp(App):
    CSS = """
    #attention { height: auto; max-height: 6; border: round $warning; padding: 0 1; }
    #attention.empty { border: round $surface; color: $text-muted; }
    #body { height: 1fr; }
    #table { height: auto; max-height: 50%; }
    #detail { height: 1fr; border-top: solid $surface; padding: 0 1; }
    #filter { display: none; }                 /* in the flow above the footer, so the footer never covers it */
    #filter.visible { display: block; }
    .h { color: $text-muted; }
    """
    # one footer line in a ~40-column side pane: most-used first, short labels; the rest fall off the right edge
    BINDINGS = [
        Binding("enter", "resume", "open", show=True), Binding("c", "new_claude", "claude"), Binding("p", "new_pi", "pi"),
        Binding("m", "message", "msg"), Binding("a", "reply", "answer"), Binding("M", "message_buffer", show=False),
        Binding("q", "detach", "detach"), Binding("Q", "kill_all", "quit all"),
        Binding("h", "cycle_host", "filter host"), Binding("slash", "filter", "find"), Binding("e", "toggle_ended", "ended"),
        Binding("v", "mark_viewed", "viewed"), Binding("w", "toggle_ws", "group"), Binding("r", "refresh", "refresh"),
        Binding("n", "toggle_popups", "pop-ups"),
        Binding("escape", "clear_filter", show=False),
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
        self.reach: dict[str, str] = {}             # key -> tmux.REACH_GLYPH key (how Enter can open it)
        self.input_mode = "filter"                   # what the bottom Input is for: "filter" | "new:<harness>" | "msg:<key>" | "confirm:…"
        self.pending_body = ""                       # the tmux buffer waiting on a y/n
        self.pending_reply: tuple[str, str] | None = None   # (session key, ask ref) the reply input answers
        self.group_ws = False                        # w: rows grouped under workstream headers
        self.notifier = Notifier(cfg.notify_idle)
        self.popups = cfg.notify                     # n toggles it for this run; off still tracks what's been seen
        self.box = None                              # our open tmux popup box (a display-popup process), if any
        self.attention: list[dict] = []              # the latest attention list, for the pop-up settle timer
        self.settling = False

    def compose(self) -> ComposeResult:
        yield Static("", id="attention")
        with Vertical(id="body"):
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True, show_header=False)
            yield VerticalScroll(Static("", id="detail_text"), id="detail")
        inp = Input(placeholder="filter sessions (title, repo, key)…", id="filter")
        inp.can_focus = False                     # only focusable while shown via "/"
        yield inp
        yield Footer(compact=True, show_command_palette=False)

    def on_mount(self) -> None:
        t = self.query_one("#table", DataTable)
        t.add_columns(" ", "age", "session")
        self.title = "ctxswitch"
        self.query_one("#attention", Static).border_title = "ctxswitch"
        if tmux.in_tmux():
            tmux.set_role(os.environ.get("TMUX_PANE"), "tui")   # lets an exiting agent recognise the viewer window
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
            self.visible_key = tmux.visible_key(sessions=data["sessions"])
            self.reach = tmux.reach_all(data["sessions"], self.this_host, self.cfg.ssh)
        except Exception:                               # noqa: BLE001 - tmux is optional
            self.visible_key = None
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
        self.query_one("#attention", Static).border_title = f"ctx · {self.host_filter or 'all'} · {live} live"
        self.render_attention(attention)
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
    def render_attention(self, items: list[dict]) -> None:
        w = self.query_one("#attention", Static)
        now = datetime.now(timezone.utc)
        if not items:
            w.update(Text("nothing needs you", style="dim")); w.add_class("empty"); return
        w.remove_class("empty")
        width = max(20, self.size.width - 4)
        t = Text()
        for i in items[:4]:
            head = f"◐{fmt.age(i.get('asked_at'), now):>5} "
            t.append(head, style="bold yellow")
            label = f"{fmt.one_line(i.get('title'), 18)}: " + (fmt.one_line(i['text'], 200) if i["item"] == "ask" else i["item"])
            t.append(fmt.one_line(label, width - len(head)) + "\n")
        if len(items) > 4:
            t.append(f"… {len(items) - 4} more", style="dim")
        t.rstrip()
        w.update(t)

    def popup(self, attention: list[dict]) -> None:
        """Pops up SETTLE_S after a change, from the attention list as it is then: a session reports "waiting" a moment
        before its question's text arrives, and the box should show the question."""
        self.attention = attention
        if not self.settling:
            self.settling = True
            self.set_timer(SETTLE_S, self.popup_now)

    def popup_now(self) -> None:
        self.settling = False
        new = self.notifier.fresh(self.attention, skip=self.visible_key)
        if not new or not self.popups:                  # off: what turns up meanwhile is on the board, not popped later
            return
        msg, ids = popup_text(new), [item_id(i) for i in new]
        if tmux.in_tmux() and self.cfg.notify_style == "popup" and (self.box is None or self.box.poll() is not None):
            try:
                self.box = tmux_box(new)
                popup_log(f"box skip={self.visible_key} {ids} {msg}")
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
        popup_log(f"{'tmux' if shown else 'toast'} skip={self.visible_key} {ids} {msg}")
        if not shown:
            self.notify(msg, timeout=8)                 # outside tmux (or tmux refused): the TUI's own toast

    def _visible(self) -> list[dict]:
        out = []
        for s in self.sessions:
            if not self.show_ended and fmt.session_state(s) == "ended":
                continue
            if self.text_filter:
                hay = " ".join([fmt.display_title(s), s.get("key", ""), fmt.repo_label(s), (s.get("last_turn") or {}).get("user_text") or ""]).lower()
                if self.text_filter.lower() not in hay:
                    continue
            out.append(s)
        return out

    def _label_width(self) -> int:
        return max(16, self.size.width - 14)          # glyph + age columns + paddings + scrollbar

    def render_table(self) -> None:
        t = self.query_one("#table", DataTable)
        now = datetime.now(timezone.utc)
        prev = self.selected
        width = self._label_width()
        t.clear()
        visible = self._visible()
        rows = 0
        if self.group_ws:                              # groups in the order of their most urgent member
            order: dict = {}
            for s in visible:
                order.setdefault(s.get("workstream"), []).append(s)
            visible = []
            for ws, members in order.items():
                hosts = sorted({m.get("host_id", "?") for m in members})
                head = Text(f"{fmt.one_line(fmt.workstream_label(ws), width - 8)} · {len(members)}", style="bold")
                if len(hosts) > 1:
                    head.append(f" · {' '.join(hosts)}", style="dim")
                t.add_row(Text("▾", style="dim"), Text(""), head, key=f"ws:{ws}"); rows += 1
                visible += members
        for s in visible:
            st = fmt.session_state(s)
            last = s.get("last_turn") or {}
            asks = s.get("open_asks") or []
            when = s.get("status_since") if s.get("live_status") in LIVE else s.get("last_activity")
            style = STATE_STYLE.get(st, "")
            where = f"{fmt.HARNESS.get(s.get('harness',''), s.get('harness',''))}·{s.get('host_id','?')}"
            if self.host_filter:                       # host is implied by the filter
                where = fmt.HARNESS.get(s.get("harness", ""), s.get("harness", ""))
            label = Text()
            reach = self.reach.get(s["key"], "")
            if s["key"] == self.visible_key:
                label.append("▶ ", style="bold")
            elif reach:
                label.append(tmux.REACH_GLYPH[reach] + " ", style=REACH_STYLE.get(reach, "dim"))
            if reach in ("foreign", "remote"):
                style = (style + " dim").strip()          # can't be opened from here
            label.append(f"{where} ", style="dim")
            repo_w = 18 if width >= 40 else 10 if width >= 28 else 0
            if repo_w:
                label.append(fmt.one_line(fmt.repo_label(s), repo_w) + " ", style="dim" if st in ("ended", "unknown") else "")
            rest = width - len(label.plain)
            label.append(fmt.one_line(fmt.display_title(s) or "(no prompt yet)", max(8, rest)), style=style)
            if asks:
                label.append(f" ◐{fmt.age(asks[0].get('asked_at'), now)}", style="bold yellow")
            if s.get("unseen"):                        # exchanges since you last looked
                label.append(f" +{s['unseen']}", style="bold cyan")
            t.add_row(Text(fmt.state_glyph(s), style=style), Text(f"{fmt.age(when, now):>4} ", style="dim"), label, key=s["key"])
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
            t.append(f"{fmt.state_glyph(s)} {s.get('host_id')}/{fmt.HARNESS.get(s.get('harness', ''), '')} ", style=STATE_STYLE.get(st, ""))
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
            t.append("YOUR MESSAGES\n", style="bold")
            asked = {a["ref"]: a for a in s.get("asks") or []}
            for m in msgs:
                t.append(f"{fmt.MSG_GLYPH.get(m['status'], '?')} ", style=MSG_STYLE.get(m["status"], "dim"))
                if m.get("ask_ref") in asked:
                    t.append(f"re \"{fmt.one_line(asked[m['ask_ref']]['text'], 60)}\": ", style="dim")
                t.append(fmt.one_line(m["body"], 160) + "\n")
                t.append(f"  {fmt.one_line(fmt.message_note(s, m), 300)}\n", style="dim" if m["status"] != "failed" else "red")
            t.append("\n")
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
            for r in rules:
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
                who = "you" if you.get("user_origin") == "human" else "peer"
                t.append(f"{who} ({fmt.age(you.get('started_at'), now)}): ", style="bold")
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
                t.append(f"{fmt.GLYPH.get(st, '?')} {x['host_id']}/{fmt.HARNESS.get(x['harness'], x['harness'])} ", style=STATE_STYLE.get(st, ""))
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

    def action_new_claude(self) -> None:
        if not tmux.in_tmux():
            self.notify("new sessions need the tmux manager (ctx tmux)", severity="warning"); return
        self._show_input("new:claude", "directory for the new claude session, enter to start", self._selected_cwd())

    def action_new_pi(self) -> None:
        if not tmux.in_tmux():
            self.notify("new sessions need the tmux manager (ctx tmux)", severity="warning"); return
        self._show_input("new:pi", "directory for the new pi session, enter to start", self._selected_cwd())

    @work(exclusive=True, group="tmux", thread=True)
    def start_agent(self, harness: str, cwd: str) -> None:
        try:
            msg = tmux.new_agent(harness, cwd)
        except Exception as e:                          # noqa: BLE001
            msg = f"tmux: {e}"
        self.call_from_thread(self.notify, msg, timeout=6)

    def _selected_session(self) -> dict | None:
        return next((x for x in self.sessions if x["key"] == self.selected), None)

    def action_message(self) -> None:
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
        self.pending_body = r.stdout
        lines = r.stdout.count("\n") + 1
        self._show_input(f"confirm:buffer:{s['key']}", f"send {lines} line(s) \"{fmt.one_line(r.stdout, 30)}\" to it? y/n")

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
            self.input_mode, self.pending_reply, self.pending_body = "filter", None, ""
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
        self._show_input("confirm:kill", f"end all {len(agents)} agents" + (f" ({busy} busy)" if busy else "") + "? y/n")
        self.notify(f"Q ends tmux session '{tmux.my_session(ps)}' and every agent in it", timeout=6)

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        mode, value = self.input_mode, ev.value
        self._hide_input()
        if mode == "confirm:kill":
            self.input_mode = "filter"; self.query_one("#filter", Input).value = ""
            if value.strip().lower() in ("y", "yes"):
                tmux.kill_all()
            return
        if mode.startswith("confirm:buffer:"):
            body, self.pending_body = self.pending_body, ""
            self.input_mode = "filter"; self.query_one("#filter", Input).value = ""
            if value.strip().lower() in ("y", "yes"):
                self.send_message(mode.split(":", 2)[2], body)
            return
        if mode == "reply":
            target, self.pending_reply = self.pending_reply, None
            self.input_mode = "filter"; self.query_one("#filter", Input).value = ""
            if target and value.strip():
                self.send_message(target[0], value, target[1])
            return
        if mode.startswith("msg:"):
            self.input_mode = "filter"; self.query_one("#filter", Input).value = ""
            if value.strip():
                self.send_message(mode.split(":", 1)[1], value)
            return
        if mode.startswith("new:"):
            self.start_agent(mode.split(":", 1)[1], value)
            self.input_mode = "filter"; self.query_one("#filter", Input).value = ""


def run(cfg: Config, host: str | None = None) -> None:
    CtxApp(cfg, host).run()
