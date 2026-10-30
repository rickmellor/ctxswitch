"""Shell sessions as sessions: started as `ctx shell --id <uuid>`, managed on the board like agents, never sent a
message, and recapped as commands rather than conversation."""
import os
import shutil
import signal
import subprocess
import time
import uuid

import pytest
from starlette.testclient import TestClient

from ctxswitch.client import fmt, tmux
from ctxswitch.hub.api import Hub
from ctxswitch.hub.store import Store
from ctxswitch.hub.tracker.recap import build_prompt
from test_phase45 import _ask, _session, _turn

needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
H = {"Authorization": "Bearer t"}
WS = "me/proj@main"
LAP, SHELL = "laptop/claude/s1", "laptop/shell/3f2b0000-0000-4000-8000-000000000000"
SOCK = f"ctxshell{os.getpid()}"
_n = 0


def tm(*a, check=True):
    r = subprocess.run(["tmux", "-L", SOCK, *a], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(r.stderr)
    return r.stdout.strip()


@pytest.fixture
def server(monkeypatch):
    """A private tmux server with the manager layout: viewer left, board right. (tests/test_tmux.py's, own socket.)"""
    global SOCK, _n
    _n += 1
    SOCK = f"ctxshell{os.getpid()}_{_n}"
    monkeypatch.setattr(tmux, "TMUX", "tmux")
    monkeypatch.setenv("TMUX", "fake")
    real = subprocess.run
    def run(args, **kw):
        if args and args[0] == "tmux":
            args = ["tmux", "-L", SOCK, *args[1:]]
        return real(args, **kw)
    monkeypatch.setattr(tmux.subprocess, "run", run)
    monkeypatch.setattr(tmux, "_this_host", lambda: "laptop")
    monkeypatch.setattr(tmux, "_ssh", lambda: {})
    tm("new-session", "-d", "-s", "ctx", "-n", "mgr", "-x", "160", "-y", "40")
    tm("split-window", "-h", "-l", "50", "-t", "ctx:mgr")
    yield
    tm("kill-server", check=False)


def mgr(ps=None):
    return sorted((p for p in (ps if ps is not None else tmux.panes()) if p.window_name == "mgr"), key=lambda p: p.index)


def wait_for(what, tries=40):
    for _ in range(tries):
        if what():
            return True
        time.sleep(0.1)
    return False


def fake_ctx(tmp_path, body: str):
    """A stand-in for `ctx shell`: the real one isn't on this branch, so only the argv we build is under test."""
    p = tmp_path / "ctx"
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(0o755)
    return str(p)


# -- starting one ---------------------------------------------------------------------------------------------

@needs_tmux
def test_n_starts_a_recording_shell_tagged_as_its_own_session(server, monkeypatch, tmp_path):
    """n's shell is a session now: `ctx shell --id <uuid4>`, and its pane carries that key from the start."""
    _, tui = mgr()
    monkeypatch.setenv("TMUX_PANE", tui.id)
    monkeypatch.setattr(tmux, "_ctx_bin", lambda: fake_ctx(tmp_path, "exec bash --norc -i"))
    assert tmux.new_agent("shell", str(tmp_path)) == f"new shell in {tmp_path}"
    v = tmux.viewer_pane()
    assert v.role == "shell" and v.window_name == "mgr"                    # swapped into the viewer, as an agent is
    host, harness, sid = v.key.split("/")
    assert (host, harness) == ("laptop", "shell") and str(uuid.UUID(sid)) == sid     # the key the collector will report
    assert wait_for(lambda: next(p for p in tmux.panes() if p.id == v.id).cmd == "bash")
    cmd = tm("display-message", "-p", "-t", v.id, "#{pane_start_command}")
    assert f"shell --id {sid}" in cmd                                      # the same id in the pane's argv
    assert tmux.visible_key(sessions=[{"key": v.key}]) == v.key             # Alt+N finds it before the collector does


@needs_tmux
def test_a_shell_at_its_prompt_is_a_live_session_the_reap_leaves_alone(server, monkeypatch, tmp_path):
    _, tui = mgr()
    monkeypatch.setenv("TMUX_PANE", tui.id)
    monkeypatch.setattr(tmux, "_ctx_bin", lambda: fake_ctx(tmp_path, "exec bash --norc -i"))
    tmux.new_agent("shell", str(tmp_path))
    v = tmux.viewer_pane()
    assert wait_for(lambda: next(p for p in tmux.panes() if p.id == v.id).cmd == "bash")
    ps = tmux.panes()
    assert v.id in {p.id for p in tmux.agent_panes(ps)}          # bash at a prompt, still managed like an agent
    assert tmux.agent_pane(v.key, ps).id == v.id                 # move/k find it by its session key
    assert tmux.reap_viewer(ps) is None                          # an idle shell is not a dead agent's leftover shell


@needs_tmux
def test_k_hangs_up_a_shell_but_terminates_an_agent(server, monkeypatch, tmp_path):
    """Interactive bash ignores TERM, so a shell session gets SIGHUP; an agent keeps the wrapper's SIGTERM."""
    seen = []
    kill = lambda pid, sig: seen.append(sig)                     # noqa: E731
    monkeypatch.setattr(tmux, "_ctx_bin", lambda: fake_ctx(tmp_path, "exec bash --norc -i"))
    sh = tmux._agent_window("ctx", str(tmp_path), "sh1", [tmux._ctx_bin(), "shell", "--id", "abc"], role="shell")
    ag = tmux._agent_window("ctx", str(tmp_path), "ag1", ["sleep", "300"])
    assert wait_for(lambda: len(tmux._children(int(tm("display-message", "-p", "-t", sh, "#{pane_pid}")))) == 1)
    assert wait_for(lambda: len(tmux._children(int(tm("display-message", "-p", "-t", ag, "#{pane_pid}")))) == 1)
    ps = {p.id: p for p in tmux.panes()}
    assert tmux.end_agent(ps[sh], kill=kill) == 1 and seen == [signal.SIGHUP]
    assert tmux.end_agent(ps[ag], kill=kill) == 1 and seen == [signal.SIGHUP, signal.SIGTERM]


@needs_tmux
def test_an_exiting_agent_hands_the_viewer_to_a_shell_session(server, monkeypatch, tmp_path):
    """Who takes the left pane: a shell session counts as a session, so it beats a fresh anonymous shell."""
    viewer, tui = mgr()
    monkeypatch.setenv("TMUX_PANE", tui.id)
    tmux.set_role(tui.id, "tui")
    tmux.set_role(viewer.id, "agent")
    sh = tmux._agent_window("ctx", str(tmp_path), "shwork", ["bash", "--norc", "-i"], role="shell")
    tmux.tag(sh, "laptop/shell/aaa")
    assert wait_for(lambda: next(p for p in tmux.panes() if p.id == sh).cmd == "bash")
    assert tmux.on_agent_exit(pane_id=viewer.id, prompt=lambda _: None).startswith("swapped")
    assert tmux.viewer_pane().id == sh


# -- resuming one ---------------------------------------------------------------------------------------------

@needs_tmux
def test_resume_argv_and_the_window_a_resumed_shell_gets(server, monkeypatch, tmp_path):
    """Enter on an ended shell session: a new bash on the same transcript, in that session's directory."""
    bin_ = fake_ctx(tmp_path, f"printf '%s\\n' \"$@\" > {tmp_path}/argv; pwd > {tmp_path}/pwd; exec bash --norc -i")
    monkeypatch.setattr(tmux, "_ctx_bin", lambda: bin_)
    assert tmux.resume_argv({"harness": "shell", "session_id": "3f2b"}) == [bin_, "shell", "--id", "3f2b"]
    _, tui = mgr()
    monkeypatch.setenv("TMUX_PANE", tui.id)
    s = {"key": "laptop/shell/dead", "harness": "shell", "session_id": "dead", "host_id": "laptop",
         "live_status": "ended", "cwd": str(tmp_path)}
    assert tmux.show(s, focus=False).startswith("resumed")
    assert wait_for(lambda: (tmp_path / "argv").exists())
    assert (tmp_path / "argv").read_text().split() == ["shell", "--id", "dead"]
    assert (tmp_path / "pwd").read_text().strip() == str(tmp_path.resolve())
    p = next(p for p in tmux.panes() if p.key == s["key"])
    assert p.role == "shell" and p.id in {q.id for q in tmux.agent_panes()}    # still a shell, not an agent


# -- labels and messages ---------------------------------------------------------------------------------------

def test_the_board_labels_a_shell_and_refuses_to_message_it():
    assert fmt.HARNESS["shell"] == "sh" and fmt.harness_label({"harness": "shell"}) == "sh"
    assert fmt.short_key({"host_id": "laptop", "harness": "shell", "session_id": "3f2b"}) == "laptop/sh/3f2b"
    assert fmt.can_message({"harness": "shell"}) == "shells don't take messages"
    assert fmt.can_message({"harness": "claude"}) is None
    assert fmt.reply_target({"harness": "shell", "asks": []}) == (None, "shells don't take messages")
    from ctxswitch.client.tui import app
    assert app.HARNESS_NAME["shell"] == "shell"


def _live(store, host, *keys, status="idle"):
    store.set_liveness(host, [{"key": k, "status": st, "status_since": "2026-10-29T10:00:00Z", "pid": 1, "socket": "/x"}
                              for k, st in (k if isinstance(k, tuple) else (k, status) for k in keys)])


def test_the_hub_refuses_messages_to_a_shell_whatever_route(tmp_path):
    store = Store(tmp_path / "hub.db")
    _session(store, LAP, live=False)
    _session(store, SHELL, live=False, when="2026-10-29T12:00:00Z")   # the newest thing on this work
    c = TestClient(Hub(store, token="t").app())
    url = f"/v1/sessions/{SHELL}/messages"
    assert c.post(url, headers=H, json={"body": "run the tests"}).status_code == 400      # the operator
    assert c.post(url, headers=H, json={"body": "run the tests", "from_key": LAP}).status_code == 400
    assert store.messages(SHELL) == []                                # nothing queued by either
    # the collector's outbox never gets one, however it got there
    _live(store, "laptop", SHELL, LAP)
    assert store.add_message(SHELL, "queued behind the hub", "laptop")
    assert [m["key"] for m in store.outbox("laptop", agents=True)] == []
    # nor does a workstream message bind to it: the agent on that work takes it, newest shell aside
    mid = c.post("/v1/workstreams/messages", headers=H, json={"workstream": WS, "body": "the hub token moved"}).json()["id"]
    assert [(m["id"], m["key"]) for m in store.outbox("laptop")] == [(mid, LAP)]


def test_an_idle_shell_is_never_a_session_waiting_on_you(tmp_path):
    """A bash is idle at its prompt nearly all the time; that isn't "your move", and it asks nothing."""
    store = Store(tmp_path / "hub.db")
    _session(store, LAP, live=False)
    _session(store, SHELL, live=False)
    store.upsert_turns(SHELL, [_turn(0, "ls", "exit 0 · 0.1s", origin="shell",
                                     asks=[_ask("0:plain", "did that work?")])], None)
    _live(store, "laptop", SHELL, LAP)
    items = store.attention("laptop")
    assert {i["key"] for i in items if i["item"] == "idle"} == {LAP}         # the idle Claude still is
    assert all(i["key"] != SHELL for i in items)                             # and its asks don't count either


# -- the recap ---------------------------------------------------------------------------------------------------

def test_a_shells_recap_prompt_says_what_it_is(tmp_path):
    store = Store(tmp_path / "hub.db")
    _session(store, SHELL)
    store.upsert_turns(SHELL, [_turn(0, "git status", "exit 0 · 0.4s\n M src/x.py", origin="shell",
                                     complete=False)], None)
    p = build_prompt(store.session(SHELL))
    assert "This is a plain shell, not an AI agent" in p
    assert "you ran: git status" in p and "result: exit 0 · 0.4s" in p
    assert "(command still running)" in p
    assert "\n  agent:" not in p and "\n  (agent still working" not in p    # nothing called an agent's, only the note
    # an agent's prompt is untouched
    _session(store, LAP)
    store.upsert_turns(LAP, [_turn(0, "fix the parser", "Shall I split it?")], None)
    q = build_prompt(store.session(LAP))
    assert "you: fix the parser" in q and "agent: Shall I split it?" in q and "plain shell" not in q
