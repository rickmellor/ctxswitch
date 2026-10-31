"""Regenerate the README images (docs/img/*.svg) from the real board, with made-up sessions: no real host,
session or transcript goes into them.

    .venv/bin/python scripts/screenshots.py

board.svg   ctx in tmux: an agent on the left, the board on the right, tmux's status line
legend.svg  every glyph on the board, in its colours (the legend part of `?`)
help.svg    the `?` help screen as it opens
"""
from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Group
from rich.table import Table
from rich.text import Text
from starlette.testclient import TestClient
from textual.app import App
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from ctxswitch.client.tui import app as tui
from ctxswitch.common.config import Config
from ctxswitch.hub.api import Hub
from ctxswitch.hub.store import Store
from ctxswitch.hub.tracker.recap import anchor_of

OUT = Path(__file__).resolve().parent.parent / "docs" / "img"
NOW = datetime.now(timezone.utc)
WEB, API, DOCS, INFRA, UI, BENCH, OLD = ("laptop/claude/7f3a91c2", "devbox/claude/2b8e40d1", "laptop/pi/01a2b3c4",
                                         "gpu-host/claude/9c1d22e7", "laptop/claude/4e5f6a7b", "devbox/pi/01d4e5f6",
                                         "laptop/claude/0a0b0c0d")
JOB = "laptop/claude/5d6e7f80"                 # a pool job WEB launched with saint-agent


def iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def turn(idx, user, agent, ago, asks=(), complete=True):
    return {"idx": idx, "started_at": iso(ago + 1), "ended_at": iso(ago), "user_text": user, "user_origin": "human",
            "assistant_text": agent, "tally": {"edits": 3, "bash": 2, "reads": 5}, "asks": list(asks), "events": [],
            "complete": complete, "offset_end": idx * 100 + 50}


def seed(store: Store) -> None:
    rows = [  # key, title, repo, branch, live status, minutes in that state, turns
        (WEB, "OAuth login", "webapp", "main", "busy", 4, [turn(0, "add GitHub OAuth login", "Plan: …", 40),
                                                          turn(1, "use the existing session store", "Wiring the callback route…", 4, complete=False)]),
        (API, "rate limiter", "api", "feat/rate-limit", "waiting", 3, [
            turn(0, "add per-key rate limiting to the public API", "Done the middleware.", 30),
            turn(1, "don't touch the billing endpoints", "Understood. Token bucket or sliding window?", 3,
                 asks=[{"ref": "1:plain", "kind": "plain", "text": "Token bucket or sliding window?", "answered": False}])]),
        (DOCS, "release notes", "docs", "main", "idle", 12, [turn(0, "draft the 2.4 release notes", "Drafted in NOTES.md.", 12)]),
        (INFRA, "GPU node upgrade", "infra", "main", "busy", 1, [turn(0, "upgrade the driver on the GPU nodes", "Draining node 2…", 1, complete=False)]),
        (UI, "settings page", "webapp", "feat/settings", "waiting", 1, [turn(0, "run the e2e suite", "", 1, complete=False)]),
        (BENCH, "load test", "bench", "main", "idle", 55, [turn(0, "run the 10k rps profile", "p99 41 ms; report in out/.", 55)]),
        (OLD, "CI cache", "webapp", "main", "ended", 300, [turn(0, "speed up CI", "Cached deps: 6m → 2m.", 300)]),
    ]
    for key, title, repo, branch, status, ago, turns in rows:
        host, harness, sid = key.split("/")
        store.upsert_session({"key": key, "host_id": host, "harness": harness, "session_id": sid, "title": title,
                              "cwd": f"/home/me/{repo}", "repo_root": f"/home/me/{repo}", "repo_remote": f"git@github.com:acme/{repo}.git",
                              "git_branch": branch, "last_activity": iso(ago), "started_at": iso(ago + 60)})
        store.upsert_turns(key, turns, None)
    # a pool job OAuth login launched with saint-agent: it nests under that row, with its own cost estimate
    store.upsert_session({"key": JOB, "host_id": "laptop", "harness": "claude", "session_id": JOB.rsplit("/", 1)[-1],
                          "title": "oauth-tests.md", "cwd": "/home/me/webapp", "repo_root": "/home/me/webapp",
                          "repo_remote": "git@github.com:acme/webapp.git", "git_branch": "main",
                          "last_activity": iso(6), "started_at": iso(20),
                          "parent_key": WEB, "job": "sa-20261030-1412-j1", "job_status": "ok",
                          "job_brief": "oauth-tests.md", "job_turns": 72,
                          "job_tokens": {"input": 21_000, "output": 6_400, "cache_read": 1_900_000, "cache_creation": 48_000}})
    store.upsert_turns(JOB, [turn(0, "brief: oauth-tests.md", "Both flows pass; committed.", 6)], None)
    for host in ("laptop", "devbox", "gpu-host"):
        store.host_seen(host)
        store.set_liveness(host, [{"key": k, "status": st, "status_since": iso(ago), "pid": 1, "socket": "/x"}
                                  for k, _, _, _, st, ago, _ in rows if k.startswith(host + "/") and st != "ended"]
                                 + ([{"key": JOB, "status": "idle", "status_since": iso(6), "pid": 1, "socket": "/x"}]
                                    if host == "laptop" else []))
    store.db.execute("INSERT INTO asks(key,ref,kind,text,turn_idx,asked_at) VALUES(?,?,?,?,?,?)",
                     (UI, "hook:perm:Bash:1", "permission", "allow Bash: npm run e2e", 0, iso(1)))
    store.apply_extraction(API, turn(1, "x", "", 3), [
        {"kind": "instruction", "text": "Don't touch the billing endpoints", "quote": "don't touch the billing endpoints", "extra": {}},
        {"kind": "claim", "text": "Middleware done, tests pass", "quote": "Done the middleware.", "extra": {"claim": "tests_pass"}}],
        [], [], "Per-key rate limiting for the public API")
    store.mark_viewed(API, iso(20))
    for key in (API, WEB, DOCS, INFRA, BENCH):
        s = store.session(key, turns=0)
        recap = {API: "Middleware in; waiting on you: token bucket or sliding window.",
                 WEB: "Wiring the OAuth callback onto the existing session store.",
                 DOCS: "2.4 notes drafted in NOTES.md; your review next.",
                 INFRA: "Draining GPU node 2 for the driver upgrade.",
                 BENCH: "10k rps profile done: p99 41 ms."}[key]
        store.set_recap(key, recap, anchor_of(s), "Rate limiting the public API.\nYou said: billing endpoints are off limits." if key == API else None)
    store.add_message(API, "keep the limits per API key, not per IP", "laptop")


