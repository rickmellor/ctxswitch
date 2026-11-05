"""``GET /v1/launch``: the one place that knows how to start or resume a session on any host.

The rule lives in the client (client/tmux.py's ``resume_argv`` / ``new_agent``) because there it can read the local
config. A front end that isn't Python — a herdr-app JS mod — can't, and must not re-derive it: a profile is an
account's command plus an ``env`` wrapper whose pieces only ``ClaudeProfile.argv()`` orders. So each collector
publishes what its host can launch (``info.profiles`` = ``ClaudeProfile.to_public()``, ``info.agents``, ``info.hc``)
and the hub answers from those, host by host: the argv to run, the environment to set and drop, where to start it,
and the key to label the terminal with.

The answer is the same argv ``tmux.resume_argv`` / ``tmux.new_agent`` build on that host — tests/test_launch_api.py
proves the parity from the same ``ClaudeProfile`` objects the collector published.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

HARNESSES = ("claude", "pi", "agy", "shell")


def _host_info(store, host: str) -> dict[str, Any]:
    """What one host last said about itself (collector/main.py's ``host_info``), as ``store.host_seen`` kept it."""
    r = store.db.execute("SELECT info FROM hosts WHERE host_id=?", (host,)).fetchone()
    if r is None or not r[0]:
        return {}
    try:
        return json.loads(r[0]) or {}
    except (ValueError, TypeError):
        return {}


def _owns(pub: dict, transcript_path: str | None) -> bool:
    """Whether a transcript lives under a published profile's config dir (``ClaudeProfile.owns``, from its fields)."""
    if not transcript_path:
        return False
    home = Path(os.path.expanduser(pub.get("config_dir") or "~/.claude"))
    try:
        Path(transcript_path).relative_to(home / "projects")
        return True
    except (ValueError, TypeError):
        return False


def _published(store, host: str) -> list[dict]:
    pubs = _host_info(store, host).get("profiles")
    return [p for p in pubs if isinstance(p, dict)] if isinstance(pubs, list) else []


def _profile_of(pubs: list[dict], transcript_path: str | None) -> dict | None:
    """The profile a Claude session was started under, by where its transcript lies (``config.profile_for``'s rule:
    an owner with a config_dir of its own, else the first owner, else the first profile)."""
    owners = [p for p in pubs if _owns(p, transcript_path)]
    return next((p for p in owners if p.get("config_dir")), owners[0] if owners else (pubs[0] if pubs else None))


def _named(pubs: list[dict], name: str | None) -> dict | None:
    """A published profile by name, or the first when none is named (``config.profile_named``); None = no such name."""
    if not name:
        return pubs[0] if pubs else None
    return next((p for p in pubs if p.get("name") == name), None)


def _reply(host: str, info: dict, pub: dict | None, harness: str, profile: str | None, argv: list[str], cwd: str,
           key: str | None, label: str) -> dict[str, Any]:
    """One launch answer: what to run, in what environment, where, and what to call the terminal."""
    pub = pub or {}
    agents = info.get("agents") if isinstance(info.get("agents"), dict) else None
    config_dir = str(pub.get("config_dir") or "")    # baked into argv's `env K=V` pairs already; here to read, not apply
    return {"host": host or "", "harness": harness, "profile": profile, "argv": argv, "cwd": cwd,
            "env": dict(pub.get("env") or {}), "unset": list(pub.get("unset") or []), "config_dir": config_dir,
            "label": label, "key": key, "installed": (agents or {}).get(harness)}


def _label(pubs: list[dict], harness: str, profile: str | None) -> str:
    """The window's name as client/tmux.py's ``new_agent`` labels it: the harness, with the account named when the
    host has more than one to choose between."""
    return f"{harness}[{profile}]" if harness == "claude" and profile and len(pubs) > 1 else harness


def recipe(store, *, key: str | None = None, host: str | None = None, harness: str | None = None,
           profile: str | None = None, cwd: str | None = None) -> dict[str, Any] | None:
    """How to start (or resume) one session on its host — None when it can't be answered (the route says 404 for an
    unknown session key, 400 for a missing or unknown host/harness/profile).

    With ``key``: resume that session — its host, harness and profile (whichever published config dir holds its
    transcript) all come from its row. Without one: a fresh session on ``host`` (required) under ``harness``
    (required), Claude under ``profile`` (default: the host's first)."""
    if key:
        s = store.session(key, turns=0)
        if not s:
            return None
        host, harness = s.get("host_id"), s.get("harness")
        if harness not in HARNESSES:
            return None
        sid = s.get("session_id") or ""
        tp = (s.get("meta") or {}).get("transcript_path") or s.get("transcript_path")
        pubs = _published(store, host)
        pub, name = None, None
        if harness == "claude":
            pub = _profile_of(pubs, tp) or {"name": "default", "argv": ["claude"]}
            name = pub.get("name")
            argv = [*(pub.get("argv") or ["claude"]), "--resume", sid]
        elif harness == "pi":
            argv = ["pi", "--session", tp] if tp else None
        elif harness == "agy":
            argv = ["agy", "--conversation", sid]
        else:
            argv = ["ctx", "shell", "--id", sid]
        if not argv:
            return None
        return _reply(host, _host_info(store, host), pub, harness, name, argv, s.get("cwd") or "~", key,
                      _label(pubs, harness, name))

    if not host:
        return None
    if harness not in HARNESSES:
        return None
    pubs, info = _published(store, host), _host_info(store, host)
    pub, name, new_key, argv = None, None, None, None
    if harness == "claude":
        pub = _named(pubs, profile)
        if pub is None and profile:                  # a name that host doesn't publish
            return None
        if pub is None:                              # a host that published nothing starts plain `claude`
            pub = {"name": "default", "argv": ["claude"]}
        name = pub.get("name")
        argv = [*(pub.get("argv") or ["claude"])]
        if pub.get("model"):
            argv += ["--model", pub["model"]]
    elif harness == "shell":
        new_key = f"{host}/shell/{uuid.uuid4()}"
        argv = ["ctx", "shell", "--id", new_key.rsplit("/", 1)[1]]
    else:
        argv = [harness]
    return _reply(host, info, pub, harness, name, argv, (cwd or "~").strip() or "~", new_key,
                  _label(pubs, harness, name))
