"""ctx — command line client.

  ctx ls [--host H | --all] [--live]     one line per session (default: this host)
  ctx show <key-or-prefix>               ledger view: meta, open asks, recent exchanges
  ctx send <key-or-prefix> [text | -] [--buffer]   message a session (queued until it runs); --buffer = tmux paste buffer
  ctx send --cancel <id>                 cancel a message that is still queued
  ctx reply <key-or-prefix> [text | -]   answer the session's open question (a message linked to it)
  ctx attention [--all]                  what needs the operator, oldest wait first
  ctx spend [--days N] [--host H] [--json]   what things cost: cloud by account and model, local energy, pool jobs
  ctx hosts                              collectors seen by the hub
  ctx search <words> [--kind K] [--host H]   decisions, instructions, claims and your prompts, every session
  ctx mcp                                an MCP server on stdio (read-only) for agents: sessions, ledgers, search
  ctx tui                                the board (narrow; built for a tmux side pane)
  ctx tmux [--width 18%|40]             create/attach the tmux manager: session left, ctx tui right
  ctx shell [--id ID]                   a bash whose commands show on the board (started by n → shell)
  ctx install / uninstall [--claude --pi --collector]   wire this host up (hooks, pi extension, collector unit)
  ctx doctor                             check this host's wiring, one line per check
  ctx hub / ctx collector                run the services (same as ctx-hub / ctx-collector)
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
from datetime import datetime, timedelta, timezone
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
    print("\n".join(fmt.show_lines(s, a.turns, a.decisions, now, children=_children(c, s))))
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


def cmd_move(a) -> int:
    """Move a Claude session to another profile (account) and resume it: in tmux's viewer pane, else print how."""
    from ctxswitch.client import move as mv, tmux
    c = _client()
    key = _resolve(c, a.key)
    if key is None:
        return 1
    cfg = load()
    s = c.session(key, turns=0)
    if not s:
        print(f"no session {key}"); return 1
    to, _, prof = a.to.partition(":")
    harness = to if to in ("claude", "pi", "agy") else None
    profile = prof or None
    if harness is None:                                     # a claude profile name: a move between accounts
        profile = to
    elif harness == "claude" and profile is None and cfg.claude_profiles:
        profile = cfg.claude_profiles[0].name if len(cfg.claude_profiles) > 1 or cfg.claude_profiles[0].name != "default" else None
    try:
        if a.end and mv.can_end_here(s):
            print("ending it first…")
            why = mv.end_and_wait(s, cfg)
            if why:
                raise mv.CannotMove(why)
        if harness and not (harness == "claude" and s.get("harness") == "claude"):
            if s.get("live_status") in LIVE:
                raise mv.CannotMove("it's running: end it first" + ("" if a.end else " (--end, inside ctx's tmux)"))
            if tmux.in_tmux():
                start = lambda h, cwd, extra, p: tmux.new_agent(h, cwd, extra, profile=p, profiles=cfg.claude_profiles)
            else:
                start = lambda h, cwd, extra, p: "run: cd " + cwd + " && " + __import__("shlex").join(
                    [*(mv.profile_named(p, cfg.claude_profiles).argv() if h == "claude" else [h]), *extra])
            print(mv.handoff(s, harness, profile, cfg, c, start, full=not a.brief)); return 0
        target, path = mv.move(s, profile, cfg.claude_profiles, cfg.host_id)
    except mv.CannotMove as e:
        print(f"cannot move: {e}" + ("" if a.end or "running" not in str(e) else " (or --end, inside ctx's tmux)")); return 1
    print(f"moved to {target.name}: {path}")
    if tmux.in_tmux() and not a.no_resume:
        print(tmux.show(s, ssh=cfg.ssh, extra=mv.resume_extra(target)))
    else:
        print("run: " + mv.command_hint(s, target, cfg.claude_profiles))
    return 0


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


def _children(c: HubClient, s: dict) -> list[dict]:
    """The pool jobs a session launched, by when they started: an older hub (no children_keys) has none."""
    out = []
    for k in s.get("children_keys") or []:
        try:
            x = c.session(k, turns=0)
        except Exception:                               # noqa: BLE001 - show the session either way
            x = None
        if x:
            out.append(x)
    return sorted(out, key=lambda x: x.get("started_at") or "")