AGENT = Text.assemble(
    ("✻ Claude Code", "bold #d97757"), ("  ~/webapp\n\n", "dim"),
    ("> ", "bold"), ("add GitHub OAuth login, use the existing session store\n\n", ""),
    ("● ", "#d97757"), ("I'll add the OAuth flow in three steps:\n", ""),
    ("  1. register the provider in ", ""), ("auth/providers.ts\n", "cyan"),
    ("  2. add the ", ""), ("/auth/github/callback", "cyan"), (" route\n", ""),
    ("  3. reuse ", ""), ("SessionStore", "cyan"), (" for the login session\n\n", ""),
    ("● ", "#d97757"), ("Update", "bold"), ("(src/routes/auth.ts)\n", ""),
    ("  └ Updated src/routes/auth.ts with 24 additions\n", "dim"),
    ("● ", "#d97757"), ("Bash", "bold"), ("(npm test -- auth)\n", ""),
    ("  └ ", "dim"), ("✓ 18 passed", "green"), (" (1.2s)\n\n", "dim"),
    ("✻ Wiring the callback route… ", "#d97757"), ("(esc to interrupt)\n", "dim"),
)
STATUS = Text.assemble(("[ctx] ", "bold black on #5fd75f"), ("0:mgr* ", "black on #5fd75f"), ("1:webapp  2:api  3:docs  4:infra  5:settings ", "black on #5fd75f"))


class Demo(tui.CtxApp):
    """The board as ctx tmux lays it out: an agent pane on the left, the board in a 60-column pane on the right,
    tmux's status line along the bottom."""
    CSS = tui.CtxApp.CSS + """
    #frame { height: 1fr; }
    #agent { width: 1fr; padding: 0 1; }
    #boardcol { width: 61; border-left: vkey #5fd75f; }
    #tmux { height: 1; background: #5fd75f; color: black; }
    """

    def _label_width(self) -> int:
        return 60 - 17                                  # the board's pane, not this whole demo screen

    def compose(self):
        with Horizontal(id="frame"):
            yield Static(AGENT, id="agent")
            with Vertical(id="boardcol"):
                yield from super().compose()
        yield Static(STATUS, id="tmux")


