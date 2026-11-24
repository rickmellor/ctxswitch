"""The board's local-pool panel: what THIS machine's own tools say about the pool, in words.

Nothing here goes through the hub: every line comes from a local command — ``saint-pool status``,
``saint-pool config get --json``, ``saint-pool config reviewers --json`` and, when it's on PATH,
``pool-stats --json --since <today>`` — so a board on any host shows the pool that host would actually use, and a
host with no hub at all still gets its panel. The CPU/GPU panel (``g``) is a different thing and untouched.

Editing is the same story: the panel never writes ``~/.claude/saint-pool`` or the pool's config file itself
(saint-pool owns those), it runs ``saint-pool config set KEY VALUE`` and reads back what the tool now says. So the
panel is only ever as right as the tools are, and what they can't answer prints "—" or one dim line, never a guess.

Every command goes through one runner (:class:`PoolTools`), so a test hands the panel canned output instead of
letting it run anything real.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime
from typing import NamedTuple

from rich.text import Text
from textual.widgets import Static

from ctxswitch.client.tui.metrics_panel import DASH, header_band

POOL_TOOL = "saint-pool"                   # the switch and its config, on this host's PATH
STATS_TOOL = "pool-stats"                  # today's pool numbers; optional, and says so when it isn't there
TOOL_TIMEOUT_S = 6.0                       # saint-pool status probes SAINT's /health, which can take a few seconds
ROUND_MIN, ROUND_MAX = 1, 5                # what `rounds` may hold, and what + - walk between
EDIT_KEYS = "T review · R reviewer · + − rounds · r refresh"
READ_ONLY_MSG = f"read-only: no {POOL_TOOL} on this host's PATH to set"

# the keys the panel edits, as (what the board's action calls it, the config key it sets)
KEY_REVIEW = "enabled"
KEY_REVIEWER = "reviewer"
KEY_ROUNDS = "rounds"


class Cmd(NamedTuple):
    """One local command's answer: its exit code and the two streams, as text (never bytes — the panel reads words)."""
    rc: int
    out: str
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def said(self) -> str:
        """What to show when it didn't work: stderr's first line, or stdout's, or just the code."""
        for line in ((self.err or "") + "\n" + (self.out or "")).splitlines():
            if line.strip():
                return line.strip()[:120]
        return f"exit {self.rc}"


