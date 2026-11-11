"""The fast matcher behind "that prompt looks like it's for another session" — pure, no I/O, no LLM.

The operator runs several coding-agent sessions at once and sometimes types into the wrong window. A prompt is
compared against the fingerprints of the other live sessions: words that say *which work* the prompt is about. A
fingerprint (``fingerprint``) is the session's own vocabulary — repo name, branch, the cwd's path parts, its title,
purpose, recap and standing instructions — minus STOPWORDS, the words that name no work: plain English and the
generic dev talk every session contains ("test", "branch", "commit", "please"). Without that list a prompt made of
filler would match everything, and "branch" would look like evidence.

So a term's weight is 1 over how many candidate sessions have it: a word only one session talks about is that
session's, and says the whole story (weight 1); a word half of them share says half as much. ``check`` answers
"other" only when another session's score is strong in absolute terms, far ahead of this session's, and rests on at
least two terms no other session (including this one) has — distinctive terms, so a shared word can never carry the
call on its own. Everything else answers "ok": a wrong block costs the operator a prompt, a wrong "ok" only costs the
LLM layer a chance to flag it afterwards.

The caller picks the candidates (Store.misplace_candidates): live sessions on any host, this one excluded.
"""
from __future__ import annotations

import re
from typing import Any

PROMPT_CHARS = 4000                                   # a misplaced row keeps this much of the prompt
MIN_TERMS = 4                                         # prompt terms needed before an answer means anything
MIN_SCORE = 2.0                                       # an other session's score to call it at all
MIN_RATIO = 3.0                                       # ... and how far ahead of this session's
MIN_DISTINCT = 2                                      # ... and how many terms only it has
FLOOR = 0.5                                           # what "this session" scores when it scores nothing
MAX_WHY = 4                                           # terms named in ``why``
CANDIDATES = 40                                       # sessions a check may consider
MAX_JOIN = 3                                          # name pieces glued back together (zephyr-dmp-sdk)

_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.\-]{2,}")
_SPLIT = re.compile(r"[-_.]+")
# path parts that name a place, not a piece of work; after a home prefix comes the user's own name
_HOME = ("home", "users", "root")
_GENERIC = {"repos", "repo", "src", "lib", "bin", "worktrees", "work", "workdir", "workspace", "projects", "code",
            "dev", "tmp", "temp", "var", "usr", "local", "opt", "mnt", "media", "data", "documents", "desktop",
            "downloads", "state", "build", "dist", "out", "node_modules", "claude", ".claude", ".local", ".config",
            ".cache"}

# Common English and generic dev words: they say how the operator talks, not which work a prompt is about. A term
# here is never evidence for a session, whichever session's fingerprint it would otherwise have landed in.
STOPWORDS = frozenset("""
a about above across after against almost along already although always among an and another anybody anyone anything
anywhere around as at away back be because been before being below beneath beside between both but by came can
cannot could did do does doing done down due each either else enough even ever every everybody everyone everything
except few for from further get gets give go going got had has have he hence her here hers him his how however i if
in into is it its just keep kept know let lets like list listed little long made make many may me might mine more
most much must my near need next no nor not nothing now of off often on once one only onto or other ours out over
own per please quite rather same say second see seem several shall she should since so some still such take than
that the their them then there these they thing this those though through thus to too under until up upon us use
used using very was way we well were what when where whether which while who whom whose why will with within
without would yes you your yours
add adds added aim aims also branch branches build builds building change changes changed check checks checked
checking code commit commits committed done drift drifts else error errors feature features file files fix fixes wanted wants want bugs bug
fixed go help hub board boards issue issues line lines log logs main make makes master merge merged merges move moves moved new note
notes old part parts patch patches prompt prompts push pushed pushes put recap recaps repo repos run runs running
said session sessions should side state step steps still task tasks test tests then thing think this time times todo
turn turns update updates updated use uses using work works working write writes wrote
""".split())


# A prompt that arrived through ctxswitch isn't the operator typing: ctx delivered it. collector/deliver.py's
# HEADER and agent_header's first line, kept here so the hook and the tracker skip it the same way.
HEADERS = ("Message from the operator (sent with ctxswitch):", "Message from another agent session")
MIN_WORDS = 4                                         # words a prompt needs before a check means anything


def skip(prompt: str) -> bool:
    """True when nothing should be asked about this prompt: a slash command the harness runs itself, a message ctx
    delivered rather than typed, or too few words to say which work it is about."""
    text = (prompt or "").strip()
    return (not text or text.startswith("/") or text.startswith(HEADERS)
            or len(text.split()) < MIN_WORDS)


