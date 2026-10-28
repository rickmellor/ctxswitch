"""TOML configuration shared by collector, hub and clients.

Search order: $CTXSWITCH_CONFIG, then $XDG_CONFIG_HOME/ctxswitch/config.toml. Missing file = local defaults.
"""
from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def default_path() -> Path:
    env = os.environ.get("CTXSWITCH_CONFIG")
    if env:
        return Path(env)
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ctxswitch" / "config.toml"


def data_dir() -> Path:
    p = Path(os.environ.get("CTXSWITCH_DATA", "") or Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "ctxswitch")
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class HubConfig:
    url: str = "http://127.0.0.1:7433"
    token: str = ""
    bind: str = "127.0.0.1"
    port: int = 7433
    db_path: str = ""                      # default: <data_dir>/hub.db


@dataclass
class CollectorConfig:
    host_id: str = ""                      # default: hostname
    claude_roots: list[str] = field(default_factory=lambda: ["~/.claude/projects"])
    claude_sessions_dir: str = "~/.claude/sessions"
    pi_roots: list[str] = field(default_factory=lambda: ["~/.pi/agent/sessions"])
    claude_win_roots: list[str] = field(default_factory=list)   # e.g. /mnt/c/Users/<me>/.claude/projects
    queue_path: str = ""                   # default: <data_dir>/collector-queue.db
    backfill_days: int = 30                # how far back to index on first run (hard signals only)
    max_text_chars: int = 4000             # trim for user/assistant text shipped to the hub


@dataclass
class SeatConfig:
    name: str = "text"
    url: str = "http://llm.local:4000/v1"
    api_key: str = "local"
    model: str = "saint-local-text"
    concurrency: int = 1
    priority: int = 10                     # lower = preferred
    disable_thinking: bool = True          # Qwen seats: thinking eats the whole budget (spikes/SPIKES.md)


@dataclass
class Config:
    hub: HubConfig = field(default_factory=HubConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    seats: list[SeatConfig] = field(default_factory=lambda: [SeatConfig()])
    notify: bool = False

    @property
    def host_id(self) -> str:
        return self.collector.host_id or socket.gethostname()


def _apply(obj, d: dict):
    for k, v in d.items():
        if hasattr(obj, k):
            setattr(obj, k, v)
    return obj


def load(path: Path | None = None) -> Config:
    path = path or default_path()
    cfg = Config()
    if not path.exists():
        return cfg
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    _apply(cfg.hub, raw.get("hub", {}))
    _apply(cfg.collector, raw.get("collector", {}))
    if "seats" in raw:
        cfg.seats = [_apply(SeatConfig(), s) for s in raw["seats"]]
    cfg.notify = bool(raw.get("notify", cfg.notify))
    return cfg