def default_run(argv: list[str]) -> Cmd:
    """The one place the panel runs a command. It never raises: a tool that isn't there is ``rc=127``."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=TOOL_TIMEOUT_S)     # noqa: S603
        return Cmd(int(p.returncode), p.stdout or "", p.stderr or "")
    except subprocess.TimeoutExpired:
        return Cmd(124, "", f"{argv[0] if argv else '?'}: timed out after {TOOL_TIMEOUT_S:.0f}s")
    except OSError as e:
        return Cmd(127, "", str(e))


class PoolTools:
    """The pool's own command-line tools, as the panel needs them: four reads and one write, through one runner.

    ``run`` takes an argv list and answers :class:`Cmd`; ``which`` answers where a tool is (None when it isn't).
    Both default to the real thing; a test passes its own and never runs a command."""

    def __init__(self, run=None, which=None):
        self._run = run or default_run
        self._which = which or shutil.which

    def have(self, tool: str) -> bool:
        return bool(self._which(tool))

    def status(self) -> Cmd:
        return self._run([POOL_TOOL, "status"])

    def config(self) -> Cmd:
        return self._run([POOL_TOOL, "config", "get", "--json"])

    def reviewers(self) -> Cmd:
        return self._run([POOL_TOOL, "config", "reviewers", "--json"])

    def stats(self, since: str) -> Cmd | None:
        """Today's numbers, or None when the tool isn't installed — which is a dim line, not an error."""
        return self._run([STATS_TOOL, "--json", "--since", since]) if self.have(STATS_TOOL) else None

    def set(self, key: str, value: str) -> Cmd:
        """One edit. This is the only way the panel changes anything: it never writes a config file itself."""
        return self._run([POOL_TOOL, "config", "set", key, str(value)])


# -- reading: what the commands said, in fields ---------------------------------------------------------------------

_STATUS_RE = re.compile(r"saint-pool:\s*(?P<state>ON|OFF)\b(?P<rest>.*)", re.I)
_URL_RE = re.compile(r"https?://[^\s,)]+")


def parse_status(text: str) -> dict:
    """``saint-pool status`` as fields: state (on/off/""), url, reachable (None when the line doesn't say) and the
    ``review:`` line when that version of the tool prints one. A status line this code doesn't know is state ""."""
    out = {"state": "", "url": "", "reachable": None, "review_line": ""}
    for line in (text or "").splitlines():
        m = re.search(r"review:\s*(.*)", line, re.I)
        if m and not _STATUS_RE.search(line):
            out["review_line"] = m.group(1).strip() or out["review_line"]
        s = _STATUS_RE.search(line)
        if not s:
            continue
        out["state"] = s.group("state").lower()
        rest = s.group("rest") or ""
        u = _URL_RE.search(rest)
        out["url"] = u.group(0) if u else ""
        low = rest.lower()
        if "unreachable" in low or "not reachable" in low:
            out["reachable"] = False
        elif "reachable" in low:
            out["reachable"] = True
    return out


def _loads(cmd: Cmd):
    """A command's stdout as JSON, or None when it failed or printed something that isn't."""
    if cmd is None or not cmd.ok:
        return None
    try:
        return json.loads((cmd.out or "").strip())
    except ValueError:
        return None


def parse_settings(cmd: Cmd) -> dict | None:
    """``saint-pool config get --json`` as its five fields (enabled, reviewer, rounds, applies_to,
    skip_read_mostly), whatever order it lists them in. No answer at all is None, which the panel says in words."""
    body = _loads(cmd)
    if not isinstance(body, dict):
        return None
    review = body.get("review") if isinstance(body.get("review"), dict) else {}
    get = lambda k: body[k] if k in body else review.get(k)                       # noqa: E731 - two places, one read
    return {k: get(k) for k in ("enabled", "reviewer", "rounds", "applies_to", "skip_read_mostly") if get(k) is not None}


def parse_reviewers(cmd: Cmd) -> list | None:
    """``saint-pool config reviewers --json`` as rows (name, cloud, verified), in the order it lists them. None when
    the tool has no such subcommand yet, which is one dim line rather than an empty list."""
    body = _loads(cmd)
    rows = body.get("reviewers") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        return None
    return [{"name": str(r.get("name") or ""), "cloud": str(r.get("cloud") or ""),
             "verified": bool(r.get("verified"))} for r in rows if isinstance(r, dict)]


def parse_stats(cmd: Cmd) -> dict | None:
    """``pool-stats --json --since <today>`` down to what the panel has room for: jobs finished, how many of them
    were ok, job-hours, the verify tally, the asks, and the review verdicts when that build reports them."""
    body = _loads(cmd)
    if not isinstance(body, dict):
        return None
    pool = (body.get("window") or {}).get("pool")
    if not isinstance(pool, dict):
        pool = body.get("pool") if isinstance(body.get("pool"), dict) else {}
    out: dict = {}
    for key in ("jobs_finished", "ok", "ok_rate", "job_hours", "verify_pass", "verify_fail", "asks"):
        if isinstance(pool.get(key), (int, float)) and not isinstance(pool.get(key), bool):
            out[key] = pool[key]
    verdicts = pool.get("review")
    if isinstance(verdicts, dict):
        out["review"] = {str(k): int(v) for k, v in verdicts.items() if isinstance(v, (int, float))}
    return out or None


def today() -> str:
    """The day ``pool-stats --since`` is asked for: today, this host's calendar date."""
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def read_pool(tools: PoolTools, since: str) -> dict:
    """Everything the panel shows, from this host's tools: the switch, the review settings, the reviewer list,
    today's numbers — plus what's missing and whether anything can be edited at all.

    Without ``saint-pool`` on PATH there is nothing to read or set, and the snapshot says so (``readonly``); the
    day's numbers still come through when ``pool-stats`` is there. A tool that answered nothing leaves its field
    None, which the panel puts one dim line on."""
    have_tool = tools.have(POOL_TOOL)
    snap: dict = {"readonly": not have_tool, "missing": [], "error": None,
                  "pool": parse_status(""), "settings": None, "reviewers": None, "stats": None}
    if not have_tool:
        snap["missing"].append(POOL_TOOL)
    else:
        st = tools.status()
        snap["pool"] = parse_status(st.out if st.ok else "")
        if not st.ok:
            snap["status_error"] = st.said()
        snap["settings"] = parse_settings(tools.config())
        snap["reviewers"] = parse_reviewers(tools.reviewers())
    stats = tools.stats(since)
    snap["stats"] = parse_stats(stats) if stats is not None else None
    if stats is None:
        snap["missing"].append(STATS_TOOL)
    return snap


def set_value(tools: PoolTools, key: str, value) -> str | None:
    """One edit, and the words to show when saint-pool refuses it (None when it took it)."""
    cmd = tools.set(key, str(value))
    return None if cmd.ok else cmd.said()


def next_reviewer(reviewers: list | None, current: str) -> str | None:
    """The next VERIFIED reviewer after the one in use, cycling; None when the list names none. An unverified
    reviewer is never picked, and a current one that isn't in the list still walks the verified ones from the first."""
    ok = [str(r.get("name") or "") for r in (reviewers or []) if isinstance(r, dict) and r.get("verified")]
    ok = [n for n in ok if n]
    if not ok:
        return None
    return ok[(ok.index(current) + 1) % len(ok)] if current in ok else ok[0]


# -- rendering ------------------------------------------------------------------------------------------------------

def _on_off(v) -> tuple[str, str]:
    """A setting as "on"/"off"/"—" and its colour: on is what the pool is doing, so it's the one that stands out."""
    if v is None:
        return DASH, "dim"
    return ("on", "green") if v else ("off", "dim")


def _num(v, fmt: str = "{:.0f}") -> str:
    return fmt.format(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else DASH


def _clip(t: Text, width: int) -> Text:
    return t[:max(4, width)]


def _joined(parts: list[tuple[str, str]], width: int, label: str = "", sep: str = " · ") -> Text:
    """``label<sep>a<sep>b<sep>c`` from (words, style) pairs, giving ground from the right: a pane too narrow for the
    lot drops the last part, then the one before it, so what's left is whole words and never a word cut in half.
    What's kept is the label and the first part however tight the pane gets; narrower than those, they're cut."""
    def line(keep: int) -> Text:
        t = Text(label, style="bold")
        for i, (s, style) in enumerate(parts[:keep]):
            t.append((sep if (i or label) else "") + s, style)
        return t

    for keep in range(len(parts), 0, -1):
        if len(line(keep).plain) <= width:
            return line(keep)
    return _clip(line(1), width)


def _head_line(pool: dict, width: int, missing: list, snap: dict) -> Text:
    """``pool · OFF``, or ON with its URL and whether SAINT answered — the switch, as ``saint-pool status`` said it."""
    if POOL_TOOL in missing:                                   # this host can't ask: that says the whole panel
        return header_band(_joined([(f"{POOL_TOOL} not on PATH", "yellow")], width, label="pool"), width)
    state = str(pool.get("state") or "")
    if state == "on":
        parts: list[tuple[str, str]] = [("ON", "bold green")]
    elif state == "off":
        return header_band(_joined([("OFF", "dim")], width, label="pool"), width)
    else:                                                      # no status line at all: say what the tool said
        return header_band(_joined([(snap.get("status_error") or "no status", "yellow")], width, label="pool"), width)
    if pool.get("url"):
        parts.append((str(pool["url"]), "dim"))
    reach = pool.get("reachable")
    if reach is False:
        parts.append(("UNREACHABLE", "red"))
    elif reach is True:
        parts.append(("reachable", "green"))
    return header_band(_joined(parts, width, label="pool"), width)


def _settings_line(settings: dict | None, width: int) -> Text:
    """``review on · qwen-max · 2 rounds · construction · skip read-mostly on`` — what the review pass will do, in
    the order it asks about it: whether, who, how many times, what it applies to, and what it waves through. A pane
    too narrow loses them from the right, so the parts the panel's own keys edit are the last to go."""
    if settings is None:
        return _clip(Text(f"{POOL_TOOL} config get: no answer (older saint-pool?)", "dim"), width)
    on, style = _on_off(settings.get("enabled"))
    rounds = settings.get("rounds")
    applies = settings.get("applies_to")
    if isinstance(applies, (list, tuple)):
        applies = ", ".join(str(a) for a in applies)
    skip, sstyle = _on_off(settings.get("skip_read_mostly"))
    parts: list[tuple[str, str]] = [
        (on, style),
        (str(settings.get("reviewer") or DASH), "bold" if settings.get("reviewer") else "dim"),
        (f"{_num(rounds)} round{'s' if _num(rounds) != '1' else ''}", "dim"),
        (str(applies or DASH), "dim"),
        (f"skip read-mostly {skip}", sstyle)]
    return _joined(parts, width, label="review")


def _reviewers_line(reviewers: list | None, width: int) -> Text:
    """The reviewer list, each name with where it runs when that isn't the pool itself, and a ✓ when it's verified
    (only those are offered by R) or a ✗ when it isn't. A pane too narrow for every name says how many didn't fit."""
    if reviewers is None:
        return _clip(Text(f"{POOL_TOOL} config reviewers: no answer (older saint-pool?)", "dim"), width)
    if not reviewers:
        return _clip(Text("reviewers: none named", "dim"), width)
    for keep in range(len(reviewers), 0, -1):
        t = Text("reviewers ", style="bold")
        for i, r in enumerate(reviewers[:keep]):
            name = str(r.get("name") or "?")
            cloud = str(r.get("cloud") or "")
            t.append((" · " if i else "") + (f"{name} ({cloud})" if cloud and cloud != "none" else name),
                     "bold" if r.get("verified") else "dim")
            t.append(" ✓" if r.get("verified") else " ✗", "green" if r.get("verified") else "red")
        if keep < len(reviewers):
            t.append(f" · +{len(reviewers) - keep} more", "dim")
        if len(t.plain) <= width:
            return t
    return _clip(Text(f"reviewers {len(reviewers)} named", "dim"), width)


def _stats_line(stats: dict | None, width: int, missing: list) -> Text:
    """Today, as the ledger says: jobs, how they ended, the time they took, the verify tally and the asks — and the
    review verdicts when this build of pool-stats reports them."""
    if STATS_TOOL in missing:
        return _clip(Text(f"{STATS_TOOL} not on PATH — no job numbers here", "dim"), width)
    if not stats:
        return _clip(Text(f"{STATS_TOOL}: nothing to report today", "dim"), width)
    jobs, ok, rate = stats.get("jobs_finished"), stats.get("ok"), stats.get("ok_rate")
    if not isinstance(rate, (int, float)) and isinstance(jobs, (int, float)) and jobs:
        rate = (ok or 0) / float(jobs) * 100
    vfail = stats.get("verify_fail")
    asks = stats.get("asks")
    parts: list[tuple[str, str]] = [
        (f"{_num(jobs)} jobs", "bold" if isinstance(jobs, (int, float)) else "dim"),
        (f"ok {_num(rate)}%", "green" if isinstance(rate, (int, float)) and rate >= 80
         else "yellow" if isinstance(rate, (int, float)) else "dim"),
        (f"{_num(stats.get('job_hours'), '{:.1f}')} job-h", "dim"),
        (f"verify {_num(stats.get('verify_pass'))}/{_num(vfail)}", "red" if isinstance(vfail, (int, float)) and vfail else "dim"),
        (f"{_num(asks)} asks", "yellow" if isinstance(asks, (int, float)) and asks else "dim")]
    for word, n in sorted((stats.get("review") or {}).items(), key=lambda kv: (-kv[1], str(kv[0]))):
        parts.append((f"review {n} {word}", "green" if str(word).startswith("ok") else "yellow"))
    return _joined(parts, width, label="today")


def panel_text(snap: dict, width: int) -> Text:
    """The snapshot :func:`read_pool` answers, as the panel: the switch on a band, then the review settings, the
    reviewers, today's numbers, and the keys that change them. No line is wider than ``width``; a value a tool
    didn't answer is "—", and a tool that isn't installed is one dim line."""
    snap = snap or {}
    pool, missing = snap.get("pool") or {}, list(snap.get("missing") or [])
    lines = [_head_line(pool, width, missing, snap)]
    if snap.get("pool", {}).get("review_line"):                # a status line that carries its own review note
        lines.append(_clip(Text.assemble(("review: ", "dim"), str(snap["pool"]["review_line"]), ""), width))
    if POOL_TOOL in missing:                                   # nothing below it can be read either
        lines.append(_clip(Text(f"{POOL_TOOL} config: nothing to read, and nothing to set", "dim"), width))
    else:
        lines.append(_settings_line(snap.get("settings"), width))
        lines.append(_reviewers_line(snap.get("reviewers"), width))
    lines.append(_stats_line(snap.get("stats"), width, missing))
    if snap.get("error"):
        lines.append(_clip(Text(f"config set: {snap['error']}", "red"), width))
    lines.append(_clip(Text(READ_ONLY_MSG if snap.get("readonly") else EDIT_KEYS, "dim"), width))
    return Text("\n").join(lines)


class PoolPanel(Static):
    """The board's local-pool strip (id "pool"): hidden until ``P``, then what this host's pool tools last said,
    which the app puts there with :func:`panel_text`. It refreshes when it opens and on ``r`` — no timer, since
    nothing here is a live signal — and its edits go through saint-pool itself."""
