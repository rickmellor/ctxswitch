"""ctx — command line client.

  ctx ls [--host H | --all] [--live]     one line per session (default: this host)
  ctx show <key-or-prefix>               ledger view: meta, open asks, recent exchanges
  ctx attention [--all]                  what needs the operator, oldest wait first
  ctx hosts                              collectors seen by the hub
  ctx tui                                the board (narrow; built for a tmux side pane)
  ctx tmux [--width 25%|56]             create/attach the tmux manager: session left, ctx tui right
  ctx hub / ctx collector                run the services (same as ctx-hub / ctx-collector)
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

from ctxswitch.client import fmt
from ctxswitch.client.hubclient import HubClient
from ctxswitch.common.config import load


def _client() -> HubClient:
    return HubClient(load())


def _host_arg(a) -> str | None:
    if getattr(a, "all", False):
        return None
    return a.host or load().host_id or socket.gethostname()


def cmd_ls(a) -> int:
    c = _client()
    data = c.sessions(_host_arg(a), live_only=a.live)
    now = datetime.now(timezone.utc)
    rows = data["sessions"]
    if not rows:
        print("no sessions"); return 0
    for s in rows:
        last = s.get("last_turn") or {}
        asks = s.get("open_asks") or []
        wait = f" waiting {fmt.age(asks[0].get('asked_at'), now)}" if asks else ""
        when = s.get("status_since") if s.get("live_status") in ("busy", "idle", "shell") else s.get("last_activity")
        print(f"{fmt.state_glyph(s)} {fmt.short_key(s):<28} {fmt.age(when, now):>6} {fmt.one_line(fmt.repo_label(s), 24):<24} "
              f"{fmt.one_line(fmt.display_title(s), 50)}{wait}")
    return 0


def cmd_show(a) -> int:
    c = _client()
    key = a.key
    if key.count("/") < 2:
        matches = [s for s in c.sessions(None)["sessions"] if key in s["key"] or key in (s.get("title") or "")]
        if len(matches) != 1:
            print(f"{len(matches)} sessions match {key!r}:" if matches else f"no session matches {key!r}")
            for s in matches[:20]:
                print("  ", s["key"], "-", fmt.one_line(s.get("title"), 60))
            return 1
        key = matches[0]["key"]
    s = c.session(key, turns=a.turns)
    if not s:
        print("not found"); return 1
    now = datetime.now(timezone.utc)
    print(f"{fmt.state_glyph(s)} {s['key']}   [{fmt.session_state(s)} since {fmt.age(s.get('status_since') or s.get('last_activity'), now)}]")
    print(f"  title : {s.get('title') or '-'}")
    print(f"  where : {s.get('cwd') or '-'}  ({fmt.repo_label(s)})   model: {s['meta'].get('model') or '-'}  perm: {s['meta'].get('permission_mode') or '-'}")
    print(f"  turns : {s.get('turn_count')}   started {fmt.age(s.get('started_at'), now)} ago, last activity {fmt.age(s.get('last_activity'), now)} ago"
          + (f", viewed {fmt.age(s.get('last_viewed_at'), now)} ago" if s.get('last_viewed_at') else ""))
    opens = [x for x in s.get("asks", []) if not x["answered"]]
    if opens:
        print("  OPEN ASKS:")
        for x in opens:
            print(f"    ◐ [{x['kind']}] {fmt.one_line(x['text'], 100)}   (waiting {fmt.age(x.get('asked_at'), now)})")
    answered = [x for x in s.get("asks", []) if x["answered"]][-a.decisions:]
    if answered:
        print("  decisions:")
        for x in answered:
            print(f"    ✓ {fmt.one_line(x['text'], 70)}  →  {fmt.one_line(x.get('answer'), 60)}")
    print("  exchanges:")
    for t in s.get("turns", [])[-a.turns:]:
        flag = "" if t.get("complete") else " (in progress)"
        print(f"    #{t['idx']} {fmt.age(t.get('started_at'), now):>5} ago [{t.get('user_origin')}]{flag}")
        print(f"       you  : {fmt.one_line(t.get('user_text'), 110)}")
        if t.get("assistant_text"):
            print(f"       agent: {fmt.one_line(t.get('assistant_text'), 110)}")
        tally = t.get("tally") or {}
        ev = t.get("events") or []
        extras = ", ".join(f"{v} {k}" for k, v in tally.items() if v) + ("; " + ", ".join(ev) if ev else "")
        if extras:
            print(f"       tools: {extras}")
    if not a.no_mark:
        c.viewed(key, now.isoformat().replace("+00:00", "Z"))
    return 0


def cmd_attention(a) -> int:
    c = _client()
    items = c.attention(_host_arg(a))["items"]
    now = datetime.now(timezone.utc)
    if not items:
        print("nothing needs you"); return 0
    for i in items:
        label = f"[{i['kind']}] {fmt.one_line(i['text'], 80)}" if i["item"] == "ask" else "idle"
        print(f"◐ {fmt.age(i.get('asked_at'), now):>6}  {i['host_id']}/{fmt.HARNESS.get(i['harness'], i['harness'])}  {fmt.one_line(i.get('title'), 36):<36}  {label}")
    return 0


def cmd_hosts(a) -> int:
    for h in _client().hosts():
        print(f"{h['host_id']:<16} last seen {fmt.age(datetime.fromtimestamp(h['last_seen'], tz=timezone.utc).isoformat())} ago")
    return 0


def cmd_tmux(a) -> int:
    from ctxswitch.client import tmux
    print(tmux.bootstrap(a.width))
    return 0


def cmd_tmux_exit(a) -> int:
    from ctxswitch.client import tmux
    try:
        tmux.on_agent_exit(a.rc, a.started)
        return 0
    except Exception:                                   # noqa: BLE001 - the wrapper falls back to a shell; leave a trace
        import traceback
        log = Path.home() / ".local/state/ctxswitch/tmux-exit.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as f:
            f.write(f"--- {datetime.now(timezone.utc).isoformat()} pane={os.environ.get('TMUX_PANE')}\n{traceback.format_exc()}")
        print(f"ctx tmux-exit failed (see {log}); dropping to a shell")
        return 1


def cmd_tui(a) -> int:
    from ctxswitch.client.tui.app import run
    run(load(), host=_host_arg(a))
    return 0


def main(argv=None) -> None:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)      # `ctx ls | head` must not traceback
    ap = argparse.ArgumentParser(prog="ctx", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("ls", cmd_ls), ("attention", cmd_attention), ("tui", cmd_tui)):
        p = sub.add_parser(name); p.set_defaults(fn=fn)
        p.add_argument("--host"); p.add_argument("--all", action="store_true")
        if name == "ls": p.add_argument("--live", action="store_true")
    p = sub.add_parser("show"); p.set_defaults(fn=cmd_show)
    p.add_argument("key"); p.add_argument("--turns", type=int, default=6); p.add_argument("--decisions", type=int, default=8)
    p.add_argument("--no-mark", action="store_true", help="don't record this as a view")
    sub.add_parser("hosts").set_defaults(fn=cmd_hosts)
    p = sub.add_parser("tmux"); p.set_defaults(fn=cmd_tmux); p.add_argument("--width", default="25%", help="TUI pane width: columns or percent (default 25%%)")
    p = sub.add_parser("tmux-exit", help="(internal) run by agent windows when the agent exits"); p.set_defaults(fn=cmd_tmux_exit)
    p.add_argument("--rc", type=int, default=0); p.add_argument("--started", type=float)
    p = sub.add_parser("hub"); p.set_defaults(fn=lambda a: __import__("ctxswitch.hub.main", fromlist=["main"]).main())
    p = sub.add_parser("collector"); p.set_defaults(fn=lambda a: __import__("ctxswitch.collector.main", fromlist=["main"]).main())
    a, rest = ap.parse_known_args(argv)
    if a.cmd in ("hub", "collector"):
        sys.argv = [f"ctx-{a.cmd}", *rest]
    try:
        sys.exit(a.fn(a) or 0)
    except ConnectionError as e:
        print(f"hub unreachable: {e}"); sys.exit(2)
