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
    recap: bool = True                     # one-line LLM recap per session (local seat)
    recap_days: int = 7                    # only sessions live or active this recently (recap and ledger)
    ledger: bool = True                    # extract instructions/decisions/claims/asks per exchange (same seat)


@dataclass
class CollectorConfig:
    host_id: str = ""                      # default: hostname
    claude_roots: list[str] = field(default_factory=lambda: ["~/.claude/projects"])
    claude_sessions_dir: str = "~/.claude/sessions"
    pi_roots: list[str] = field(default_factory=lambda: ["~/.pi/agent/sessions"])
    claude_win_roots: list[str] = field(default_factory=list)   # e.g. /mnt/c/Users/<me>/.claude/projects
    queue_path: str = ""                   # default: <data_dir>/collector-queue.db
    spool_dir: str = ""                    # hook/pi-extension events; default $CTXSWITCH_SPOOL or ~/.local/state/ctxswitch/spool
    backfill_days: int = 30                # how far back to index on first run (hard signals only)
    max_text_chars: int = 4000             # trim for user/assistant text shipped to the hub
    accept_messages: bool = True           # deliver operator messages from the hub into live sessions here


@dataclass
class SeatConfig:
    name: str = "text"
    # no config file (the hub container): $CTXSWITCH_SEAT_URL, else a seat on this machine
    url: str = field(default_factory=lambda: os.environ.get("CTXSWITCH_SEAT_URL", "http://127.0.0.1:4000/v1"))
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
    notify: bool = True                    # tmux pop-up for each new question / dialog (client/notify.py)
    notify_idle: bool = False              # ... and each time an agent finishes its turn
    # [ssh] <host_id> = "<ssh destination>": how this machine reaches another host's sessions to resume them.
    # Unlisted hosts use the host id itself (an ~/.ssh/config alias); "" = not reachable from here.
    ssh: dict[str, str] = field(default_factory=dict)

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
    cfg.notify_idle = bool(raw.get("notify_idle", cfg.notify_idle))
    cfg.ssh = {str(k): str(v) for k, v in (raw.get("ssh") or {}).items()}
    return cfg