def where(s: dict[str, Any]) -> str:
    """Where a session runs, the way a person would say it: ``<host> · <repo>@<branch>``."""
    return f"{s.get('host_id') or '?'} · {repo_name(s) or '?'}@{s.get('git_branch') or ''}"


def _clip(text: str | None, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _terms(text: str | None) -> list[str]:
    """The work-naming words of some text, in the order they appear: lowercase, 3+ characters, no stopwords."""
    out: list[str] = []
    for t in _TOKEN.findall((text or "").lower()):
        t = t.rstrip("._-")
        if len(t) >= 3 and t not in STOPWORDS and t not in out:
            out.append(t)
    return out


def _name_terms(text: str | None) -> set[str]:
    """Terms of an identifier (repo name, branch, path part): the whole thing, its pieces, and the pieces glued
    back in twos and threes — an operator types "dmp sdk", "dmp-sdk" and "zephyr-dmp-sdk" of the same repo."""
    out: set[str] = set()
    for chunk in _terms(text):
        out.add(chunk)
        bits = [b for b in _SPLIT.split(chunk) if len(b) >= 3 and b not in STOPWORDS]
        for i, b in enumerate(bits):
            out.add(b)
            for j in range(i + 1, min(len(bits), i + MAX_JOIN)):
                out.add("-".join(bits[i:j + 1]))
    return out


def repo_name(s: dict[str, Any]) -> str:
    """The last path part of the repo: its root on this host, else the remote's path, host and .git dropped."""
    remote = (s.get("repo_remote") or "").strip()
    if remote:
        path = remote.rstrip("/").removesuffix(".git").split("://", 1)[-1].replace(":", "/")
        parts = [p for p in path.split("/") if p]
        return parts[-1] if parts else ""
    root = (s.get("repo_root") or "").rstrip("/")
    return root.rsplit("/", 1)[-1] if root else ""


def _path_parts(cwd: str | None) -> list[str]:
    """A cwd's path parts that name work: no home-dir prefix (and the user name after it), no generic part."""
    parts = [p for p in (cwd or "").replace("\\", "/").split("/") if p and not p.startswith(".")]
    if parts and parts[0].lower() in _HOME:
        parts = parts[2:]
    return [p for p in parts if p.lower() not in _GENERIC]


def fingerprint(s: dict[str, Any]) -> set[str]:
    """What this session's work is called by: repo, branch, cwd, title, purpose, recap, standing instructions —
    minus the words that name no work."""
    out = _name_terms(repo_name(s)) | _name_terms(s.get("git_branch"))
    out |= _name_terms(" ".join(_path_parts(s.get("cwd"))))
    for k in ("title", "purpose", "recap"):
        out |= set(_terms(s.get(k)))
    for text in s.get("instructions") or []:
        out |= set(_terms(text if isinstance(text, str) else str(text)))
    return out


def check(prompt: str, this: dict[str, Any], others: list[dict[str, Any]]) -> dict[str, Any]:
    """{"verdict": "ok"}, or "other" naming the session the prompt reads like it belongs to."""
    terms = _terms(prompt)
    if len(terms) < MIN_TERMS:
        return {"verdict": "ok"}
    fps = [fingerprint(this)] + [fingerprint(s) for s in others]
    have = {t: sum(t in f for f in fps) for t in terms}

    def score(f: set[str]) -> float:
        return sum(1.0 / have[t] for t in terms if t in f)

    best, pick = -1.0, None
    for i, f in enumerate(fps[1:]):
        sc = score(f)
        if pick is None or sc > best or (sc == best and (others[i].get("last_activity") or "") >
                                         (others[pick].get("last_activity") or "")):
            best, pick = sc, i
    if pick is None:
        return {"verdict": "ok"}
    target, fp = others[pick], fps[pick + 1]
    if best < MIN_SCORE or best < MIN_RATIO * max(score(fps[0]), FLOOR):
        return {"verdict": "ok"}
    distinct = [t for t in terms if t in fp and t not in fps[0]
                and all(t not in fps[j + 1] for j in range(len(others)) if j != pick)]
    if len(distinct) < MIN_DISTINCT:
        return {"verdict": "ok"}
    return {"verdict": "other", "target": target, "target_key": target.get("key") or "",
            "target_title": _clip(target.get("title") or target.get("key") or "", 200),
            "why": "mentions " + ", ".join(distinct[:MAX_WHY])}
