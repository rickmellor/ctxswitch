"""TOML configuration shared by collector, hub and clients.

Search order: $CTXSWITCH_CONFIG, then $XDG_CONFIG_HOME/ctxswitch/config.toml. Missing file = local defaults.
"""
from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


AGY_ROOT = "~/.gemini/antigravity-cli/conversations"    # where Antigravity (agy) keeps one SQLite file per conversation


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
    agy_roots: list[str] = field(default_factory=list)   # Antigravity conversations (<id>.db); load() defaults it to AGY_ROOT
    shell_roots: list[str] = field(default_factory=lambda: ["~/.local/state/ctxswitch/shells"])   # `ctx shell` transcripts (<id>.jsonl + <id>.out)
    queue_path: str = ""                   # default: <data_dir>/collector-queue.db
    spool_dir: str = ""                    # hook/pi-extension events; default $CTXSWITCH_SPOOL or ~/.local/state/ctxswitch/spool
    backfill_days: int = 30                # how far back to index on first run (hard signals only)
    max_text_chars: int = 4000             # trim for user/assistant text shipped to the hub
    accept_messages: bool = True           # deliver operator messages from the hub into live sessions here


@dataclass
class SeatConfig:
    name: str = "text"
    # no config file (the hub container): $CTXSWITCH_SEAT_URL / _MODEL / _API_KEY, else a seat on this machine
    url: str = field(default_factory=lambda: os.environ.get("CTXSWITCH_SEAT_URL") or "http://127.0.0.1:4000/v1")
    api_key: str = field(default_factory=lambda: os.environ.get("CTXSWITCH_SEAT_API_KEY") or "local")
    model: str = field(default_factory=lambda: os.environ.get("CTXSWITCH_SEAT_MODEL") or "saint-local-text")
    concurrency: int = 1                   # not used yet: calls run one at a time
    priority: int = 10                     # lower = preferred; the hub uses the one seat with the lowest
    disable_thinking: bool = True          # Qwen seats: thinking eats the whole budget (spikes/SPIKES.md)


@dataclass
class ClaudeProfile:
    """One way to start Claude Code: an account/config directory and the command that launches it. `c` on the board
    and Enter (resume) use the profile a session's transcript lives under, so a Max session resumes as Max."""
    name: str = "default"
    command: list[str] = field(default_factory=lambda: ["claude"])   # argv; wrappers welcome (a script, not an alias)
    config_dir: str = ""                   # CLAUDE_CONFIG_DIR for this profile; "" = Claude's default (~/.claude)
    env: dict[str, str] = field(default_factory=dict)              # extra environment for the launch
    unset: list[str] = field(default_factory=list)                 # environment to drop (AWS_PROFILE, CLAUDE_CODE_USE_BEDROCK)
    model: str = ""                        # --model for new sessions and for one moved here from another profile ("" = Claude's default)

    @property
    def home(self) -> Path:
        """The profile's Claude config directory. Deliberately not $CLAUDE_CONFIG_DIR: a profile is a fixed place
        (the collector's default roots are ~/.claude too), so the board means the same thing from any shell."""
        return Path(os.path.expanduser(self.config_dir or "~/.claude"))

    def argv(self) -> list[str]:
        """The launch argv: ``env`` wraps the command when the profile changes the environment."""
        kv = dict(self.env)
        if self.config_dir:
            kv["CLAUDE_CONFIG_DIR"] = str(self.home)
        if not kv and not self.unset:
            return list(self.command)
        out = ["env"]
        for u in self.unset:
            out += ["-u", u]
        out += [f"{k}={v}" for k, v in kv.items()]
        return out + list(self.command)

    def owns(self, transcript_path: str | None) -> bool:
        """Whether a transcript (``<config_dir>/projects/<slug>/<id>.jsonl``) belongs to this profile."""
        if not transcript_path:
            return False
        try:
            Path(transcript_path).relative_to(self.home / "projects")
            return True
        except ValueError:
            return False


