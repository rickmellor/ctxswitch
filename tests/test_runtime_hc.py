"""The Runtime interface over herdr-core (hc): a fake core on a real unix socket, and one test against the real hc.

The fake answers JSON-RPC 2.0 one line per call on a socket in a thread and records every request, so a test can say
what the driver sent — argv, cwd, on, labels, keep — and not only what it did with the answer.
"""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

import pytest

from ctxswitch.client import tmux
from ctxswitch.client.runtime import Runtime, get_runtime
from ctxswitch.client.runtime import hc as hcmod
from ctxswitch.client.runtime.hc import HcRuntime
from ctxswitch.client.runtime.tmux import TmuxRuntime
from ctxswitch.common.config import ClaudeProfile

HERE = "laptop"


# ---------------------------------------------------------------- the fake core

class FakeCore:
    """A herdr-core stand-in: JSON-RPC over a real unix socket, served in a thread, recording every request."""

    def __init__(self, path: Path, terminals: list[dict] | None = None, machines: list[dict] | None = None):
        self.path = str(path)
        self.terminals = terminals or []
        self.machines = machines or []
        self.by_addr = {t["address"]: t for t in self.terminals}
        self.remote: dict[str, list[dict]] = {}          # machine name -> what its own core would answer
        self.requests: list[tuple[str, dict]] = []
        self.runs = 0
        self.killed: list[str] = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(64)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def add(self, **t):
        t.setdefault("state", "live")
        t.setdefault("labels", {})
        t.setdefault("argv", ["sleep", "30"])
        self.terminals.append(t)
        self.by_addr[t["address"]] = t
        return t

    def calls(self, method: str) -> list[dict]:
        return [p for m, p in self.requests if m == method]

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        try:
            self.server.close()
        except OSError:
            pass
        Path(self.path).unlink(missing_ok=True)

    # -- serving ----------------------------------------------------------

    def _serve(self):
        self.server.settimeout(0.2)
        while not self.stop.is_set():
            try:
                conn, _ = self.server.accept()
            except (TimeoutError, OSError):
                continue
            with conn:
                conn.settimeout(5)
                f = conn.makefile("rw")
                while not self.stop.is_set():
                    try:
                        line = f.readline()
                    except OSError:
                        break
                    if not line:
                        break
                    f.write(json.dumps(self.reply(json.loads(line))) + "\n")
                    f.flush()

    def reply(self, msg: dict) -> dict:
        try:
            result = self.handle(msg.get("method") or "", msg.get("params") or {})
        except LookupError as e:
            return {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32001, "message": str(e)}}
        self.requests.append((msg.get("method") or "", msg.get("params") or {}))
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": result}

    def handle(self, method: str, params: dict):
        if method == "machine.list":
            return {"machines": self.machines}
        if method == "terminal.list":
            # a core lists its own terminals, then each connected machine's with the machine in the address
            ts = [self.summary(t) for t in self.terminals if params.get("all") or t["state"] == "live"]
            for m in self.machines:
                if m.get("state", "connected") == "connected":
                    ts += [{**t, "address": f"{m['name']}/{t['address']}"} for t in self.remote.get(m["name"], [])]
            return {"terminals": ts, "hidden_finished": 0,
                    "machines": [{"name": m["name"], "state": m.get("state", "connected")} for m in self.machines]}
        if method == "terminal.run":
            self.runs += 1
            t = {"address": f"t{self.runs}", "argv": params.get("argv"), "cwd": params.get("cwd"),
                 "labels": dict(params.get("labels") or {}), "state": "live", "pid": 900 + self.runs}
            if params.get("name"):
                t["name"] = params["name"]
            self.add(**t)
            return dict(t, cols=200, rows=120, keep=params.get("keep", False))
        if method == "terminal.kill":
            t = self.get_terminal(params["terminal"])
            t["state"], t["exit_code"] = "finished", 0
            self.killed.append(params["terminal"])
            return t
        if method == "terminal.label":
            t = self.get_terminal(params["terminal"])
            t["labels"] = {**(t.get("labels") or {}), **params.get("labels", {})}
            return t
        if method == "terminal.get":
            return self.get_terminal(params["terminal"])
        raise LookupError(f"unknown method {method}")

    def get_terminal(self, addr: str) -> dict:
        if addr not in self.by_addr:
            raise LookupError(f"no terminal {addr}")
        return self.by_addr[addr]

    def summary(self, t: dict) -> dict:
        return {k: t[k] for k in ("address", "argv", "cwd", "labels", "name", "pid", "state", "status", "turn")
                if k in t}


