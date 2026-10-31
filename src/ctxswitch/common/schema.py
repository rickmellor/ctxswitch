"""Shared data model: what a collector extracts and what the hub stores.

Plain dataclasses that round-trip through JSON (``to_dict``/``from_dict``) so collector, hub and clients agree on
one vocabulary without a schema library.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Literal

Harness = Literal["claude", "pi", "claude-win", "agy", "shell"]
AskKind = Literal["question", "plan", "permission", "elicitation", "prompt", "plain"]
# plain = final text ending in "?" (heuristic); permission/elicitation/prompt come from hooks (hub/hooks.py)
UserOrigin = Literal["human", "peer", "task", "meta", "unknown", "shell"]
LiveStatus = Literal["busy", "waiting", "idle", "shell", "ended", "unknown"]   # waiting: Claude's dialog is up
LIVE = ("busy", "waiting", "idle", "shell")


def session_key(host_id: str, harness: str, session_id: str) -> str:
    return f"{host_id}/{harness}/{session_id}"


@dataclass
class Ask:
    kind: AskKind
    text: str                       # the question as the agent put it (trimmed)
    ref: str                        # tool_use id or "<turn idx>:plain"
    answered: bool = False
    answer: str | None = None       # the operator's answer / plan verdict / feedback text
    answered_at: str | None = None  # ISO timestamp


@dataclass
class ToolTally:
    edits: int = 0
    bash: int = 0
    reads: int = 0
    agents: int = 0
    other: int = 0
    errors: int = 0                 # tool results flagged is_error

    def summary(self) -> str:
        parts = []
        if self.edits: parts.append(f"{self.edits} edit{'s' if self.edits != 1 else ''}")
        if self.bash: parts.append(f"{self.bash} bash")
        if self.reads: parts.append(f"{self.reads} read{'s' if self.reads != 1 else ''}")
        if self.agents: parts.append(f"{self.agents} agent{'s' if self.agents != 1 else ''}")
        if self.other: parts.append(f"{self.other} other")
        if self.errors: parts.append(f"{self.errors} err")
        return ", ".join(parts) or "no tools"


@dataclass
class Turn:
    """One operator-facing exchange: the human's prompt and everything the agent did until the next prompt."""
    idx: int                        # 0-based within the session
    started_at: str                 # ISO, from the user entry
    ended_at: str | None            # ISO of the last entry seen for this turn
    user_text: str                  # verbatim (trimmed to a cap)
    user_origin: UserOrigin
    assistant_text: str             # the agent's last text block in the turn (trimmed)
    tally: ToolTally = field(default_factory=ToolTally)
    asks: list[Ask] = field(default_factory=list)
    events: list[str] = field(default_factory=list)   # "compaction", "api_error", "slash_command:/x", "branch" …
    usage: dict | None = None       # {"input","output","cache_read","cache_creation"}, summed over the turn's
                                    # assistant entries; None when the harness reports none (shell, agy)
    model: str = ""                 # model of the turn's last assistant entry (pi: "provider/model"); "" unknown
    cost_usd: float | None = None   # a harness-billed cost (pi prices its cloud providers itself); else None
    complete: bool = False          # the harness closed the turn, or a later prompt exists, or the session ended
    offset_end: int = 0             # byte offset after the last entry consumed; the replay/idempotence key

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Turn":
        d = dict(d)
        d["tally"] = ToolTally(**d.get("tally", {}))
        d["asks"] = [Ask(**a) for a in d.get("asks", [])]
        return cls(**d)


@dataclass
class SessionMeta:
    host_id: str
    harness: Harness
    session_id: str
    transcript_path: str
    cwd: str = ""
    repo_root: str = ""             # resolved by the collector when cwd is inside a git work tree
    repo_remote: str = ""           # origin URL, used to group workstreams across hosts
    git_branch: str = ""
    title: str = ""                 # ai-title / custom-title; falls back to the first prompt
    name: str = ""                  # harness-assigned session name (Claude agent-name)
    harness_version: str = ""
    started_at: str = ""
    last_activity: str = ""
    model: str = ""
    permission_mode: str = ""
    profile: str = ""               # the [[claude_profiles]] account the transcript lives under, named by its own host
    # a Claude session a pool job launched (collector/jobs.py reads the saint-agent ledger):
    # empty/None for every other session.
    parent_key: str = ""            # f"{host}/claude/{launching session}", "" when the ledger doesn't name one
    job: str = ""                   # ledger job id, "sa-…"
    job_status: str = ""            # "running" until an end line exists, then its status
    job_brief: str = ""             # basename of the brief the job was given
    job_dur_s: int | None = None
    job_turns: int | None = None
    job_tokens: dict | None = None  # the end line's tokens, the same four keys as Turn.usage

    @property
    def key(self) -> str:
        return session_key(self.host_id, self.harness, self.session_id)

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["key"] = self.key
        return d


@dataclass
class Liveness:
    key: str
    status: LiveStatus
    status_since: str | None = None     # ISO
    pid: int | None = None
    socket: str | None = None           # Claude messagingSocketPath / pi extension socket
    name: str | None = None             # user-set session name (Claude registry); may not be in the transcript yet

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


PLAN_ASK = "Plan approval requested"


def question_text(questions: Any) -> str:
    """An AskUserQuestion ask's text, the same from its transcript tool_use and its PreToolUse hook."""
    qs = [q.get("question", "") if isinstance(q, dict) else q for q in questions or []]
    return trim(" | ".join(q for q in qs if isinstance(q, str) and q) or "(question)", 600)


USAGE_KEYS = ("input", "output", "cache_read", "cache_creation")


def add_usage(turn: "Turn", src: dict, map_: tuple[tuple[str, str], ...]) -> None:
    """Add one assistant entry's token counts onto the turn's usage, creating the dict on first use.

    ``src`` is the entry's usage object; ``map_`` pairs its key names with ours (the harnesses name them
    differently). Anything that isn't an int counts 0.
    """
    if turn.usage is None:
        turn.usage = dict.fromkeys(USAGE_KEYS, 0)
    for dst, src_key in map_:
        v = src.get(src_key)
        if isinstance(v, int):
            turn.usage[dst] += v


def trim(text: str, cap: int) -> str:
    """Keep the head and a shorter tail so both the ask and the sign-off survive trimming."""
    if len(text) <= cap:
        return text
    head = int(cap * 0.75)
    tail = cap - head - 3
    return text[:head] + "\n…\n" + text[-tail:]
