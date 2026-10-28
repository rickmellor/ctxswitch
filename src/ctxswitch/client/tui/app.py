"""ctx tui — the operator board.

  ┌ attention: what needs you, oldest wait first ─────────────────────────────────┐
  ├ sessions (host filter: this | all | <host>) ───────┬ ledger of the selected session ┤
  │ ◐ laptop/cc/e7a45b61  3m  ctxswitch@main  title…  │ state / last exchange / open asks / decisions / exchanges │
  └────────────────────────────────────────────────────┴───────────────────────────────┘
Keys: h host filter · e toggle ended · v mark viewed · enter resume · r refresh · / filter · q quit
Live: WebSocket change notices from the hub; falls back to polling every 10 s.
"""
from __future__ import annotations

import asyncio
import json
import socket
from datetime import datetime, timezone

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Input, Static

from ctxswitch.client import fmt
from ctxswitch.client.hubclient import HubClient
from ctxswitch.client.launch import open_tab
from ctxswitch.common.config import Config

STATE_STYLE = {"busy": "bold green", "waiting": "bold yellow", "idle": "cyan", "unknown": "dim", "ended": "dim"}


class CtxApp(App):
    CSS = """
    #attention { height: auto; max-height: 8; border: round $warning; padding: 0 1; }
    #attention.empty { border: round $surface; color: $text-muted; }
    #body { height: 1fr; }
    #sessions { width: 58%; }
    #detail { width: 42%; border-left: solid $surface; padding: 0 1; }
    #filter { dock: bottom; display: none; }
    #filter.visible { display: block; }
    .h { color: $text-muted; }
    """
    BINDINGS = [
        Binding("h", "cycle_host", "host"), Binding("e", "toggle_ended", "ended"), Binding("v", "mark_viewed", "viewed"),
        Binding("enter", "resume", "resume", priority=True), Binding("r", "refresh", "refresh"),
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

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="attention")
        with Horizontal(id="body"):
            with Vertical(id="sessions"):
                yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            yield VerticalScroll(Static("", id="detail_text"), id="detail")
        inp = Input(placeholder="filter sessions (title, repo, key)…", id="filter")
        inp.can_focus = False                     # only focusable while shown via "/"
        yield inp
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#table", DataTable)
        t.add_columns(" ", "session", "age", "repo@branch", "title / last prompt", "waiting")
        self.title = "ctxswitch"
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
        self.call_from_thread(self.apply_data, data["sessions"], att, hosts, data["change"])

    def sub_title_set(self, s: str) -> None:
        self.sub_title = s

    def apply_data(self, sessions: list[dict], attention: list[dict], hosts: list[str], change: int) -> None:
        self.change = change
        self.hosts = hosts
        self.sessions = sessions
        self.sub_title = f"host: {self.host_filter or 'all'} · {len(sessions)} sessions · change {change}"
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
        t = Text()
        for i in items[:6]:
            label = f"[{i['kind']}] {fmt.one_line(i['text'], 70)}" if i["item"] == "ask" else "idle — finished its turn"
            t.append(f"◐ {fmt.age(i.get('asked_at'), now):>5} ", style="bold yellow")
            t.append(f"{i['host_id']}/{fmt.HARNESS.get(i['harness'], i['harness'])} ", style="dim")
            t.append(f"{fmt.one_line(i.get('title'), 30):<30} ", style="bold")
            t.append(label + "\n")
        if len(items) > 6:
            t.append(f"… {len(items) - 6} more", style="dim")
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

    def render_table(self) -> None:
        t = self.query_one("#table", DataTable)
        now = datetime.now(timezone.utc)
        prev = self.selected
        t.clear()
        for s in self._visible():
            st = fmt.session_state(s)
            last = s.get("last_turn") or {}
            asks = s.get("open_asks") or []
            when = s.get("status_since") if s.get("live_status") in ("busy", "idle") else s.get("last_activity")
            style = STATE_STYLE.get(st, "")
            t.add_row(Text(fmt.state_glyph(s), style=style), Text(fmt.short_key(s), style=style), fmt.age(when, now),
                      fmt.one_line(fmt.repo_label(s), 22), fmt.one_line(s.get("title") or last.get("user_text"), 48),
                      fmt.age(asks[0].get("asked_at"), now) if asks else "", key=s["key"])
        if prev and prev in [s["key"] for s in self._visible()]:
            try:
                t.move_cursor(row=t.get_row_index(prev))
            except Exception:                           # noqa: BLE001
                pass
        elif t.row_count:
            self.selected = str(t.get_row_at(0)[1]) and self._visible()[0]["key"]

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
        t.append(f"{fmt.state_glyph(s)} {s['key']}\n", style="bold")
        t.append(f"{st} since {fmt.age(s.get('status_since') or s.get('last_activity'), now)}", style=STATE_STYLE.get(st, ""))
        t.append(f" · {s.get('turn_count')} exchanges · started {fmt.age(s.get('started_at'), now)} ago", style="dim")
        if s.get("last_viewed_at"):
            t.append(f" · viewed {fmt.age(s['last_viewed_at'], now)} ago", style="dim")
        t.append("\n")
        t.append(f"{s.get('title') or '-'}\n", style="bold")
        t.append(f"{s.get('cwd') or '-'}  {fmt.repo_label(s)}  {s['meta'].get('model') or ''}  {s['meta'].get('permission_mode') or ''}\n\n", style="dim")
        opens = [x for x in s.get("asks", []) if not x["answered"]]
        if opens:
            t.append("WAITING ON YOU\n", style="bold yellow")
            for x in opens:
                t.append(f"  ◐ [{x['kind']}] {x['text']}\n", style="yellow")
                t.append(f"     waiting {fmt.age(x.get('asked_at'), now)}\n", style="dim")
            t.append("\n")
        turns = s.get("turns", [])
        if turns:
            last = turns[-1]
            t.append("LAST EXCHANGE\n", style="bold")
            t.append(f"  you  : {fmt.one_line(last.get('user_text'), 400)}\n")
            if last.get("assistant_text"):
                t.append(f"  agent: {fmt.one_line(last.get('assistant_text'), 600)}\n")
            tally = {k: v for k, v in (last.get("tally") or {}).items() if v}
            if tally or last.get("events"):
                t.append("  " + ", ".join(f"{v} {k}" for k, v in tally.items()) + ("  " + " ".join(last.get("events", [])) if last.get("events") else "") + "\n", style="dim")
            if not last.get("complete"):
                t.append("  (in progress)\n", style="green")
            t.append("\n")
        answered = [x for x in s.get("asks", []) if x["answered"]]
        if answered:
            t.append("DECISIONS\n", style="bold")
            for x in answered[-6:]:
                t.append(f"  ✓ {fmt.one_line(x['text'], 60)}\n", style="dim")
                t.append(f"    → {fmt.one_line(x.get('answer'), 90)}\n")
            t.append("\n")
        if len(turns) > 1:
            t.append("EARLIER\n", style="bold")
            for tr in turns[-7:-1][::-1]:
                t.append(f"  #{tr['idx']} {fmt.age(tr.get('started_at'), now):>5} ", style="dim")
                t.append(f"{fmt.one_line(tr.get('user_text'), 90)}\n")
                if tr.get("assistant_text"):
                    t.append(f"        ↳ {fmt.one_line(tr.get('assistant_text'), 90)}\n", style="dim")
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
        if s:
            self.notify(open_tab(s, self.this_host), timeout=8)

    def action_filter(self) -> None:
        f = self.query_one("#filter", Input); f.add_class("visible"); f.can_focus = True; f.focus()

    def action_clear_filter(self) -> None:
        f = self.query_one("#filter", Input); f.value = ""; f.remove_class("visible"); f.can_focus = False
        self.text_filter = ""; self.render_table(); self.query_one("#table").focus()

    def on_input_changed(self, ev: Input.Changed) -> None:
        self.text_filter = ev.value; self.render_table()

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        f = self.query_one("#filter", Input); f.remove_class("visible"); f.can_focus = False
        self.query_one("#table").focus()


def run(cfg: Config, host: str | None = None) -> None:
    CtxApp(cfg, host).run()