@pytest.fixture
def core(tmp_path, monkeypatch):
    """A private core on its own socket; the board believes it runs on "laptop" and has no [ssh] table of its own."""
    srv = FakeCore(tmp_path / "core.sock")
    monkeypatch.setenv("HC_SOCKET", str(tmp_path / "core.sock"))
    monkeypatch.delenv("HC_SESSION", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(tmux, "_this_host", lambda: HERE)
    monkeypatch.setattr(tmux, "_ssh", lambda: {})
    monkeypatch.setattr(tmux, "_profiles", lambda: [ClaudeProfile()])
    monkeypatch.setattr(hcmod, "BIN", str(tmp_path / "no-such-hc"))     # the CLI fallback is never reached
    yield srv
    srv.close()


@pytest.fixture
def rt(core, tmp_path):
    return HcRuntime(proc=str(tmp_path / "proc"))


def row(**kw) -> dict:
    """A board row: a claude session on "laptop" unless told otherwise."""
    r = {"key": f"{HERE}/claude/sid1", "host_id": HERE, "harness": "claude", "session_id": "sid1",
         "cwd": "/home/rick/repos/ctxswitch", "title": "Widget work", "live_status": "ended", "pid": None}
    r.update(kw)
    return r


def sent(core, i=0) -> dict:
    """The params of the i-th terminal.run the fake core saw."""
    return core.calls("terminal.run")[i]


# ---------------------------------------------------------------- plumbing

def test_socket_path_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HC_SOCKET", str(tmp_path / "overridden.sock"))
    assert hcmod.socket_path() == str(tmp_path / "overridden.sock")
    monkeypatch.delenv("HC_SOCKET")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("HC_SESSION", "ctxtest")
    assert hcmod.socket_path() == str(tmp_path / "herdr-core" / "ctxtest.sock")
    monkeypatch.delenv("HC_SESSION")
    assert hcmod.socket_path() == str(tmp_path / "herdr-core" / "default.sock")
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert hcmod.socket_path() == f"/run/user/{os.getuid()}/herdr-core/default.sock"


def test_the_cli_fallback_maps_each_call_to_a_command():
    assert hcmod.cli_argv("terminal.list", {"all": True, "labels": {"ctx.key": "a/b/c"}}) == \
        ["ls", "--json", "--all", "--label", "ctx.key=a/b/c"]
    assert hcmod.cli_argv("terminal.kill", {"terminal": "t7"}) == ["kill", "t7", "--json"]
    assert hcmod.cli_argv("terminal.get", {"terminal": "t7"}) == ["get", "t7", "--json"]
    assert hcmod.cli_argv("machine.list", {}) == ["machine", "ls", "--json"]
    assert hcmod.cli_argv("terminal.label", {"terminal": "t7", "labels": {"ctx.key": "a/b"}}) == \
        ["label", "t7", "ctx.key=a/b"]
    assert hcmod.cli_argv("terminal.run", {"argv": ["sleep", "30"], "cwd": "/tmp", "keep": True, "on": "gpu-host",
                                           "labels": {"ctx.host": "laptop"}, "name": "sleep30"}) == \
        ["run", "--bg", "--json", "--name", "sleep30", "--cwd", "/tmp", "--on", "gpu-host",
         "--label", "ctx.host=laptop", "--keep", "--", "sleep", "30"]
    assert hcmod.cli_argv("events.subscribe", {}) is None            # only the socket can follow changes


def test_no_socket_means_the_cli_answers(core, monkeypatch, tmp_path):
    hc = tmp_path / "hc"
    hc.write_text('#!/bin/sh\ncase "$1" in\n'
                  ' --version) echo "hc 0.0-test" ;;\n'
                  ' machine) echo \'{"machines":[]}\' ;;\n'
                  ' ls) echo \'{"terminals":[{"address":"t9","state":"live",'
                  '"labels":{"ctx.key":"laptop/claude/sid1"}}],"hidden_finished":0,"machines":[]}\' ;;\nesac\n')
    hc.chmod(0o755)
    monkeypatch.setattr(hcmod, "BIN", str(hc))
    r = HcRuntime(sock=str(tmp_path / "nobody-home.sock"))
    assert r.available() is True
    assert [t["address"] for t in r.terminals()] == ["t9"]
    assert r.where(row(live_status="idle")) == "t9"
    assert r.reach([row(live_status="idle")], HERE, {}) == {"laptop/claude/sid1": "pane"}


def test_a_socket_that_refuses_is_no_core_and_an_error(core, monkeypatch, tmp_path):
    monkeypatch.setattr(hcmod, "BIN", str(tmp_path / "no-such-hc"))   # nothing to fall back on
    dead = tmp_path / "dead.sock"
    dead.write_text("")                                              # a file a dead core left behind
    r = HcRuntime(sock=str(dead))
    assert r.available() is False                                    # answering, or nothing, never "maybe"
    with pytest.raises(hcmod.HcError):
        r.terminals()
    with pytest.raises(hcmod.HcError):                               # a real socket that says no
        HcRuntime(sock=core.path).rpc("terminal.rm", {"terminal": "t1"})


# ---------------------------------------------------------------- reach

def test_reach_on_this_host_found_foreign_resume_and_none(core, rt):
    core.add(address="t1", labels={"ctx.key": "laptop/claude/here"})
    core.add(address="t2", labels={"ctx.key": "laptop/claude/quit"}, state="finished")
    rows = [row(key="laptop/claude/here", live_status="idle", pid=4242),
            row(key="laptop/claude/foreign", live_status="busy", pid=4242),      # live, but no terminal holds it
            row(key="laptop/claude/gone", live_status="ended"),
            row(key="laptop/claude/quit", live_status="ended"),
            row(key="laptop/other/x", harness="mystery", session_id="x", live_status="ended")]
    assert rt.reach(rows, HERE, {}) == {"laptop/claude/here": "pane",
                                        "laptop/claude/foreign": "foreign",
                                        "laptop/claude/gone": "resume",
                                        "laptop/claude/quit": "resume",           # finished: Enter starts it again
                                        "laptop/other/x": "none"}


def test_reach_finds_an_unlabelled_terminal_by_the_pid_walk(core, rt, tmp_path):
    proc = tmp_path / "proc"
    (proc / "4242").mkdir(parents=True)
    (proc / "4242" / "stat").write_text("4242 (claude) S 4111 4242 4242 0\n")     # its parent is the terminal's pid
    r = HcRuntime(proc=str(proc))
    core.add(address="t1", pid=4111)                                              # started by hand: no labels yet
    assert r.reach([row(live_status="busy", pid=4242)], HERE, {}) == {"laptop/claude/sid1": "pane"}
    assert core.calls("terminal.label") == [{"terminal": "t1", "labels": {"ctx.key": "laptop/claude/sid1",
                                                                          "ctx.host": HERE}}]
    assert r.where(row(live_status="busy", pid=4242)) == "t1"


def test_reach_on_another_host_found_attach_ssh_and_remote(core, rt):
    remote = lambda **kw: row(key="gpu-host/claude/r1", host_id="gpu-host", **kw)   # noqa: E731
    assert rt.reach([remote(live_status="idle", pid=7)], HERE, {}) == {"gpu-host/claude/r1": "remote"}

    core.machines = [{"name": "gpu-host", "host": "gpu-host", "state": "connected", "target": "gpu-host.internal"}]
    core.remote["gpu-host"] = [{"address": "t7", "state": "live", "argv": ["claude"],
                                "labels": {"ctx.key": "gpu-host/claude/r1"}}]
    assert rt.reach([remote(live_status="idle", pid=7)], HERE, {}) == {"gpu-host/claude/r1": "pane"}
    assert rt.where(remote(live_status="idle", pid=7)) == "gpu-host/t7"
    core.remote["gpu-host"] = []                                             # that core shows nothing of ours
    assert rt.reach([remote(live_status="busy", pid=7, tmux_at="ctx:@2.%9")], HERE, {}) == {"gpu-host/claude/r1": "attach"}
    assert rt.reach([remote(live_status="busy", pid=7)], HERE, {}) == {"gpu-host/claude/r1": "remote"}
    assert rt.reach([remote(live_status="ended")], HERE, {}) == {"gpu-host/claude/r1": "ssh"}
    assert rt.reach([remote(live_status="ended", harness="mystery")], HERE, {}) == {"gpu-host/claude/r1": "remote"}
    assert rt.reach([row(key="other/claude/x", host_id="other", live_status="ended")], HERE, {}) == \
        {"other/claude/x": "remote"}
    # a machine the core names apart from the host id is this host's too, by the [ssh] target it reaches
    core.machines = [{"name": "elsewhere", "host": "gpu-host", "state": "connected", "target": "gpu-host.internal"}]
    core.remote = {"elsewhere": [{"address": "t9", "state": "live", "labels": {"ctx.key": "gpu-host/claude/r1"}}]}
    assert rt.reach([remote(live_status="idle", pid=7)], HERE, {"gpu-host": "gpu-host.internal"}) == \
        {"gpu-host/claude/r1": "pane"}
    assert rt.where(remote(live_status="idle", pid=7)) == "elsewhere/t9"
    assert rt.start("gpu-host", "pi", "/tmp", ssh={"gpu-host": "gpu-host.internal"}).endswith("in t1")
    assert sent(core)["on"] == "elsewhere"                                   # the name this core reaches it by
    core.remote = {}
    assert rt.reach([remote(live_status="ended")], HERE, {"gpu-host": "gpu-host.internal"}) == \
        {"gpu-host/claude/r1": "ssh"}
    # a machine that isn't connected lists nothing, so nothing there can be reached but over ssh
    core.machines = [{"name": "gpu-host", "host": "gpu-host", "state": "unreachable", "target": "gpu-host.internal"}]
    assert rt.reach([remote(live_status="ended")], HERE, {}) == {"gpu-host/claude/r1": "ssh"}


# ---------------------------------------------------------------- open / start / resume_many / end

def test_open_a_live_terminal_focuses_the_app_or_says_attach(core, rt, monkeypatch, tmp_path):
    core.add(address="t1", labels={"ctx.key": "laptop/claude/sid1"})
    monkeypatch.setattr(rt, "focus_app", None)                       # no herdr app on this host
    assert rt.open(row(live_status="idle", pid=1), ssh={}) == "attach with: hc attach t1"

    app = tmp_path / "herdr-app"
    app.write_text(f"#!/bin/sh\necho \"$*\" > {tmp_path}/app\nexit 0\n")
    app.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setattr(rt, "focus_app", str(app))
    assert rt.open(row(live_status="idle", pid=1), ssh={}) == "showing Widget work"
    assert (tmp_path / "app").read_text().strip() == "do agent.focus --terminal t1"


def test_open_an_ended_claude_resumes_it_under_its_own_profile(core, rt, tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    for p in (a, b):
        (p / "projects").mkdir(parents=True)
    monkeypatch.setattr(tmux, "_profiles", lambda: [
        ClaudeProfile(name="work", command=["claude"], config_dir=str(a)),
        ClaudeProfile(name="max", command=["claude"], config_dir=str(b), model="opus")])
    r = row(session_id="abc", live_status="ended", meta={"transcript_path": str(b / "projects" / "-p" / "abc.jsonl")})
    assert rt.open(r, ssh={}) == "resumed Widget work in t1"
    p = sent(core)
    assert p["argv"] == ["env", f"CLAUDE_CONFIG_DIR={b}", "claude", "--resume", "abc"]   # its own profile's account
    assert p["keep"] is True and p["no_parent"] is True and "on" not in p
    assert p["cwd"] == "/home/rick/repos/ctxswitch"
    assert p["labels"] == {"ctx.key": "laptop/claude/sid1", "ctx.host": HERE}
    assert p["name"] == "widget-work"                                # a slug of its title


def test_open_an_ended_pi_or_shell(core, rt, tmp_path):
    tp = tmp_path / "session.jsonl"
    tp.write_text("")
    rt.open(row(harness="pi", session_id="p1", meta={"transcript_path": str(tp)}), ssh={})
    assert sent(core)["argv"] == ["pi", "--session", str(tp)]
    sid = str(uuid.uuid4())
    r = row(harness="shell", session_id=sid, key=f"{HERE}/shell/{sid}")
    assert rt.open(r, ssh={}) == "resumed Widget work in t2"
    assert sent(core, 1)["argv"] == [tmux._ctx_bin(), "shell", "--id", sid]


def test_open_says_no_when_nothing_can_resume_it(core, rt):
    assert rt.open(row(harness="mystery"), ssh={}) == "cannot resume a mystery session"
    assert core.runs == 0


def test_open_a_remote_row_says_what_reach_said(core, rt):
    r = row(key="gpu-host/claude/r1", host_id="gpu-host", live_status="busy", pid=7)
    assert rt.open(r, ssh={}) == "running on gpu-host: quit it there, then Enter resumes it here over ssh"
    assert core.runs == 0


def test_start_claude_pi_and_shell_here(core, rt, tmp_path, monkeypatch):
    monkeypatch.setattr(hcmod.shutil, "which", lambda x: f"/usr/bin/{x}")
    monkeypatch.setattr(tmux, "_profiles", lambda: [ClaudeProfile(name="max", command=["claude"], model="opus")])
    assert rt.start(HERE, "claude", str(tmp_path), ssh={}) == f"new claude on {HERE} in t1"
    p = sent(core)
    assert p["argv"] == ["claude", "--model", "opus"] and p["cwd"] == str(tmp_path)
    assert p["labels"] == {"ctx.host": HERE}                         # its session id is Claude's to choose
    assert p["keep"] is True and p["no_parent"] is True and "on" not in p

    assert rt.start(HERE, "pi", str(tmp_path), ssh={}) == f"new pi on {HERE} in t2"
    assert sent(core, 1)["argv"] == ["pi"]

    out = rt.start(HERE, "shell", str(tmp_path), ssh={})
    p = sent(core, 2)
    sid = p["argv"][3]
    assert p["argv"][1:3] == ["shell", "--id"] and uuid.UUID(sid)         # ctx shell --id <uuid>
    assert out == f"new shell on {HERE} in t3"
    assert p["labels"] == {"ctx.host": HERE, "ctx.key": f"{HERE}/shell/{sid}"}   # its key is known before it starts

    assert rt.start(HERE, "mystery", str(tmp_path), ssh={}) == "unknown harness mystery"
    assert rt.start(HERE, "claude", str(tmp_path / "nope"), ssh={}).startswith("no such directory")
    assert rt.start(HERE, "claude", str(tmp_path), profile="ghost", ssh={}).startswith("no claude profile 'ghost'")
    monkeypatch.setattr(hcmod.shutil, "which", lambda x: None)
    assert rt.start(HERE, "pi", str(tmp_path), ssh={}) == "pi is not installed on this host"


def test_start_on_another_host_needs_a_machine_there(core, rt, monkeypatch):
    assert rt.start("gpu-host", "claude", "~/repos/x", ssh={}).startswith("no herdr-core machine for gpu-host")
    assert core.runs == 0
    core.machines = [{"name": "gpu-host", "host": "gpu-host", "state": "connected", "target": "gpu-host"}]
    out = rt.start("gpu-host", "claude", "~/repos/x", ssh={})
    p = sent(core)
    assert out == "new claude on gpu-host in t1"
    assert p["on"] == "gpu-host" and p["labels"]["ctx.host"] == "gpu-host"
    assert p["labels"]["ctx.key"].startswith("gpu-host/claude/")         # it starts under the id we gave it
    assert p["argv"][:2] == ["claude", "--session-id"]
    assert "cwd" not in p                                                # ~ there is not ~ here: it keeps its own home


def test_resume_many_reports_every_row_in_one_message(core, rt):
    good = row(key="laptop/claude/a", session_id="a", title="Alpha")
    bad = row(key="laptop/mystery/b", harness="mystery", session_id="b", title="Beta")
    assert rt.resume_many([good, bad], ssh={}) == "resumed 1 · failed: Beta: cannot resume a mystery session"
    assert sent(core)["argv"] == ["claude", "--resume", "a"]
    assert rt.resume_many([], ssh={}) == "nothing to resume"


def test_end_kills_the_one_terminal_it_found(core, rt):
    core.add(address="t1", labels={"ctx.key": "laptop/claude/sid1"})
    assert rt.end(row(live_status="idle", pid=1), ssh={}) == "ended Widget work"
    assert core.calls("terminal.kill") == [{"terminal": "t1"}]
    assert core.killed == ["t1"]
    assert rt.end(row(key="laptop/claude/none"), ssh={}) == "it isn't in a herdr-core terminal"
    assert len(core.killed) == 1


def test_where_and_visible_keys(core, rt):
    core.add(address="t1", labels={"ctx.key": "laptop/claude/sid1"})
    assert rt.where(row(live_status="idle", pid=1)) == "t1"
    assert rt.where(row(key="laptop/claude/other")) is None
    assert rt.visible_keys([row()]) == set()                          # hc never says who is looking at what


def test_a_real_run_reply_leaves_a_usable_address(rt, monkeypatch):
    """The shape hc 0.1.18 answers terminal.run with: its address is what the driver hands back."""
    real = {"address": "t1", "argv": ["sleep", "30"], "cols": 200, "created_ms": 1, "cwd": "/tmp",
            "die_with_parent": False, "exit_code": None, "finished_ms": None, "identity": "ghostty", "keep": True,
            "labels": {"ctx.key": "k"}, "name": None, "notify": True, "parent": None, "pid": 412, "reason": None,
            "reported": False, "rm": False, "rows": 120, "screen": "exact", "signal": None, "state": "live",
            "status": None, "status_ms": None, "template": None, "title": "", "turn": 0}
    monkeypatch.setattr(rt, "rpc", lambda m, p=None, **kw: dict(real))
    assert rt._run(["sleep", "30"], row()) == "t1"


# ---------------------------------------------------------------- the interface

def test_get_runtime_picks_a_driver(monkeypatch):
    monkeypatch.setattr(tmux, "in_tmux", lambda: True)
    monkeypatch.setattr(HcRuntime, "available", lambda self: True)
    assert isinstance(get_runtime("tmux"), TmuxRuntime) and get_runtime("tmux").name == "tmux"
    assert isinstance(get_runtime("hc"), HcRuntime) and get_runtime("hc").name == "hc"
    assert isinstance(get_runtime("HC"), HcRuntime)                   # spelled however you like
    assert isinstance(get_runtime(None), TmuxRuntime)                 # inside tmux: tmux, even with hc around
    monkeypatch.setattr(tmux, "in_tmux", lambda: False)
    assert isinstance(get_runtime(), HcRuntime)                       # not in tmux, hc answering: hc
    monkeypatch.setattr(HcRuntime, "available", lambda self: False)
    assert isinstance(get_runtime(), TmuxRuntime)                     # nothing else to drive it
    with pytest.raises(ValueError):
        get_runtime("screen")


def test_both_drivers_are_runtimes(core):
    for r in (HcRuntime(), TmuxRuntime()):
        assert isinstance(r, Runtime)
        assert isinstance(r.name, str)
        for m in ("available", "reach", "open", "start", "resume_many", "end", "visible_keys", "where"):
            assert callable(getattr(r, m)), m


def test_tmux_runtime_delegates_and_adds_nothing(monkeypatch):
    calls: list[tuple] = []
    pane = type("P", (), {"id": "%7"})()

    def stub(name, ret=None):
        def f(*a, **kw):
            calls.append((name, a, kw))
            return ret
        return f

    for fn, ret in [("in_tmux", True), ("reach_all", {"k": "pane"}), ("show", "showing x"), ("new_agent", "new pi"),
                    ("new_remote_agent", "new pi on gpu-host"), ("resume_many", "resumed 2"), ("end_remote", "ended"),
                    ("on_screen_keys", {"k"}), ("visible_key", "viewer"), ("pane_for_session", pane),
                    ("agent_pane", pane), ("end_agent", 1), ("_this_host", HERE)]:
        monkeypatch.setattr(tmux, fn, stub(fn, ret))
    monkeypatch.setattr(tmux, "_is_remote", lambda s, h: (calls.append(("_is_remote", (s, h), {})) or
                                                           s.get("host_id") != HERE))
    r = TmuxRuntime()
    assert r.available() is True
    assert r.reach([row()], HERE, {"a": "b"}) == {"k": "pane"} and calls[-1][1] == ([row()], HERE, {"a": "b"})
    assert r.open(row(), ssh={"a": "b"}) == "showing x" and calls[-1][2] == {"ssh": {"a": "b"}}
    assert r.resume_many([row()], ssh={}) == "resumed 2" and calls[-1][2] == {"ssh": {}}
    assert r.visible_keys([row()]) == {"k", "viewer"}          # the panes on screen and the viewer's own session
    assert calls[-2][2] == {"sessions": [row()]} and calls[-1][2] == {"sessions": [row()]}
    assert r.where(row()) == "%7"
    assert r.end(row(), ssh={}) == "ended Widget work"                # its own pane, then tmux.end_agent
    assert calls[-2][:2] == ("agent_pane", ("laptop/claude/sid1",)) and calls[-1][0] == "end_agent"
    assert r.end(row(host_id="gpu-host"), ssh={}) == "ended" and calls[-1][0] == "end_remote"
    assert r.start("gpu-host", "pi", "/tmp", ssh={"g": "h"}) == "new pi on gpu-host"
    assert calls[-1][1] == ("gpu-host", "pi", "/tmp", {"g": "h"})
    assert r.start(HERE, "pi", "/tmp", profile="max", profiles=[], ssh={}) == "new pi"
    assert calls[-1][1] == ("pi", "/tmp") and calls[-1][2] == {"profile": "max", "profiles": []}


# ---------------------------------------------------------------- the real herdr-core

@pytest.mark.skipif(shutil.which("hc") is None, reason="herdr-core (hc) not installed")
def test_a_real_core_over_the_socket(monkeypatch):
    monkeypatch.setattr(tmux, "_this_host", lambda: HERE)
    # a short path: hc refuses a socket path over its limit, and pytest's tmp_path is far over it
    base = Path(tempfile.mkdtemp(prefix="hc", dir="/tmp"))
    env = {"XDG_STATE_HOME": str(base / "s"), "XDG_RUNTIME_DIR": str(base / "r"), "HC_SESSION": f"c{os.getpid()}"}
    (base / "r").mkdir()
    started = subprocess.run(["hc", "server", "start", "--json"], capture_output=True, text=True,
                             env={**os.environ, **env})
    if started.returncode != 0:
        shutil.rmtree(base, ignore_errors=True)
        pytest.skip(f"no private herdr-core here: {started.stderr.strip()[:120]}")
    key = f"{HERE}/shell/{uuid.uuid4()}"
    r = HcRuntime(sock=str(base / "r" / "herdr-core" / f"{env['HC_SESSION']}.sock"), env=env)
    try:
        assert r.available() is True
        addr = r._run(["sleep", "30"], {"key": key, "host_id": HERE, "cwd": str(base)})
        assert addr.startswith("t")
        assert r.where({"key": key, "host_id": HERE, "live_status": "idle"}) == addr
        assert r.reach([{"key": key, "host_id": HERE, "live_status": "idle"}], HERE, {}) == {key: "pane"}
        live = [t for t in r.terminals() if t["address"] == addr]
        assert live and live[0]["state"] == "live" and live[0]["labels"]["ctx.key"] == key
        assert r.open({"key": key, "host_id": HERE, "live_status": "idle", "title": "sleep"}, ssh={}).startswith(
            ("attach with: hc attach ", "showing "))
        assert r.end({"key": key, "host_id": HERE, "title": "sleep"}, ssh={}) == "ended sleep"
        got = subprocess.run(["hc", "get", addr, "--json"], capture_output=True, text=True,
                             env={**os.environ, **env})
        assert json.loads(got.stdout)["state"] == "finished"
    finally:
        subprocess.run(["hc", "server", "stop"], capture_output=True, text=True, env={**os.environ, **env})
        shutil.rmtree(base, ignore_errors=True)