def patch_tmux() -> None:
    """Pretend we're in ctx's tmux: OAuth login is on the left, the rest are in their own windows, one runs on
    another host and one in another terminal."""
    reach = {WEB: "pane", API: "pane", DOCS: "pane", INFRA: "remote", UI: "pane", BENCH: "foreign", OLD: "resume",
             JOB: "pane"}
    tui.tmux.reap_viewer = lambda *a, **k: None
    tui.tmux.panes = list
    tui.tmux.visible_key = lambda ps, sessions=None: WEB
    tui.tmux.on_screen_keys = lambda ps, sessions=None: {WEB}
    tui.tmux.reach_all = lambda sessions, host, ssh=None: reach
    tui.CtxApp.listen_ws = lambda self: None
    tui.CtxApp.join_tmux = lambda self: None


def patch_spend(app: tui.CtxApp) -> None:
    """Today's costs as /v1/spend would answer them: this branch's hub has no such route, and the image must show
    the header's second line as it looks with one."""
    app.client.spend = lambda frm, to, host=None: {
        "window": {"from": frm, "to": to},
        "cloud": {"usd": 3.64, "tokens": {"input": 410_000, "output": 96_000, "cache_read": 7_100_000, "cache_creation": 380_000},
                  "by_account": [{"account": "bedrock", "usd": 3.11, "tokens": 3_800_000, "sessions": 6},
                                 {"account": "max", "usd": 0.53, "tokens": 4_192_000, "sessions": 2}],
                  "by_model": [{"model": "claude-opus-5-5", "usd": 3.64, "tokens": 7_986_000, "priced": True}]},
        "local": {"energy_usd": 0.48, "if_cloud_usd": 2.9, "tokens": 17_300_000, "requests": 342,
                  "price_as": "claude-opus-5-5", "source": "saint", "error": None},
        "jobs": [{"job": "sa-20261030-1412-j1", "key": JOB, "parent_key": WEB, "parent_title": "OAuth login",
                  "status": "ok", "brief": "oauth-tests.md", "started_at": iso(20), "dur_s": 640, "turns": 72,
                  "tokens": 1_975_400, "local_usd": 0.03, "if_cloud_usd": 0.91}],
        "sessions": [{"key": WEB, "title": "OAuth login", "host_id": "laptop", "harness": "claude",
                      "account": "bedrock", "usd": 1.23, "tokens": 2_400_000, "model": "claude-opus-5-5"}],
        "total_usd": 4.12, "unpriced_models": [], "rates": "default"}


async def shoot(store: Store) -> None:
    cfg = Config(); cfg.hub.token = "t"; cfg.collector.host_id = "laptop"
    app = Demo(cfg, None)
    app.client._c = TestClient(Hub(store, token="t").app(), headers={"Authorization": "Bearer t"})
    patch_spend(app)
    async with app.run_test(size=(150, 52)) as pilot:
        await pilot.pause(1.5)
        t = app.query_one("#table")
        t.move_cursor(row=t.get_row_index(API)); await pilot.pause(1.0)
        (OUT / "board.svg").write_text(app.export_screenshot(title="ctx tmux"))
    help_app = Demo(cfg, None)
    help_app.client._c = app.client._c
    patch_spend(help_app)
    help_app.CSS = tui.CtxApp.CSS
    help_app.compose = lambda: tui.CtxApp.compose(help_app)
    async with help_app.run_test(size=(62, 46)) as pilot:
        await pilot.pause(1.0); await pilot.press("question_mark"); await pilot.pause(0.5)
        (OUT / "help.svg").write_text(help_app.export_screenshot(title="ctx · ? help"))


class LegendApp(App):
    """The glyph legend in the board's own colours (Textual's theme, not Rich's terminal palette)."""
    CSS = "Screen { padding: 0 1; }"

    def compose(self):
        parts: list = []
        for title, rows in tui.legend_sections():
            parts.append(Text(title, style="bold"))
            g = Table.grid(padding=(0, 2), pad_edge=True)
            g.add_column(width=12, no_wrap=True); g.add_column(ratio=1)
            for k, style, words in rows:
                g.add_row(Text(k, style=style), words)
            parts += [g, Text("")]
        yield Static(Group(*parts))


async def legend_svg() -> None:
    app = LegendApp()
    async with app.run_test(size=(80, 50)) as pilot:
        await pilot.pause(0.3)
        (OUT / "legend.svg").write_text(app.export_screenshot(title="ctx · the board's glyphs"))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "hub.db")
        seed(store)
        patch_tmux()
        asyncio.run(shoot(store))
    asyncio.run(legend_svg())
    for p in sorted(OUT.glob("*.svg")):
        print(p.relative_to(OUT.parent.parent), p.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main()
