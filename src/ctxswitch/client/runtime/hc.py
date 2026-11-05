"""The herdr-core (hc) driver: sessions live in hc terminals, found by the ``ctx.key`` label we put on them.

Calls go as JSON-RPC 2.0 over the core's unix socket — plain mode: connect, write one JSON object on one line, read
one line back. The ``hc`` CLI answers instead when the socket isn't there (no server running, a session this client
wasn't pointed at), so one driver works either way.

A row's terminal is the live one labelled ``ctx.key=<row key>``; on this host also the one whose pid is the row's pid
or one of its ancestors (a session started by hand, which hc knows but ctx hasn't labelled yet) — found that way, it
gets the label, so the next look is a lookup.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import uuid
from pathlib import Path

from ctxswitch.client import tmux
from ctxswitch.common.schema import LIVE

BIN = os.environ.get("CTXSWITCH_HC", "hc")
KEY_LABEL = "ctx.key"
HOST_LABEL = "ctx.host"
FOCUS_APP = "herdr-app"                   # the app that fronts the herdr window, when this host has one
PROC = "/proc"


class HcError(RuntimeError):
    """The core isn't reachable, or answered with an error."""


def socket_path() -> str:
    """Where this core listens: HC_SOCKET, else $XDG_RUNTIME_DIR/herdr-core/<session>.sock, else /run/user/<uid>/."""
    p = os.environ.get("HC_SOCKET")
    if p:
        return p
    session = os.environ.get("HC_SESSION") or "default"
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return str(Path(base) / "herdr-core" / f"{session}.sock")


def cli_argv(method: str, params: dict) -> list[str] | None:
    """The ``hc`` command that says the same thing as this call, or None when only the socket can. ``terminal.run``
    goes as argv, which the CLI can only express as one shell line, so it has no fallback."""
    if method == "terminal.list":
        argv = ["ls", "--json"]
        if params.get("all"):
            argv.append("--all")
        return argv + _label_args(params.get("labels"))
    if method == "terminal.get":
        return ["get", str(params.get("terminal", "")), "--json"]
    if method == "terminal.kill":
        return ["kill", str(params.get("terminal", "")), "--json"]
    if method == "terminal.label":
        return ["label", str(params.get("terminal", ""))] + _kv(params.get("labels"))
    if method == "machine.list":
        return ["machine", "ls", "--json"]
    if method == "terminal.run":
        argv = ["run", "--bg", "--json"]
        if params.get("name"):
            argv += ["--name", str(params["name"])]
        if params.get("cwd"):
            argv += ["--cwd", str(params["cwd"])]
        if params.get("on"):
            argv += ["--on", str(params["on"])]
        argv += _label_args(params.get("labels"))
        if params.get("keep"):
            argv.append("--keep")
        return argv + ["--"] + [str(a) for a in params.get("argv") or []]
    return None


def _label_args(labels: dict | None) -> list[str]:
    return sum((["--label", f"{k}={v}"] for k, v in (labels or {}).items()), [])


def _kv(labels: dict | None) -> list[str]:
    return [f"{k}={v}" for k, v in (labels or {}).items()]


class _Socket:
    """One call per connection: the core answers a request on the connection that made it, so nothing is shared."""

    def __init__(self, path: str, timeout: float = 5.0):
        self.path, self.timeout = path, timeout
        self._n = 0

    def call(self, method: str, params: dict, _id: int | None = None) -> dict:
        self._n += 1
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            s.connect(self.path)
        except OSError as e:
            raise HcError(f"herdr-core: {e}") from e
        try:
            f = s.makefile("rw")
            f.write(json.dumps({"jsonrpc": "2.0", "id": _id or self._n, "method": method, "params": params}) + "\n")
            f.flush()
            line = f.readline()
        finally:
            s.close()
        if not line:
            raise HcError("herdr-core closed the connection")
        try:
            msg = json.loads(line)
        except ValueError as e:
            raise HcError(f"herdr-core said: {line[:120]!r}") from e
        if "error" in msg:
            raise HcError(str((msg["error"] or {}).get("message") or "herdr-core error"))
        return msg.get("result") or {}