def cmd_search(a) -> int:
    from ctxswitch.client.mcp_server import Server
    print(Server(_client()).search(" ".join(a.words), a.kind, a.host, a.limit))
    return 0


def cmd_mcp(a) -> int:
    from ctxswitch.client.mcp_server import main as mcp_main
    return mcp_main()


def cmd_spend(a) -> int:
    """What things cost: today, 7 days and 30 days, or one window of ``--days N``. The same as the board's $ screen."""
    c = _client()
    now = datetime.now(timezone.utc)
    host = a.host or None
    one = lambda frm: _one(c, frm, now, host)
    if a.days is not None:
        rep = one(now - timedelta(days=a.days))
        if rep is None:
            return 2
        print(json.dumps(rep, indent=1) if a.json else "\n".join(fmt.spend_lines({f"{a.days} days": rep})))
        return 0
    midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    reports = {"Today": one(midnight), "7 days": one(now - timedelta(days=7)), "30 days": one(now - timedelta(days=30))}
    if all(r is None for r in reports.values()):
        return 2
    print(json.dumps(reports, indent=1) if a.json else "\n".join(fmt.spend_lines(reports)))
    return 0


def _one(c: HubClient, frm: datetime, to: datetime, host: str | None) -> dict | None:
    """One window of /v1/spend, or None with the reason printed (an older hub has no such route)."""
    try:
        return c.spend(frm.isoformat(), to.isoformat(), host)
    except ValueError as e:
        print(f"no spend report: {e}"); return None


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


def cmd_shell(a) -> int:
    from ctxswitch.shell import run
    run(a.id)                                             # never returns


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
    p = sub.add_parser("move", help="send a session elsewhere: to another Claude account (a move) or another agent (a handoff)"); p.set_defaults(fn=cmd_move)
    p.add_argument("key"); p.add_argument("--to", required=True, metavar="PROFILE|pi|agy|claude[:PROFILE]")
    p.add_argument("--brief", action="store_true", help="handoff: the last few exchanges only, not the whole conversation")
    p.add_argument("--no-resume", action="store_true", help="move only; print the resume command")
    p.add_argument("--end", action="store_true", help="a running session in ctx's tmux: end it first (as k does), then move")
    p = sub.add_parser("reply"); p.set_defaults(fn=cmd_reply)
    p.add_argument("key"); p.add_argument("text", nargs="*")
    sub.add_parser("hosts").set_defaults(fn=cmd_hosts)
    p = sub.add_parser("search"); p.set_defaults(fn=cmd_search)
    p.add_argument("words", nargs="+"); p.add_argument("--kind", help="decision,instruction,claim,prompt (default all)")
    p.add_argument("--host"); p.add_argument("--limit", type=int, default=30)
    p = sub.add_parser("spend", help="what things cost: today, 7 and 30 days (the board's $ screen)"); p.set_defaults(fn=cmd_spend)
    p.add_argument("--days", type=int, help="one window of N days instead of the three")
    p.add_argument("--host", help="one host's sessions and pool jobs (default: every host)")
    p.add_argument("--json", action="store_true", help="the report(s) as JSON")
    sub.add_parser("mcp").set_defaults(fn=cmd_mcp)
    p = sub.add_parser("tmux"); p.set_defaults(fn=cmd_tmux)
    p.add_argument("--width", default=None, help="board width: columns or percent (default: the last one you set with < > or --width, else 60)")
    p.add_argument("--restart", action="store_true", help="restart the board (e.g. after an update); agents keep running")
    p = sub.add_parser("tmux-exit", help="(internal) run by agent windows when the agent exits"); p.set_defaults(fn=cmd_tmux_exit)
    p.add_argument("--rc", type=int, default=0); p.add_argument("--started", type=float)
    p = sub.add_parser("shell", help="a bash whose commands show on the board (started by n → shell)"); p.set_defaults(fn=cmd_shell)
    p.add_argument("--id", default=None, help="session id to record under (default: a new uuid4)")
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
