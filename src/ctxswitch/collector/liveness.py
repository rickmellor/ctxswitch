"""Which sessions are alive right now, and since when.

Claude Code writes ~/.claude/sessions/<pid>.json for every interactive/-p session (status busy|idle,
statusUpdatedAt, messagingSocketPath, procStart). Files outlive crashed sessions, so an entry counts as live only
when the pid exists AND its /proc start time matches procStart (a reused pid fails the second check).
Windows-native Claude (read from WSL) writes the same registry; its pids are Windows pids, checked with
tasklist.exe (no start time available, so a reused pid would read as live until the next check).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ctxswitch.common.schema import LIVE, Liveness, session_key

log = logging.getLogger("ctxswitch.collector")


def _iso_ms(ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return None


def proc_start(pid: int, proc: str = "/proc") -> str | None:
    """Field 22 of /proc/<pid>/stat (clock ticks since boot), as the registry's ``procStart`` string."""
    try:
        with open(f"{proc}/{pid}/stat") as f:
            stat = f.read()
    except OSError:
        return None
    # comm may contain spaces/parens: split after the last ')'
    rest = stat.rsplit(")", 1)[1].split()
    return rest[19] if len(rest) > 19 else None


def read_claude_registry(sessions_dir: Path, host_id: str, proc: str = "/proc") -> dict[str, Liveness]:
    """Live Claude sessions keyed by session_key. Stale registry files are ignored (not deleted)."""
    return _claude_registry(sessions_dir, host_id, "claude", lambda pid, d: proc_start(pid, proc) == str(d.get("procStart")))


def read_claude_win_registry(sessions_dir: Path, host_id: str, pids=None) -> dict[str, Liveness] | None:
    """Live Windows-native Claude sessions; None when liveness can't be known (tasklist.exe not reachable).
    ``pids`` is the Windows pid set, or a callable producing it (only called when there are registry files)."""
    files = list(sessions_dir.glob("*.json")) if sessions_dir.is_dir() else []
    if not files:
        return {}
    alive = pids() if callable(pids) else pids
    if alive is None:
        return None
    return _claude_registry(sessions_dir, host_id, "claude-win", lambda pid, d: pid in alive)


def _user_name(d: dict) -> str | None:
    """The registry's session name when the user chose it (/rename, --name). A resumed session can carry a name
    the transcript has no custom-title for; "derived"/"auto" names are generated and the ai-title covers them."""
    n = d.get("name")
    if not isinstance(n, str) or not n.strip() or d.get("nameSource") in ("derived", "auto"):
        return None
    return n.strip()


def _tmux_at(v: Any) -> str | None:
    """A registry entry's "tmux" field as a tmux_at: "<session>:<window_id>.<pane_id>", "" for anything else."""
    return v if isinstance(v, str) and ":" in v and "." in v else None


def _claude_registry(sessions_dir: Path, host_id: str, harness: str, alive) -> dict[str, Liveness]:
    out: dict[str, Liveness] = {}
    if not sessions_dir.is_dir():
        return out
    entries = []
    for p in sessions_dir.glob("*.json"):
        try:
            with open(p) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        pid, sid = d.get("pid"), d.get("sessionId")
        if not isinstance(pid, int) or not isinstance(sid, str):
            continue
        if alive(pid, d):
            entries.append(d)
    # A window can park its conversation into a background job (the window's entry names it parkedJobId, the job's
    # entry jobId): the job is the conversation now, forked under a new session id, and the window only shows it.
    # Report the job, as shown by the window's process, and not the window's frozen session as a second live one.
    jobs = {d["jobId"]: d for d in entries if isinstance(d.get("jobId"), str) and d["jobId"]}
    via: dict[int, int] = {}
    at: dict[int, str] = {}
    for d in entries:
        job = jobs.get(d.get("parkedJobId")) if isinstance(d.get("parkedJobId"), str) else None
        if job is not None and job is not d:
            via[job["pid"]] = d["pid"]
            at[job["pid"]] = _tmux_at(d.get("tmux"))      # the job shows in the window's pane, not in one of its own
    parked = {id(d) for d in entries if isinstance(d.get("parkedJobId"), str) and d["parkedJobId"] in jobs
              and jobs[d["parkedJobId"]] is not d}
    for d in entries:
        if id(d) in parked:
            continue
        pid, sid = d["pid"], d["sessionId"]
        status = d.get("status") if d.get("status") in LIVE else "unknown"
        out[session_key(host_id, harness, sid)] = Liveness(
            key=session_key(host_id, harness, sid), status=status,
            status_since=_iso_ms(d.get("statusUpdatedAt") or d.get("updatedAt")), pid=pid,
            socket=d.get("messagingSocketPath") if isinstance(d.get("messagingSocketPath"), str) else None,
            name=_user_name(d), via_pid=via.get(pid),
            tmux_at=at.get(pid) or _tmux_at(d.get("tmux")))
    return out


