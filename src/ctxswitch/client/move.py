"""Move a Claude session between [[claude_profiles]] (accounts): Bedrock <-> Max.

A Claude Code session is its transcript, ``<config_dir>/projects/<cwd slug>/<id>.jsonl``, plus a sidecar directory
``<id>/`` (tool results, subagents). Moving both into another profile's config directory makes ``claude --resume``
find the session there. The same id stays the same ctxswitch session; the collector re-indexes the file at its new
path and the hub swaps the transcript path, so the next Enter resumes it under the new profile.

A running session can be moved too, when its agent is in a pane of ctx's tmux session: it is ended the way `k`
ends it, the move waits until Claude's session registry no longer lists it (the process is gone, so the transcript
is complete), then the files move and it resumes under the other account.

The transcript remembers the model it last used, and the other account won't have it (a Bedrock model id means
nothing to a Max subscription, and the other way round), so the first resume after a move passes the target
profile's ``model``. Only a session that isn't running can move, and only on the machine that holds its files.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from ctxswitch.common.config import ClaudeProfile, profile_for, profile_named
from ctxswitch.common.schema import LIVE


class CannotMove(ValueError):
    pass


def check(s: dict, to: str, profiles: list[ClaudeProfile], this_host: str) -> tuple[ClaudeProfile, ClaudeProfile, Path]:
    """Why ``s`` can't move to profile ``to``, as CannotMove; else (from profile, to profile, transcript path)."""
    if s.get("harness") != "claude":
        raise CannotMove("only Claude Code sessions move between accounts")
    if s.get("host_id") != this_host:
        raise CannotMove(f"its files are on {s.get('host_id')}: run `ctx move` there")
    if s.get("live_status") in LIVE:
        raise CannotMove("it's running: end it first (k), then move it")
    target = profile_named(to, profiles)
    if target is None:
        raise CannotMove(f"no claude profile {to!r} (have: {', '.join(p.name for p in profiles)})")
    src = profile_for(s, profiles)
    if src.name == target.name:
        raise CannotMove(f"already under {target.name}")
    tp = (s.get("meta") or {}).get("transcript_path") or s.get("transcript_path")
    path = Path(tp) if tp else None
    if path is None or not path.is_file():
        raise CannotMove(f"transcript not found: {tp or '(unknown)'}")
    return src, target, path


def can_end_here(s: dict) -> bool:
    """Whether `x` can end this live session itself: its agent runs in a pane of ctx's tmux session."""
    from ctxswitch.client import tmux
    return s.get("live_status") in LIVE and tmux.in_tmux() and tmux.agent_pane(s["key"]) is not None


def end_and_wait(s: dict, cfg, timeout: float = 20.0) -> str | None:
    """End the session's agent (as `k` does) and wait until Claude's registry no longer lists it, so the transcript
    is complete and closed. Marks ``s`` ended on success. Returns why not, else None."""
    from ctxswitch.client import tmux
    from ctxswitch.collector.liveness import read_claude_registry
    p = tmux.agent_pane(s["key"])
    if p is None:
        return "it runs outside ctx's tmux (another terminal, or another host): quit it there, then x"
    tmux.end_agent(p)
    deadline = time.time() + timeout
    while time.time() < deadline:
        live: dict = {}
        for d in cfg.claude_sessions_dirs():
            live |= read_claude_registry(d, cfg.host_id)
        if s["key"] not in live:
            time.sleep(0.5)                               # let the last transcript lines land
            s["live_status"] = "ended"
            return None
        time.sleep(0.3)
    return f"it didn't exit within {timeout:.0f} s"


def move(s: dict, to: str, profiles: list[ClaudeProfile], this_host: str) -> tuple[ClaudeProfile, Path]:
    """Move the session's transcript and sidecar into profile ``to``'s config directory. Returns (profile, new
    transcript path) and updates ``s`` so a resume right after picks the new profile."""
    _, target, path = check(s, to, profiles, this_host)
    dst_dir = target.home / "projects" / path.parent.name
    dst = dst_dir / path.name
    if dst.exists():
        raise CannotMove(f"{dst} already exists")
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dst))
    side = path.with_suffix("")
    if side.is_dir():
        shutil.move(str(side), str(dst.with_suffix("")))
    s.setdefault("meta", {})["transcript_path"] = str(dst)
    return target, dst


def resume_extra(target: ClaudeProfile) -> list[str]:
    """Arguments for the first resume after a move: the profile's model, so the other account's model id isn't asked for."""
    return ["--model", target.model] if target.model else []


def other_profiles(s: dict, profiles: list[ClaudeProfile]) -> list[ClaudeProfile]:
    cur = profile_for(s, profiles).name
    return [p for p in profiles if p.name != cur]


def command_hint(s: dict, target: ClaudeProfile, profiles: list[ClaudeProfile]) -> str:
    """How to resume by hand after a move outside tmux."""
    import shlex
    from ctxswitch.client.tmux import resume_argv
    return f"cd {shlex.quote(s.get('cwd') or os.path.expanduser('~'))} && {shlex.join(resume_argv(s, profiles, resume_extra(target)) or [])}"
