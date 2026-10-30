"""ctx — command line client.

  ctx ls [--host H | --all] [--live]     one line per session (default: this host)
  ctx show <key-or-prefix>               ledger view: meta, open asks, recent exchanges
  ctx send <key-or-prefix> [text | -] [--buffer]   message a session (queued until it runs); --buffer = tmux paste buffer
  ctx send --cancel <id>                 cancel a message that is still queued
  ctx reply <key-or-prefix> [text | -]   answer the session's open question (a message linked to it)
  ctx attention [--all]                  what needs the operator, oldest wait first
  ctx hosts                              collectors seen by the hub
  ctx search <words> [--kind K] [--host H]   decisions, instructions, claims and your prompts, every session
  ctx mcp                                an MCP server on stdio (read-only) for agents: sessions, ledgers, search
  ctx tui                                the board (narrow; built for a tmux side pane)
  ctx tmux [--width 18%|40]             create/attach the tmux manager: session left, ctx tui right
  ctx install / uninstall [--claude --pi --collector]   wire this host up (hooks, pi extension, collector unit)
  ctx doctor                             check this host's wiring, one line per check
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

from ctxswitch.common.schema import LIVE
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
        when = s.get("status_since") if s.get("live_status") in LIVE else s.get("last_activity")
        print(f"{fmt.state_glyph(s)} {fmt.short_key(s):<28} {fmt.age(when, now):>6} {fmt.one_line(fmt.repo_label(s), 24):<24} "
              f"{fmt.one_line(fmt.display_title(s), 50)}{wait}")
    return 0


def _resolve(c: HubClient, key: str) -> str | None:
    """A full key, or the one session whose key or title contains ``key``; prints the candidates otherwise."""
    found, matches = c.resolve(key)
    if found is None:
        print(f"{len(matches)} sessions match {key!r}:" if matches else f"no session matches {key!r}")
        for s in matches[:20]:
            print("  ", s["key"], "-", fmt.one_line(s.get("title"), 60))
    return found


def cmd_show(a) -> int:
    c = _client()
    key = _resolve(c, a.key)
    if key is None:
        return 1
    s = c.session(key, turns=a.turns)
    if not s:
        print("not found"); return 1
    now = datetime.now(timezone.utc)
    print("\n".join(fmt.show_lines(s, a.turns, a.decisions, now)))
    if not a.no_mark:
        c.viewed(key, now.isoformat().replace("+00:00", "Z"))
    return 0


def cmd_send(a) -> int:
    c = _client()
    if a.cancel is not None:
        ok = c.cancel(a.cancel)
        print(f"cancelled message {a.cancel}" if ok else f"message {a.cancel} is not queued (already sent, or no such id)")
        return 0 if ok else 1
    if not a.key:
        print("usage: ctx send <session> [text | -] [--buffer]"); return 2
    if a.buffer:
        import subprocess
        r = subprocess.run(["tmux", "show-buffer"], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"no tmux buffer: {r.stderr.strip() or 'tmux show-buffer failed'}"); return 1
        body = r.stdout
    else:
        body = _body(a)
    if not body.strip():
        print("empty message, nothing sent"); return 1
    key = _resolve(c, a.key)
    if key is None:
        return 1
    s = c.session(key, turns=0)
    if a.work:
        if not (s or {}).get("workstream"):
            print("that session isn't in a repo, so it has no workstream"); return 1
        try:
            mid = c.send_workstream(s["workstream"], body, load().host_id)
        except ValueError as e:
            print(f"not sent: {e}"); return 1
        print(f"message {mid} queued for {fmt.workstream_label(s['workstream'])}: the first session live on it (any host) takes it")
        return 0
    why = fmt.can_message(s or {})
    if s is None or why:
        print(why or "not found"); return 1
    try:
        mid = c.send(key, body, load().host_id)
    except ValueError as e:
        print(f"not sent: {e}"); return 1
    live = s.get("live_status") in LIVE
    print(f"message {mid} queued for {fmt.short_key(s)}: " + ("delivering now" if live else "delivered when it next runs"))
    return 0


def cmd_retire(a) -> int:
    c = _client()
    key = _resolve(c, a.key)
    if key is None:
        return 1
    ok = c.retire(key, a.id)
    print(f"retired {a.id}" if ok else f"no active row {a.id} in {key}")
    return 0 if ok else 1


def _body(a) -> str:
    if a.text in ([], ["-"]):
        if sys.stdin.isatty() and not a.text:
            print("type the message, then Ctrl-D:", file=sys.stderr)
        return sys.stdin.read()
    return " ".join(a.text)


def cmd_reply(a) -> int:
    c = _client()
    key = _resolve(c, a.key)
    if key is None:
        return 1
    s = c.session(key, turns=0)
    if s is None:
        print("not found"); return 1
    ask, why = fmt.reply_target(s)
    if ask is None:
        print(why); return 1
    print(f"answering [{ask['kind']}] {fmt.one_line(ask['text'], 100)}", file=sys.stderr)
    body = _body(a)
    if not body.strip():
        print("empty answer, nothing sent"); return 1
    try:
        mid = c.send(key, body, load().host_id, ask["ref"])
    except ValueError as e:
        print(f"not sent: {e}"); return 1
    print(f"answer {mid} queued for {fmt.short_key(s)}: " + ("delivering now" if s.get("live_status") in LIVE else "delivered when it next runs"))
    return 0


def cmd_attention(a) -> int:
    c = _client()
    items = c.attention(_host_arg(a))["items"]
    now = datetime.now(timezone.utc)
    if not items:
        print("nothing needs you"); return 0
    for i in items:
        label = f"[{i['kind']}] {fmt.one_line(i['text'], 80)}" if i["item"] == "ask" else i["item"]
        print(f"◐ {fmt.age(i.get('asked_at'), now):>6}  {i['host_id']}/{fmt.HARNESS.get(i['harness'], i['harness'])}  {fmt.one_line(i.get('title'), 36):<36}  {label}")
    return 0


def cmd_search(a) -> int:
    from ctxswitch.client.mcp_server import Server
    print(Server(_client()).search(" ".join(a.words), a.kind, a.host, a.limit))
    return 0


def cmd_mcp(a) -> int:
    from ctxswitch.client.mcp_server import main as mcp_main
    return mcp_main()


def cmd_hosts(a) -> int:
    for h in _client().hosts():
        print(f"{h['host_id']:<16} last seen {fmt.age(datetime.fromtimestamp(h['last_seen'], tz=timezone.utc).isoformat())} ago")
    return 0


def cmd_tmux(a) -> int:
    from ctxswitch.client import tmux
    print(tmux.bootstrap(a.width, a.restart))
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
    p = sub.add_parser("send"); p.set_defaults(fn=cmd_send)
    p.add_argument("key", nargs="?"); p.add_argument("text", nargs="*")
    p.add_argument("--buffer", action="store_true", help="send the tmux paste buffer (e.g. a copy-mode selection)")
    p.add_argument("--cancel", type=int, metavar="ID", help="cancel a queued message")
    p.add_argument("--work", action="store_true", help="to its workstream (repo + branch): whichever session is, or next is, live on it")
    p = sub.add_parser("retire", help="retire an instruction that no longer holds (ids in `ctx show`)"); p.set_defaults(fn=cmd_retire)
    p.add_argument("key"); p.add_argument("id", type=int)
    p = sub.add_parser("reply"); p.set_defaults(fn=cmd_reply)
    p.add_argument("key"); p.add_argument("text", nargs="*")
    sub.add_parser("hosts").set_defaults(fn=cmd_hosts)
    p = sub.add_parser("search"); p.set_defaults(fn=cmd_search)
    p.add_argument("words", nargs="+"); p.add_argument("--kind", help="decision,instruction,claim,prompt (default all)")
    p.add_argument("--host"); p.add_argument("--limit", type=int, default=30)
    sub.add_parser("mcp").set_defaults(fn=cmd_mcp)
    p = sub.add_parser("tmux"); p.set_defaults(fn=cmd_tmux); p.add_argument("--width", default="18%", help="TUI pane width: columns or percent (default 18%%)")
    p.add_argument("--restart", action="store_true", help="restart the board (e.g. after an update); agents keep running")
    p = sub.add_parser("tmux-exit", help="(internal) run by agent windows when the agent exits"); p.set_defaults(fn=cmd_tmux_exit)
    p.add_argument("--rc", type=int, default=0); p.add_argument("--started", type=float)
    from ctxswitch.client.install import add_parsers
    add_parsers(sub)
    p = sub.add_parser("hub"); p.set_defaults(fn=lambda a: __import__("ctxswitch.hub.main", fromlist=["main"]).main())
    p = sub.add_parser("collector"); p.set_defaults(fn=lambda a: __import__("ctxswitch.collector.main", fromlist=["main"]).main())
    a, rest = ap.parse_known_args(argv)
    if a.cmd in ("hub", "collector"):
        sys.argv = [f"ctx-{a.cmd}", *rest]
    try:
        sys.exit(a.fn(a) or 0)
    except ConnectionError as e:
        print(f"hub unreachable: {e}"); sys.exit(2)