def tmux_panes(run=subprocess.run) -> list[tuple[str, str, str, int]]:
    """(session_name, window_id, pane_id, pane_pid) for every pane of the tmux server on THIS host. One list-panes
    call per poll; no tmux binary, no server or any other failure reads as no panes (a host without tmux is normal)."""
    try:
        p = run(["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{window_id}\t#{pane_id}\t#{pane_pid}"],
                capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return []
    if getattr(p, "returncode", 1):
        return []
    out = []
    for line in (p.stdout or "").splitlines():
        f = line.split("\t")
        if len(f) < 4:
            continue
        try:
            out.append((f[0], f[1], f[2], int(f[3])))
        except ValueError:
            continue
    return out


def _ppid(pid: int, proc: str = "/proc") -> int | None:
    """Field 4 of /proc/<pid>/stat: its parent's pid."""
    try:
        with open(f"{proc}/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def fill_tmux_at(items: list[Liveness], panes: list[tuple[str, str, str, int]], proc: str = "/proc") -> None:
    """Set tmux_at on every item that has a pid and none yet: the pane on this host whose pane_pid is that pid or an
    ancestor of it (the session runs inside the pane's shell). Same walk as client/tmux.pane_for_pid."""
    if not panes:
        return
    # a pane in a grouped session is listed once per session: its own (ctx) beats a board's temporary view of it
    # (ctxview-…), which goes away when the viewer detaches
    by_pid: dict[int, str] = {}
    for p in sorted(panes, key=lambda p: p[0].startswith("ctxview-")):
        by_pid.setdefault(p[3], f"{p[0]}:{p[1]}.{p[2]}")
    for it in items:
        if it.tmux_at or not it.pid:
            continue
        cur: int | None = it.pid
        for _ in range(12):
            if cur is None or cur <= 1:
                break
            if cur in by_pid:
                it.tmux_at = by_pid[cur]
                break
            cur = _ppid(cur, proc)


# -- herdr-core -----------------------------------------------------------------------------------------------------
# herdr-core (``hc``) runs the same terminal on this host; a terminal is an address ("t7") on this host's core, and
# a board on another host reaches it the same way it reaches a tmux pane: ssh in, use that address. Its agent
# state (OSC 7501: working / idle / blocked) is richer than a registry file's busy/idle, so it also feeds
# liveness (collector/main.py's hc_loop): terminals with a reporting agent but no local session of their own are
# live sessions in their own right (harness "herdr" — an agent that has no transcript here, Codex, Cursor, …).

HC_CTX_KEY = "ctx.key"
HC_HOME_DIRS = ("bin", ".local/bin", ".herdr/bin")   # where hc lives when a systemd --user PATH leaves it out


def find_tool(name: str, which=shutil.which) -> str | None:
    """A tool on PATH, else in the operator's own bin dirs: a systemd --user unit's PATH has no ~/.local/bin, which is
    where hc, claude, pi and ctx all live on these hosts — without this the collector saw none of them."""
    found = which(name)
    if found:
        return found
    try:
        return which(name, path=os.pathsep.join(str(Path.home() / d) for d in HC_HOME_DIRS))
    except TypeError:                                  # a test's fake `which` that takes only the name
        return None


def hc_bin(which=shutil.which) -> str | None:
    """The hc binary, wherever the operator keeps it (see find_tool)."""
    return find_tool("hc", which)
HC_STATE_MAP = {"working": "busy", "blocked": "waiting", "idle": "idle", "done": "idle", "error": "unknown"}
# an agent ctx already has its own way to see: a registry file (Claude, pi) or its own rcfile (ctx shell). herdr
# never reports one of those as a session of its own — if no registry entry maps to that terminal, ctx's own
# source says it isn't live, and a session with no transcript and no registry entry would be a phantom.
HC_LOCAL_APPS = ("claude", "claude-code", "claude-win", "pi", "agy", "ctx-shell", "shell")


def _hc_json(argv: list[str], run=subprocess.run) -> Any:
    try:
        p = run(argv, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(p, "returncode", 1):
        return None
    try:
        return json.loads(p.stdout or "")
    except (ValueError, TypeError):
        return None


_hc_gets: dict[str, tuple] = {}        # address -> ((turn, status) the record was fetched at, the record)


def hc_terminals(run=subprocess.run) -> list[dict]:
    """Every LIVE terminal on this host's core as its ``hc get`` record, for the poll that calls it — at most one
    ``hc get`` per terminal between the polls that ask: the get is skipped while its (turn, status) is what it was
    when the record was fetched. ``hc ls`` carries no pid and no labels, and both are needed to place a session.
    No hc binary, no server, or any other failure reads as no terminals (a host without herdr-core is normal);
    nothing here may raise into the liveness poll."""
    hc = hc_bin() or "hc"                          # the poll only gets here when hc_bin() found one (main.py)
    ls = _hc_json([hc, "ls", "--json"], run)
    terms = ls.get("terminals") if isinstance(ls, dict) else None
    if not isinstance(terms, list):
        _hc_gets.clear()                          # no answer at all: the terminals are gone, so are their records
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for t in terms:
        if not isinstance(t, dict) or t.get("state") != "live":
            continue
        addr = t.get("address")
        # a terminal on a machine this core is connected to ("gpu-host/t3") belongs to that host's own collector
        if not isinstance(addr, str) or not addr or "/" in addr:
            continue
        seen.add(addr)
        key = (t.get("turn"), t.get("status"))
        hit = _hc_gets.get(addr)
        if hit and hit[0] == key:
            out.append(hit[1])
            continue
        got = _hc_json([hc, "get", addr, "--json"], run)
        rec = got if isinstance(got, dict) else dict(t)   # a get that failed still leaves the ls row to go on
        rec.setdefault("address", addr)
        _hc_gets[addr] = (key, rec)
        out.append(rec)
    for addr in [a for a in _hc_gets if a not in seen]:
        del _hc_gets[addr]
    return out


def _hc_status(v: Any) -> dict | None:
    """A status as the JSON carries it: an object from ``hc get`` and the events stream, a bare state string from
    ``hc ls``. Anything else (null: never reported) has no state."""
    if isinstance(v, dict):
        return v
    return {"state": v} if isinstance(v, str) and v else None


def hc_status_of(status: Any) -> tuple[str | None, dict | None]:
    """A terminal's herdr status -> (ctx status, ask or None). working→busy, blocked→waiting (with the dialog's
    kind and text), idle|done→idle, error→unknown; no status (never reported) has no agent state of its own."""
    if not isinstance(status, dict):
        return None, None
    st = status.get("state")
    if st not in HC_STATE_MAP:
        return None, None
    ask = None
    if st == "blocked" and status.get("msg"):
        kind = status.get("kind")
        ask = {"kind": kind if kind in ("question", "permission") else "plain",
               "text": str(status["msg"]).strip()[:600]}
    return HC_STATE_MAP[st], ask


def fill_hc_at(items: list[Liveness], records: list[dict], proc: str = "/proc") -> None:
    """Set hc_at on every live item that has a pid and none yet: the terminal whose pid (or its foreground pid)
    is that pid or an ancestor of it (the session runs inside the terminal, like a tmux pane — same walk as
    fill_tmux_at), or whose ctx.key label is the item's key (a hand-mapped terminal)."""
    if not records:
        return
    by_pid: dict[int, str] = {}
    for r in records:
        addr = r.get("address")
        if not isinstance(addr, str) or not addr:
            continue
        fg = r.get("foreground")
        fg_pid = fg.get("pid") if isinstance(fg, dict) else None
        if isinstance(fg_pid, int):
            by_pid.setdefault(fg_pid, addr)
        pid = r.get("pid")
        if isinstance(pid, int):
            by_pid.setdefault(pid, addr)
    by_label = {r["labels"][HC_CTX_KEY]: r["address"] for r in records
                if isinstance(r.get("address"), str) and isinstance(r.get("labels"), dict)
                and r["labels"].get(HC_CTX_KEY)}
    for it in items:
        if it.hc_at or not it.pid:
            continue
        addr = by_label.get(it.key)
        if not addr:
            cur: int | None = it.pid
            for _ in range(12):
                if cur is None or cur <= 1:
                    break
                if cur in by_pid:
                    addr = by_pid[cur]
                    break
                cur = _ppid(cur, proc)
        if addr:
            it.hc_at = addr


_hc_labelled: set[str] = set()         # addresses this process has put its ctx.key on


def label_sessions(items: list[Liveness], records: list[dict], run=subprocess.run) -> None:
    """Give every terminal this poll mapped to a session its ctx.key label, so a later poll can place it by label
    alone (the pid walk can fail on a restarted core, and a headless agent's pid is its own program's). One
    ``hc label`` per terminal — remembered for this process — and none at all once the label is on the record."""
    for it in items:
        addr = it.hc_at
        if not addr or addr in _hc_labelled:
            continue
        rec = next((r for r in records if r.get("address") == addr), None)
        if rec is None:
            continue
        labels = rec.get("labels") if isinstance(rec.get("labels"), dict) else {}
        if labels.get(HC_CTX_KEY) == it.key:
            _hc_labelled.add(addr)                 # already labelled: nothing to do, ever again
            continue
        if labels.get(HC_CTX_KEY):
            continue                               # it names another session: that mapping isn't ours to overwrite
        p = run([hc_bin() or "hc", "label", addr, f"{HC_CTX_KEY}={it.key}"], capture_output=True, text=True, timeout=3)
        if not getattr(p, "returncode", 1):
            _hc_labelled.add(addr)


class HcLoop:
    """This host's core as a liveness source: one long-lived ``hc events --json`` read line by line, keeping
    ``state`` = {address: {state, kind, msg, app, turn, ts, title, pid, reported}} from its snapshot and its
    status/turn/created/finished/removed lines. merge() feeds that into a poll: a blocked dialog on a terminal the
    registries don't know about is a live session of its own; on one they do know it can only UPGRADE its status
    (idle→waiting) and attach its ask. No hc on PATH: no reader, and merge() has nothing to say. The stream dies
    and restarts with a 5→60 s backoff; a non-object line ("missed") means the core restarted — its fresh
    snapshot is the next line anyway, so the reader just keeps reading."""
    HC_ARGV = ["hc", "events", "--json"]

    def __init__(self, host_id: str, popen=subprocess.Popen, which=shutil.which, sleep=time.sleep):
        self.host_id, self.popen, self.which, self.sleep = host_id, popen, which, sleep
        self.state: dict[str, dict] = {}
        self._proc = None
        self._stop = False

    def stop(self) -> None:
        self._stop = True
        if self._proc is not None:
            self._proc.kill()
            try:
                self._proc.wait(timeout=2)
            except Exception:                       # noqa: BLE001 - it's a dying reader, nothing to learn from it
                pass
            self._proc = None

    def _apply(self, d: dict, now: float) -> None:
        """One snapshot terminal or event line: fold its fields into state[address]. ts is when THIS collector saw
        the change (the stream carries no timestamps), so status_since is the same kind of stamp a registry gives."""
        addr = d.get("terminal") or d.get("address")
        if not isinstance(addr, str) or not addr:
            return
        typ = d.get("type")
        if typ in ("finished", "removed", "lost"):
            self.state.pop(addr, None)
            return
        s = self.state.get(addr)
        if s is None or typ in ("created", "restarted"):
            s = self.state[addr] = {"state": None, "kind": None, "msg": None, "app": None,
                                    "turn": None, "ts": None, "title": "", "pid": None, "reported": False}
        if typ is None:                             # a snapshot or "created" terminal: the whole record
            st = _hc_status(d.get("status"))
            s.update(state=st.get("state") if st else None, kind=st.get("kind") if st else None,
                     msg=st.get("msg") if st else None,
                     app=(st.get("app") if st else None) or d.get("agent"),
                     turn=d.get("turn"), title=d.get("title") or d.get("name") or "",
                     pid=d.get("pid") if isinstance(d.get("pid"), int) else None,
                     reported=bool(d.get("reported")), ts=_iso_ms(now * 1000))
            return
        data = d.get("data") if isinstance(d.get("data"), dict) else {}
        if typ == "status":
            st = _hc_status(data.get("status")) or {"state": None}   # null: the program stopped reporting
            s.update(state=st.get("state"), kind=st.get("kind"), msg=st.get("msg"),
                     app=st.get("app") or s.get("app"), reported=True, ts=_iso_ms(now * 1000))
        elif typ == "turn":
            s.update(turn=data.get("turn"), ts=_iso_ms(now * 1000))

    def _read_lines(self, p, ready) -> None:
        for line in p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and "params" in d:      # an event line: {"jsonrpc","method":"event","params":{…}}
                d = d.get("params") or {}
            if not isinstance(d, dict):
                continue                                    # "missed": the core restarted; its snapshot is next
            if "terminals" in d:                            # the snapshot: one whole record per terminal
                for t in d.get("terminals") or []:
                    if isinstance(t, dict):
                        self._apply(t, time.time())
                continue
            if d.get("type") or d.get("terminal") is not None:
                self._apply(d, time.time())
        if ready is not None:
            ready.set()

    def stream(self, ready=None) -> None:
        """Blocking reader: stream ``hc events --json`` until it dies, then restart after a 5→60 s backoff.
        ``ready``, when given, is set whenever the stream ends (tests wait on it between their lines)."""
        backoff = 5
        while not self._stop:
            hc = hc_bin(self.which)
            if hc is None:                          # no herdr-core on this host: nothing to watch
                return
            p = None
            try:
                p = self.popen([hc, *self.HC_ARGV[1:]], stdout=subprocess.PIPE, text=True)
                self._proc = p
                self._read_lines(p, ready)
            except Exception:                       # noqa: BLE001 - the poll's reader may not take the poll down
                log.exception("herdr events")
            finally:
                self._proc = None
                if p is not None:
                    p.kill()
                    try:
                        p.wait(timeout=2)
                    except Exception:               # noqa: BLE001
                        pass
            if ready is not None:
                ready.set()
            if self._stop:
                return
            self.sleep(backoff)
            backoff = min(backoff * 2, 60)

    @staticmethod
    def _tell(item: Liveness, st: dict) -> None:
        """What herdr adds to a session ctx already has: the dialog its terminal is holding, and a blocked status
        for one its own source calls idle. Nothing else — a registry's busy beats a core's idle."""
        _, ask = hc_status_of({"state": st.get("state"), "kind": st.get("kind"), "msg": st.get("msg")})
        item.hc_ask = {"kind": ask["kind"], "text": ask["text"]} if ask else None
        if st.get("state") == "blocked" and item.status == "idle":
            item.status, item.status_since = "waiting", st.get("ts") or item.status_since

    def merge(self, live: dict, records: list[dict] | None = None) -> None:
        """Feed herdr's state into this host's liveness (call after fill_hc_at, with the poll's records).

        The events stream owns the sessions ctx has no other way to see: a terminal reporting a status no session
        maps to is a session of its own, harness "herdr" — an agent with no transcript here (Codex, Cursor, …). It
        is also what knows one is gone, since finished/removed drops it: a poll whose ``hc ls`` briefly failed
        still reports what the stream holds rather than ending every herdr session. This poll's own records can
        only speak about a session ctx already knows, which is where their fresher status and ask land."""
        placed: set[str] = set()
        for addr, st in list(self.state.items()):   # the reader thread may add one while this poll runs
            if "/" in addr:
                continue                                # "gpu-host/t3": that host's own collector reports it
            item = next((it for it in live.values() if it.hc_at == addr), None)
            if item is not None:
                self._tell(item, st)
                placed.add(addr)
                continue
            status, ask = hc_status_of({"state": st.get("state"), "kind": st.get("kind"), "msg": st.get("msg")})
            key = session_key(self.host_id, "herdr", addr)
            if key in live or status is None or (st.get("app") or "").lower() in HC_LOCAL_APPS:
                continue     # reporting nothing of its own, or an agent ctx sees through its own source already
            live[key] = Liveness(key=key, status=status, status_since=st.get("ts"), pid=st.get("pid"), hc_at=addr,
                                 name=(st.get("title") or None) or st.get("app"), hc_ask=ask)
            placed.add(addr)
        for r in records or []:
            addr = r.get("address")
            if not isinstance(addr, str) or not addr or addr in placed or "/" in addr or r.get("state") != "live":
                continue
            item = next((it for it in live.values() if it.hc_at == addr), None)
            if item is not None:
                st = _hc_status(r.get("status"))
                self._tell(item, {"state": st.get("state") if st else None, "kind": st.get("kind") if st else None,
                                  "msg": st.get("msg") if st else None, "ts": None})


def read_agy_presence(conversations_dir: Path, host_id: str, proc: str = "/proc") -> dict[str, Liveness]:
    """Antigravity holds an flock on ../presence/<conversation id>.lock while a conversation is open. Locked = live.
    busy/idle comes from conversation_summaries.db (status BUSY/RUNNING, not_fully_idle); the pid is the agy process
    holding the lock (found through /proc, so the board can map it to its pane)."""
    import fcntl
    from ctxswitch.collector.adapters.agy import read_summary
    out: dict[str, Liveness] = {}
    presence = conversations_dir.parent / "presence"
    if not presence.is_dir():
        return out
    holders: dict[str, int] | None = None
    for lock in presence.glob("*.lock"):
        try:
            fd = os.open(lock, os.O_RDWR)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                continue                                  # nobody holds it: not running
            except OSError:
                pass                                      # held: a live conversation
        finally:
            os.close(fd)
        sid = lock.stem
        if holders is None:
            holders = _agy_lock_holders(proc)
        summ = read_summary(str(conversations_dir.parent / "conversation_summaries.db"), sid)
        st = summ.get("status", "")
        status = "busy" if summ.get("busy") or st.endswith(("BUSY", "RUNNING")) else "idle" if st.endswith("IDLE") else "unknown"
        k = session_key(host_id, "agy", sid)
        out[k] = Liveness(key=k, status=status, status_since=_iso_sql(summ.get("last_modified")), pid=holders.get(sid))
    return out


def _agy_lock_holders(proc: str = "/proc") -> dict[str, int]:
    """{conversation id: pid} for every agy process with a presence lock open."""
    out: dict[str, int] = {}
    try:
        pids = [int(p) for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return out
    for pid in pids:
        try:
            with open(f"{proc}/{pid}/comm") as f:
                if f.read().strip() != "agy":
                    continue
            for fd in os.listdir(f"{proc}/{pid}/fd"):
                target = os.readlink(f"{proc}/{pid}/fd/{fd}")
                if "/presence/" in target and target.endswith(".lock"):
                    out.setdefault(target.rsplit("/", 1)[1][:-5], pid)
        except OSError:
            continue
    return out


def _iso_sql(v: Any) -> str | None:
    """'2026-10-30 18:29:28.131084492+00:00' (sqlite datetime) -> ISO with Z."""
    if not isinstance(v, str) or len(v) < 19:
        return None
    return v[:19].replace(" ", "T") + "Z"


def runtime_dir() -> Path:
    """Where the pi extension registers (same order as pi_ext/ctxswitch.ts)."""
    if os.environ.get("CTXSWITCH_RUNTIME"):
        return Path(os.environ["CTXSWITCH_RUNTIME"])
    if os.environ.get("XDG_RUNTIME_DIR"):
        return Path(os.environ["XDG_RUNTIME_DIR"])
    run = Path(f"/run/user/{os.getuid()}")
    return run if run.exists() else Path.home() / ".local" / "state" / "ctxswitch" / "run"


def read_pi_registry(runtime_dir: Path, host_id: str, proc: str = "/proc") -> dict[str, Liveness]:
    """pi sessions register through the ctxswitch extension as <runtime>/ctxswitch/pi-<pid>.json. A crashed pi
    leaves its file behind: live only if the pid exists and, when the extension recorded it, its start matches."""
    out: dict[str, Liveness] = {}
    d = runtime_dir / "ctxswitch"
    if not d.is_dir():
        return out
    for p in d.glob("pi-*.json"):
        try:
            with open(p) as f:
                e = json.load(f)
        except (OSError, ValueError):
            continue
        pid, sid = e.get("pid"), e.get("sessionId")
        if not isinstance(pid, int) or not isinstance(sid, str) or not os.path.exists(f"{proc}/{pid}"):
            continue
        if e.get("procStart") is not None and proc_start(pid, proc) != str(e["procStart"]):
            continue
        k = session_key(host_id, "pi", sid)
        out[k] = Liveness(key=k, status=e.get("status") if e.get("status") in ("busy", "idle") else "unknown", status_since=e.get("statusSince"), pid=pid,
                          socket=e.get("socket"))
    return out


def read_shell_registry(runtime_dir: Path, host_id: str, proc: str = "/proc") -> dict[str, Liveness]:
    """A ``ctx shell`` registers as <runtime>/ctxswitch/shell-<pid>.json while it runs (busy while a command is
    running, idle at its prompt). An exited shell deletes its file; one that died leaves it behind, so it counts as
    live only when the pid exists and, when recorded, its start time still matches."""
    out: dict[str, Liveness] = {}
    d = runtime_dir / "ctxswitch"
    if not d.is_dir():
        return out
    for p in d.glob("shell-*.json"):
        try:
            with open(p) as f:
                e = json.load(f)
        except (OSError, ValueError):
            continue
        pid, sid = e.get("pid"), e.get("sessionId")
        if not isinstance(pid, int) or not isinstance(sid, str) or not os.path.exists(f"{proc}/{pid}"):
            continue
        if e.get("procStart") is not None and proc_start(pid, proc) != str(e["procStart"]):
            continue
        k = session_key(host_id, "shell", sid)
        out[k] = Liveness(key=k, status=e.get("status") if e.get("status") in ("busy", "idle") else "unknown",
                          status_since=e.get("statusSince"), pid=pid)
    return out
