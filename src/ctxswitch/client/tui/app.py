"""ctx tui — the operator board.

Built as a narrow sidebar (a tmux pane of ~45–60 columns next to the agent sessions), stacked vertically:

  ┌ attention: what needs you, oldest wait first ┐
  ├ sessions (host filter: this | all | <host>) ─┤
  │ ◐  3m cc·laptop ctxswitch@main  title…       │
  ├ ledger of the selected session ──────────────┤
  │ state / open asks / last exchange / decisions│
  └──────────────────────────────────────────────┘
Keys: h host filter · e toggle ended · v mark viewed · enter show/resume · c/p new claude/pi session (asks for a
directory, default = selected session's) · r refresh · / filter · q quit
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

from ctxswitch.client import fmt
from ctxswitch.client.hubclient import HubClient
from ctxswitch.client import tmux
from ctxswitch.client.launch import open_tab
from ctxswitch.common.config import Config

STATE_STYLE = {"busy": "bold green", "waiting": "bold yellow", "idle": "cyan", "shell": "magenta", "unknown": "dim", "ended": "dim"}


class CtxApp(App):
    CSS = """
    #attention { height: auto; max-height: 6; border: round $warning; padding: 0 1; }
    #attention.empty { border: round $surface; color: $text-muted; }
    #body { height: 1fr; }
    #table { height: auto; max-height: 50%; }
    #detail { height: 1fr; border-top: solid $surface; padding: 0 1; }
    #filter { dock: bottom; display: none; }
    #filter.visible { display: block; }
    .h { color: $text-muted; }
    """
    BINDINGS = [
        Binding("h", "cycle_host", "host"), Binding("e", "toggle_ended", "ended"), Binding("v", "mark_viewed", "viewed"),
        Binding("enter", "resume", "resume", show=True), Binding("r", "refresh", "refresh"),
        Binding("c", "new_claude", "new claude"), Binding("p", "new_pi", "new pi"),
        Binding("slash", "filter", "/ filter"), Binding("escape", "clear_filter", show=False), Binding("q", "quit", "quit"),
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
        self.input_mode = "filter"                   # what the bottom Input is for: "filter" | "new:claude" | "new:pi"

    def compose(self) -> ComposeResult:
        yield Static("", id="attention")
        with Vertical(id="body"):
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True, show_header=False)
            yield VerticalScroll(Static("", id="detail_text"), id="detail")
        inp = Input(placeholder="filter sessions (title, repo, key)…", id="filter")
        inp.can_focus = False                     # only focusable while shown via "/"
        yield inp
        yield Footer()

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
        except Exception:                               # noqa: BLE001 - tmux is optional
            self.visible_key = None
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
            label = f"{fmt.one_line(i.get('title'), 18)}: " + (fmt.one_line(i['text'], 200) if i["item"] == "ask" else "idle")
            t.append(fmt.one_line(label, width - len(head)) + "\n")
        if len(items) > 4:
            t.append(f"… {len(items) - 4} more", style="dim")
        t.rstrip()
        w.update(t)

    def _visible(self) -> list[dict]:
        out = []
        for s in self.sessions:
            if not self.show_ended and fmt.session_state(s) == "ended":
                continue
            if self.text_filter:
                hay = " ".join([s.get("title") or "", s.get("key", ""), fmt.repo_label(s), (s.get("last_turn") or {}).get("user_text") or ""]).lower()
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
        for s in visible:
            st = fmt.session_state(s)
            last = s.get("last_turn") or {}
            asks = s.get("open_asks") or []
            when = s.get("status_since") if s.get("live_status") in ("busy", "idle", "shell") else s.get("last_activity")
            style = STATE_STYLE.get(st, "")
            where = f"{fmt.HARNESS.get(s.get('harness',''), s.get('harness',''))}·{s.get('host_id','?')}"
            if self.host_filter:                       # host is implied by the filter
                where = fmt.HARNESS.get(s.get("harness", ""), s.get("harness", ""))
            label = Text()
            if s["key"] == self.visible_key:
                label.append("▶ ", style="bold")
            label.append(f"{where} ", style="dim")
            repo_w = 18 if width >= 40 else 10 if width >= 28 else 0
            if repo_w:
                label.append(fmt.one_line(fmt.repo_label(s), repo_w) + " ", style="dim" if st in ("ended", "unknown") else "")
            rest = width - len(label.plain)
            label.append(fmt.one_line(fmt.display_title(s) or "(no prompt yet)", max(8, rest)), style=style)
            if asks:
                label.append(f" ◐{fmt.age(asks[0].get('asked_at'), now)}", style="bold yellow")
            t.add_row(Text(fmt.state_glyph(s), style=style), Text(f"{fmt.age(when, now):>4} ", style="dim"), label, key=s["key"])
        t.styles.height = max(1, len(visible))        # grow with the list, up to the max-height cap
        if prev and prev in [s["key"] for s in visible]:
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
        try:
            s = self.client.session(key, turns=8)
        except Exception:                               # noqa: BLE001
            return
        if s:
            self.call_from_thread(self.render_detail, s)

    def render_detail(self, s: dict | None = None) -> None:
        w = self.query_one("#detail_text", Static)
        if s is None:
            if not self.selected:
                w.update(Text("select a session", style="dim")); return
            self.load_detail(); return
        now = datetime.now(timezone.utc)
        st = fmt.session_state(s)
        t = Text()
        t.append(f"{fmt.state_glyph(s)} {fmt.display_title(s) or '-'}\n", style="bold")
        t.append(f"{st} {fmt.age(s.get('status_since') or s.get('last_activity'), now)}", style=STATE_STYLE.get(st, ""))
        t.append(f" · {s.get('turn_count')} exch · {fmt.age(s.get('started_at'), now)} old", style="dim")
        if s.get("last_viewed_at"):
            t.append(f" · seen {fmt.age(s['last_viewed_at'], now)}", style="dim")
        t.append("\n")
        t.append(f"{fmt.short_key(s)} {fmt.repo_label(s)}\n", style="dim")
        t.append(f"{s.get('cwd') or '-'}\n", style="dim")
        model = (s['meta'].get('model') or '').rsplit('/', 1)[-1]
        t.append(f"{model} {s['meta'].get('permission_mode') or ''}\n\n", style="dim")
        opens = [x for x in s.get("asks", []) if not x["answered"]]
        if opens:
            t.append("WAITING ON YOU\n", style="bold yellow")
            for x in opens:
                t.append(f"◐ {fmt.age(x.get('asked_at'), now)} [{x['kind']}] ", style="yellow")
                t.append(x['text'] + "\n")
            t.append("\n")
        turns = s.get("turns", [])
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
        answered = [x for x in s.get("asks", []) if x["answered"]]
        if answered:
            t.append("DECISIONS\n", style="bold")
            for x in answered[-6:]:
                t.append(f"✓ {fmt.one_line(x['text'], 120)}\n", style="dim")
                t.append(f"  → {fmt.one_line(x.get('answer'), 160)}\n")
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

    def action_toggle_ended(self) -> None:
        self.show_ended = not self.show_ended; self.render_table()

    def action_refresh(self) -> None:
        self.refresh_data()

    def action_mark_viewed(self) -> None:
        if self.selected:
            self.client.viewed(self.selected, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
            self.notify("marked viewed"); self.load_detail()

    def action_resume(self) -> None:
        s = next((x for x in self.sessions if x["key"] == self.selected), None)
        if not s:
            return
        if tmux.in_tmux():
            self.show_in_tmux(s)
        else:
            self.notify(open_tab(s, self.this_host), timeout=8)

    @work(exclusive=True, group="tmux", thread=True)
    def show_in_tmux(self, s: dict) -> None:
        try:
            msg = tmux.show(s)
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

    def action_filter(self) -> None:
        self._show_input("filter", "filter sessions (title, repo, key)…", self.text_filter)

    def _hide_input(self) -> None:
        f = self.query_one("#filter", Input); f.remove_class("visible"); f.can_focus = False
        self.query_one("#table").focus()

    def action_clear_filter(self) -> None:
        if self.input_mode == "filter":
            self.text_filter = ""; self.query_one("#filter", Input).value = ""; self.render_table()
        self._hide_input()

    def on_input_changed(self, ev: Input.Changed) -> None:
        if self.input_mode == "filter":
            self.text_filter = ev.value; self.render_table()

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        mode, value = self.input_mode, ev.value
        self._hide_input()
        if mode.startswith("new:"):
            self.start_agent(mode.split(":", 1)[1], value)
            self.input_mode = "filter"; self.query_one("#filter", Input).value = ""


def run(cfg: Config, host: str | None = None) -> None:
    CtxApp(cfg, host).run()