class _Cli:
    """The same calls through the ``hc`` binary, for when there's no socket to talk to."""

    def __init__(self, env: dict | None = None, timeout: float = 15.0):
        self.env, self.timeout = env, timeout

    def call(self, method: str, params: dict) -> dict:
        argv = cli_argv(method, params)
        if not argv:
            raise HcError(f"the hc CLI cannot do {method}")
        try:
            r = subprocess.run([BIN, *argv], capture_output=True, text=True, env=self.env or None, timeout=self.timeout)
        except (OSError, subprocess.SubprocessError) as e:         # no hc installed, or it outlived the call
            raise HcError(f"herdr-core: {e}") from e
        if r.returncode != 0:
            raise HcError((r.stderr or r.stdout or "").strip()[:200] or f"hc {' '.join(argv)} failed")
        try:
            return json.loads(r.stdout or "{}")
        except ValueError as e:
            raise HcError(f"hc {' '.join(argv[:2])} said: {r.stdout[:120]!r}") from e


class HcRuntime:
    """Sessions live in hc terminals on this core — and on the machines this core has added.

    ``machines`` maps a ctx host id to the name this core calls that machine by; hosts not in it are named by their
    host id, which is the usual setup (a host id is a hostname, hc's default machine name).
    """

    name = "hc"

    def __init__(self, sock: str | None = None, env: dict | None = None, machines: dict[str, str] | None = None,
                 proc: str = PROC, focus_app: str | None = None, timeout: float = 5.0):
        self.sock = sock
        self.env = env                                  # extra environment for the CLI fallback (a private core in tests)
        self.machines = machines if machines is not None else {}
        self.proc = proc
        self.timeout = timeout
        self.focus_app = FOCUS_APP if focus_app is None else focus_app

    # -- plumbing -------------------------------------------------------

    def path(self) -> str:
        return self.sock or socket_path()

    def available(self) -> bool:
        """A core answers on this socket; with no socket file, hc itself has to be installed and answering."""
        p = self.path()
        if os.path.exists(p):
            try:
                _Socket(p, self.timeout).call("machine.list", {})
                return True
            except HcError:                                      # a socket file left by a dead core means no core
                return False
        try:
            r = subprocess.run([BIN, "--version"], capture_output=True, text=True, env=self.env or None)
        except OSError:
            return False
        return r.returncode == 0

    def _send(self, method: str, params: dict, path: str) -> dict:
        """One call over the socket; the CLI answers instead when the socket can't be reached at all."""
        try:
            return _Socket(path, self.timeout).call(method, params)
        except HcError:
            if not cli_argv(method, params):
                raise
            return _Cli(self.env).call(method, params)

    def rpc(self, method: str, params: dict | None = None, *, env: dict | None = None) -> dict:
        """env names a particular core's state/runtime dirs for the CLI fallback; the socket path already says which
        core this is, so it only matters when there's no socket to talk to."""
        if env is None:
            return self._send(method, params or {}, self.path())
        return _Cli({**os.environ, **env}).call(method, params or {})

    # -- terminals and machines -----------------------------------------

    def terminals(self, all_: bool = False) -> list[dict]:
        """Every terminal this core knows, each with the name of the machine it runs on in ``machine`` ("" here)."""
        out: list[dict] = []
        for m in self.machines_here():
            if (m.get("state") or "") != "connected":
                continue
            name = m.get("name") or ""
            try:
                res = self.rpc("terminal.list", {"all": all_, "on": name})
            except HcError:
                continue                                          # a machine that dropped mid-list isn't an error here
            for t in res.get("terminals") or []:
                addr = t.get("address") or ""
                out.append({**t, "machine": name,
                            "address": addr if addr.startswith(f"{name}/") else f"{name}/{addr}"})
        out += [{**t, "machine": ""} for t in self.rpc("terminal.list", {"all": all_}).get("terminals") or []]
        return out

    def machines_here(self) -> list[dict]:
        try:
            return self.rpc("machine.list").get("machines") or []
        except HcError:
            return []

    def machine_name(self, host_id: str | None, ssh: dict | None = None) -> str:
        """What this core calls the machine that is ``host_id``: the mapping's name, else the host id itself."""
        return self.machines.get(host_id or "", host_id or "")

    def _has_machine(self, host_id: str | None, ssh: dict | None = None, ms: list[dict] | None = None) -> bool:
        """Whether this core has a machine for that host — by name, or by the ssh target its [ssh] entry gives it."""
        want = self.machine_name(host_id, ssh)
        target = tmux.ssh_target(host_id, ssh or {})
        return any(m.get("name") == want or (target and m.get("target") == target)
                   for m in (self.machines_here() if ms is None else ms))

    def terminal_for(self, row: dict, ts: list[dict] | None = None) -> dict | None:
        """The live terminal running ``row``: labelled with its key, else (this host only) running its pid or one of
        its ancestors — which we then label, so ctx.key is the only lookup next time."""
        key, host = row.get("key", ""), row.get("host_id")
        ts = self.terminals() if ts is None else ts
        for t in ts:
            if t.get("state") == "live" and (t.get("labels") or {}).get(KEY_LABEL) == key:
                return t
        if tmux._is_remote(row, tmux._this_host()) or not row.get("pid"):
            return None                                           # a pid from another host means nothing here
        pids = self._ancestors(int(row["pid"]))
        for t in ts:
            if t.get("state") == "live" and not t.get("machine") and t.get("pid") in pids:
                self._label(t.get("address") or "", key, host)
                return {**t, "labels": {**(t.get("labels") or {}), KEY_LABEL: key}}
        return None

    def _ancestors(self, pid: int) -> set[int]:
        """``pid`` and the pids above it, to where the process tree stops."""
        out: set[int] = set()
        cur: int | None = pid
        for _ in range(12):
            if cur is None or cur <= 1 or cur in out:
                break
            out.add(cur)
            cur = tmux._ppid(cur, self.proc)
        return out

    def _label(self, addr: str, key: str, host: str | None) -> None:
        try:
            self.rpc("terminal.label", {"terminal": addr, "labels": {KEY_LABEL: key, HOST_LABEL: host or ""}})
        except HcError:
            pass                                                  # a terminal that won't be labelled is still found

    # -- Runtime --------------------------------------------------------

    def reach(self, rows: list[dict], this_host: str, ssh: dict) -> dict[str, str]:
        ssh = tmux._ssh() if ssh is None else ssh
        this_host = this_host or tmux._this_host()
        ts, ms = self.terminals(), self.machines_here()
        return {row["key"]: self._reach_row(row, ts, ms, this_host, ssh) for row in rows}

    def _reach_row(self, row: dict, ts: list[dict], ms: list[dict], this_host: str, ssh: dict) -> str:
        t = self.terminal_for(row, ts)
        if not tmux._is_remote(row, this_host):
            if t is not None:
                return "pane"                                     # hc can always show one of its own terminals
            if row.get("live_status") in LIVE and row.get("pid"):
                return "foreign"                                  # running here, in a terminal hc doesn't hold
            return "resume" if tmux.resume_argv(row) else "none"
        if t is not None and self._machine_matches(t, row, ssh, ms):
            return "pane"                                         # its core is added here: hc shows it from here
        if row.get("live_status") in LIVE and row.get("tmux_at"):
            return "attach"                                       # live over there in a tmux: ssh shows it
        if row.get("live_status") not in LIVE and tmux.resume_argv(row) and self._has_machine(row.get("host_id"), ssh, ms):
            return "ssh"                                          # ended: it starts again there, through this core
        return "remote"

    def _machine_matches(self, t: dict, row: dict, ssh: dict | None = None, ms: list[dict] | None = None) -> bool:
        """Whether the machine holding that terminal is the machine the row's host id names."""
        want = self.machine_name(row.get("host_id"), ssh)
        target = tmux.ssh_target(row.get("host_id"), ssh or {})
        name = t.get("machine") or (t.get("address") or "").rpartition("/")[0]
        if not name:
            return False
        if name == want:
            return True
        return bool(target) and any(m.get("name") == name and m.get("target") == target
                                    for m in (self.machines_here() if ms is None else ms))

    def open(self, row: dict, *, ssh: dict) -> str:
        ssh = tmux._ssh() if ssh is None else ssh
        this_host = tmux._this_host()
        ts, ms = self.terminals(), self.machines_here()
        t = self.terminal_for(row, ts)
        if t is not None:
            addr = t.get("address") or ""
            if self._focus(addr):
                return f"showing {(row.get('title') or addr)[:40]}"
            return f"attach with: hc attach {addr}"               # a library can't take over this terminal
        if tmux._is_remote(row, this_host):                       # not on a core we can see: what ssh can do
            return tmux.reach_help(row, self._reach_row(row, ts, ms, this_host, ssh), ssh)
        argv = tmux.resume_argv(row)
        if not argv:
            return f"cannot resume a {row.get('harness')} session"
        return f"resumed {(row.get('title') or row.get('session_id') or '')[:40]} in {self._run(argv, row, ssh=ssh)}"

    def start(self, host: str, harness: str, cwd: str, *, profile: str | None = None,
              profiles: list | None = None, ssh: dict) -> str:
        """A fresh session in its own terminal. The key is known up front only where ctx chooses the session id (a
        shell's, a remote claude's ``--session-id``); an agent that picks its own id gets ``ctx.host`` now and its
        ``ctx.key`` from the pid walk as soon as the collector has seen its transcript."""
        ssh = tmux._ssh() if ssh is None else ssh
        here = not tmux._is_remote({"host_id": host}, tmux._this_host())
        try:
            argv, key = self._start_argv(harness, profile, profiles) if here else self._remote_argv(host, harness)
        except ValueError as e:
            return str(e)
        if not argv:
            return f"unknown harness {harness}"
        if here:
            if not shutil.which(argv[0]) and not os.path.isabs(argv[0]):
                return f"{argv[0]} is not installed on this host"
            cwd = os.path.expanduser((cwd or "~").strip() or "~")
            if not os.path.isdir(cwd):
                return f"no such directory: {cwd}"
        elif not self._has_machine(host, ssh):
            return f"no herdr-core machine for {host} (hc machine add)"
        addr = self._run(argv, {"key": key, "host_id": host, "cwd": cwd, "title": f"{harness}-{host}"})
        return f"new {harness} on {host} in {addr}"

    def resume_many(self, rows: list[dict], *, ssh: dict) -> str:
        ssh = tmux._ssh() if ssh is None else ssh
        if not rows:
            return "nothing to resume"
        done, fails = 0, []
        for row in rows:
            try:
                argv = tmux.resume_argv(row)
                if not argv:
                    raise ValueError(f"cannot resume a {row.get('harness')} session")
                self._run(argv, row, ssh=ssh)
                done += 1
            except Exception as e:                                # noqa: BLE001 - one row's failure can't stop the rest
                fails.append(f"{tmux._short_title(row)}: {e}")
        return "resumed " + str(done) + "".join(f" · failed: {f}" for f in fails)

    def end(self, row: dict, *, ssh: dict) -> str:
        t = self.terminal_for(row)
        if t is None:
            return "it isn't in a herdr-core terminal"
        self.rpc("terminal.kill", {"terminal": t.get("address") or ""})
        return f"ended {tmux._short_title(row)}"

    def visible_keys(self, rows: list[dict]) -> set[str]:
        """Always empty: hc says nothing about which terminal a person is looking at, so an hc board has no "on
        screen" marker to show."""
        return set()

    def where(self, row: dict) -> str | None:
        t = self.terminal_for(row)
        return t.get("address") if t else None

    # -- starting and resuming -----------------------------------------

    def _run(self, argv: list[str], row: dict, *, ssh: dict | None = None) -> str:
        """terminal.run, backgrounded, labelled with the row's key so the next look finds it by that alone."""
        host = row.get("host_id") or ""
        remote = tmux._is_remote(row, tmux._this_host())
        cwd = (row.get("cwd") or "").strip() or None
        if remote and cwd and cwd.startswith("~"):
            cwd = None                                            # ~ over there is not ~ here: leave it its own home
        params: dict = {"argv": list(argv), "labels": {HOST_LABEL: host}, "keep": True, "no_parent": True}
        if row.get("key"):
            params["labels"][KEY_LABEL] = row["key"]
        if cwd:
            params["cwd"] = cwd
        if remote:
            params["on"] = self.machine_name(host, ssh)
        if name := self._name(row.get("title") or row.get("key")):
            params["name"] = name
        return self.rpc("terminal.run", params).get("address") or ""

    def _name(self, title: str | None) -> str | None:
        """A terminal name out of a title: hc wants 1-32 of ``a-z 0-9 - _``."""
        slug = re.sub(r"[^a-z0-9_-]", "-", (title or "").lower()).strip("-")
        return slug[:32] or None

    def _focus(self, addr: str) -> bool:
        """Bring that terminal to the front through the herdr app, when this host has one answering."""
        if not self.focus_app or not shutil.which(self.focus_app):
            return False
        try:
            r = subprocess.run([self.focus_app, "do", "agent.focus", "--terminal", addr],
                               capture_output=True, text=True, timeout=3)
        except Exception:                                          # noqa: BLE001 - a timeout just means no app here
            return False
        return r.returncode == 0

    def _start_argv(self, harness: str, profile: str | None, profiles: list | None) -> tuple[list | None, str | None]:
        """The argv for a fresh session on this host, built the way client/tmux.new_agent builds one, with the key we
        can label it with now (None: a provisional one, replaced by the pid walk once the collector has an id)."""
        from ctxswitch.common.config import profile_named
        host = tmux._this_host()
        if harness == "claude":
            profs = tmux._profiles() if profiles is None else profiles
            prof = profile_named(profile, profs)
            if prof is None:
                raise ValueError(f"no claude profile {profile!r} (have: {', '.join(p.name for p in profs)})")
            argv = [*prof.argv(), *(["--model", prof.model] if prof.model else [])]
            return argv, None            # its session id is Claude's to choose: the pid walk gives the terminal its key
        if harness in ("pi", "agy"):
            return [harness], None       # ... same here
        if harness == "shell":
            sid = str(uuid.uuid4())                                # the id `ctx shell` records under: its key's tail
            return [tmux._ctx_bin(), "shell", "--id", sid], f"{host}/shell/{sid}"
        return None, None

    def _remote_argv(self, host: str, harness: str) -> tuple[list | None, str | None]:
        """A fresh session on another host: its profiles are its own, so the harness is all we can choose from here
        — the same limits as tmux.new_remote_agent."""
        if harness == "claude":
            sid = str(uuid.uuid4())                                # the id it starts under is the key we label it with
            return ["claude", "--session-id", sid], f"{host}/claude/{sid}"
        if harness == "pi":
            return ["pi"], None          # its session id is pi's to choose there
        if harness == "shell":
            sid = str(uuid.uuid4())
            return [tmux._ctx_bin(), "shell", "--id", sid], f"{host}/shell/{sid}"
        return None, None