def profile_for(s: dict, profiles: list[ClaudeProfile]) -> ClaudeProfile:
    """The profile a Claude session was started under (by its transcript's config directory), else the first."""
    tp = (s.get("meta") or {}).get("transcript_path") or s.get("transcript_path")
    owners = [p for p in profiles if p.owns(tp)]
    return next((p for p in owners if p.config_dir), owners[0] if owners else profiles[0] if profiles else ClaudeProfile())


def profiles_matter(profiles: list[ClaudeProfile]) -> bool:
    """Whether Claude sessions are labelled by profile: any [[claude_profiles]] given (a Max-only host names its one
    account "max" so other boards show it), as opposed to the implicit default."""
    return len(profiles) > 1 or any(p.config_dir or p.name != "default" for p in profiles)


def profile_named(name: str | None, profiles: list[ClaudeProfile]) -> ClaudeProfile | None:
    if not name:
        return profiles[0] if profiles else ClaudeProfile()
    return next((p for p in profiles if p.name == name), None)


@dataclass
class Config:
    hub: HubConfig = field(default_factory=HubConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    seats: list[SeatConfig] = field(default_factory=lambda: [SeatConfig()])
    # [[claude_profiles]]: ways to start Claude Code (accounts). The first is what `c` uses unless you name another.
    # Each profile's config_dir adds <dir>/projects and <dir>/sessions to what the collector watches.
    claude_profiles: list[ClaudeProfile] = field(default_factory=lambda: [ClaudeProfile()])
    notify: bool = True                    # tmux pop-up for each new question / dialog (client/notify.py)
    notify_idle: bool = False              # ... and each time an agent finishes its turn
    notify_style: str = "popup"            # "popup": a box over the panes until a key; "status": the status line, 8 s
    # [ssh] <host_id> = "<ssh destination>": how this machine reaches another host's sessions to resume them.
    # Unlisted hosts use the host id itself (an ~/.ssh/config alias); "" = not reachable from here.
    ssh: dict[str, str] = field(default_factory=dict)

    @property
    def host_id(self) -> str:
        return self.collector.host_id or socket.gethostname()

    def claude_sessions_dirs(self) -> list[Path]:
        """Every Claude session registry to poll for liveness: the configured one plus each profile's."""
        out = [Path(os.path.expanduser(self.collector.claude_sessions_dir))]
        for p in self.claude_profiles:
            if p.config_dir and p.home / "sessions" not in out:
                out.append(p.home / "sessions")
        return out


def _apply(obj, d: dict):
    for k, v in d.items():
        if hasattr(obj, k):
            setattr(obj, k, v)
    return obj


def load(path: Path | None = None) -> Config:
    path = path or default_path()
    cfg = Config()
    if not path.exists():
        cfg.collector.agy_roots = [AGY_ROOT]
        return cfg
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    _apply(cfg.hub, raw.get("hub", {}))
    _apply(cfg.collector, raw.get("collector", {}))
    if "agy_roots" not in raw.get("collector", {}):      # on by default; [] in the file opts out
        cfg.collector.agy_roots = [AGY_ROOT]
    if "seats" in raw:
        cfg.seats = [_apply(SeatConfig(), s) for s in raw["seats"]]
    cfg.notify = bool(raw.get("notify", cfg.notify))
    cfg.notify_idle = bool(raw.get("notify_idle", cfg.notify_idle))
    cfg.notify_style = str(raw.get("notify_style", cfg.notify_style))
    cfg.ssh = {str(k): str(v) for k, v in (raw.get("ssh") or {}).items()}
    if "claude_profiles" in raw:
        cfg.claude_profiles = [_apply(ClaudeProfile(), p) for p in raw["claude_profiles"]] or cfg.claude_profiles
    for p in cfg.claude_profiles:                       # a profile's transcripts are indexed like the default ones
        if p.config_dir and str(p.home / "projects") not in [os.path.expanduser(r) for r in cfg.collector.claude_roots]:
            cfg.collector.claude_roots.append(str(p.home / "projects"))
    return cfg
