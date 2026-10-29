"""Phase 2: ctx-hook spooling, the collector's spool drain, pi registry checks, and the hub's hook → ask logic."""
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ctxswitch import hook
from ctxswitch.collector import spool
from ctxswitch.collector.liveness import read_pi_registry
from ctxswitch.hub.hooks import HookApplier, summarize
from ctxswitch.hub.store import Store

KEY = "laptop/claude/s1"


def _claude(spool_dir, event, **kw):
    return hook.handle_claude({"hook_event_name": event, "session_id": "s1", "cwd": "/r", **kw}, spool_dir)


def _records(spool_dir):
    return [json.loads(p.read_text()) for p in sorted(spool_dir.glob("*.json"))]


# -- ctx-hook ---------------------------------------------------------------------------------------------------
def test_hook_gates_tool_events_on_a_pending_permission(tmp_path):
    assert _claude(tmp_path, "PostToolUse", tool_name="Bash") is None            # nothing pending: not spooled
    assert _claude(tmp_path, "PermissionRequest", tool_name="Bash", tool_input={"command": "ls"})
    assert (tmp_path / ".pending" / "s1").exists()
    assert _claude(tmp_path, "PostToolUse", tool_name="Bash", tool_response={"stdout": "x" * 10_000})
    assert _claude(tmp_path, "Stop", stop_hook_active=False)
    assert not (tmp_path / ".pending" / "s1").exists()
    assert _claude(tmp_path, "PostToolUse", tool_name="Bash") is None
    recs = _records(tmp_path)
    assert [r["event"] for r in recs] == ["PermissionRequest", "PostToolUse", "Stop"]
    assert recs[0]["data"] == {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/r"}
    assert "tool_response" not in recs[1]["data"]                                   # not in KEEP: never shipped
    assert all(r["harness"] == "claude" and r["session_id"] == "s1" for r in recs)


def test_hook_notification_marks_pending_and_trims(tmp_path):
    _claude(tmp_path, "Notification", notification_type="permission_prompt", message="m" * 1000)
    assert (tmp_path / ".pending" / "s1").exists()
    (r,) = _records(tmp_path)
    assert len(r["data"]["message"]) == hook.MAX_STR and r["data"]["message"].endswith("…")


def test_hook_rejects_unknown_events_and_bad_ids(tmp_path):
    assert hook.handle_claude({"hook_event_name": "PreToolUse", "session_id": "s1"}, tmp_path) is None
    assert hook.handle_claude({"hook_event_name": "Stop", "session_id": "../x"}, tmp_path) is None
    assert hook.handle_claude({"hook_event_name": "Stop"}, tmp_path) is None
    assert _records(tmp_path) == []


def test_hook_main_never_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("CTXSWITCH_SPOOL", str(tmp_path))
    for raw in ("{not json", "[]", "", json.dumps({"hook_event_name": "Stop", "session_id": "s1"})):
        monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
        assert hook.main(["claude"]) == 0
    assert [r["event"] for r in _records(tmp_path)] == ["Stop"]
    (tmp_path / "blocker").write_text("")
    monkeypatch.setenv("CTXSWITCH_SPOOL", str(tmp_path / "blocker" / "sub"))       # unwritable spool
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"hook_event_name": "Stop", "session_id": "s1"})))
    assert hook.main(["claude"]) == 0


# -- collector spool drain --------------------------------------------------------------------------------------
def test_drain_orders_queues_deletes_and_quarantines(tmp_path):
    for i in (3, 1, 2):
        hook.write_event(tmp_path, "claude", f"s{i}", "Stop", {})
    (tmp_path / "5-1.json").write_text("{half")
    (tmp_path / ".9-1.json").write_text("{}")                                       # still being written
    out = []
    assert spool.drain(tmp_path, lambda k, p: out.append((k, p))) == 3
    assert [p["session_id"] for _, p in out] == ["s3", "s1", "s2"]                   # write order, not name order of sid
    assert all(k == "hook" and p["name"].endswith(".json") for k, p in out)
    assert sorted(x.name for x in tmp_path.iterdir()) == [".9-1.json", "bad"]
    assert (tmp_path / "bad" / "5-1.json").exists()


# -- pi registry ------------------------------------------------------------------------------------------------
def test_pi_registry_checks_proc_start(tmp_path):
    proc = tmp_path / "proc"
    for pid, start in ((10, "777"), (20, "888")):
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "stat").write_text(f"{pid} (node) " + " ".join(["0"] * 19 + [start] + ["0"] * 30))
    reg = tmp_path / "ctxswitch"; reg.mkdir()
    (reg / "pi-10.json").write_text(json.dumps({"pid": 10, "procStart": "777", "sessionId": "a", "status": "busy"}))
    (reg / "pi-20.json").write_text(json.dumps({"pid": 20, "procStart": "1", "sessionId": "b", "status": "idle"}))  # reused
    (reg / "pi-30.json").write_text(json.dumps({"pid": 30, "sessionId": "c", "status": "idle"}))                  # gone
    live = read_pi_registry(tmp_path, "laptop", proc=str(proc))
    assert set(live) == {"laptop/pi/a"} and live["laptop/pi/a"].status == "busy"


# -- hub ---------------------------------------------------------------------------------------------------------
class Feed:
    def __init__(self, tmp_path):
        self.store = Store(tmp_path / "h.db")
        self.store.upsert_session({"key": KEY, "host_id": "laptop", "harness": "claude", "session_id": "s1",
                                   "last_activity": "2026-10-28T10:00:00Z"})
        self.store.set_liveness("laptop", [{"key": KEY, "status": "busy"}])
        self.hooks, self.n = HookApplier(self.store), 0

    def __call__(self, event, sec=0, harness="claude", sid="s1", name=None, **data):
        self.n += 1
        p = {"harness": harness, "session_id": sid, "name": name or f"{self.n:04d}-1.json", "event": event,
             "ts": f"2026-10-28T10:00:{sec:02d}Z", "data": data}
        return self.hooks.apply("laptop", p)

    def asks(self, key=KEY):
        return [dict(a) for a in self.store.db.execute("SELECT * FROM asks WHERE key=? ORDER BY asked_at, ref", (key,))]

    def attention(self):
        return [(a["kind"], a["text"]) for a in self.store.attention() if a["item"] == "ask"]


def test_permission_request_then_allowed(tmp_path):
    f = Feed(tmp_path)
    f("PermissionRequest", 1, tool_name="Bash", tool_input={"command": "rm -rf build", "description": "clean"})
    f("Notification", 2, notification_type="permission_prompt", message="Claude needs your permission to use Bash")
    assert f.attention() == [("permission", "allow Bash: rm -rf build")]               # the Notification deduped
    f("PostToolUse", 9, tool_name="Bash")
    (a,) = f.asks()
    assert a["answered"] == 1 and a["answer"] == "allowed" and a["answered_at"] == "2026-10-28T10:00:09Z"
    assert f.attention() == []


def test_notification_first_is_upgraded_by_the_request(tmp_path):
    f = Feed(tmp_path)
    f("Notification", 1, notification_type="permission_prompt", message="Claude needs your permission to use mcp__db__qXy")
    f("PermissionRequest", 1, tool_name="mcp__db__qXy", tool_input={"query": "select 1"})
    assert f.attention() == [("permission", "allow mcp__db__qXy: select 1")]
    f("PostToolUse", 3, tool_name="mcp__db__q_y")                                      # '_' must not match the X
    assert len(f.attention()) == 1
    f("PermissionDenied", 4, tool_name="mcp__db__qXy", reason="user said no")
    assert f.asks()[0]["answer"] == "denied: user said no"


def test_notification_alone_and_tool_resolution_order(tmp_path):
    f = Feed(tmp_path)
    f("Notification", 1, notification_type="permission_prompt", message="needs permission")
    f("PostToolUse", 2, tool_name="Read")                                              # no tool known: resolves it
    assert f.asks()[0]["answer"] == "allowed"
    f("PermissionRequest", 40, tool_name="Bash", tool_input={"command": "a"})
    f("PermissionRequest", 41, tool_name="Bash", tool_input={"command": "b"})
    f("PostToolUseFailure", 42, tool_name="Bash")
    assert [(a["text"], a["answer"]) for a in f.asks()[1:]] == [("allow Bash: a", "allowed (tool failed)"), ("allow Bash: b", None)]


def test_turn_end_closes_and_transcript_tools_are_skipped(tmp_path):
    f = Feed(tmp_path)
    f("PermissionRequest", 1, tool_name="AskUserQuestion", tool_input={"questions": []})
    f("PermissionRequest", 1, tool_name="ExitPlanMode", tool_input={"plan": "x"})
    f("PermissionRequest", 2, tool_name="WebFetch", tool_input={"url": "https://e.x"})
    f("Notification", 3, notification_type="elicitation_dialog", message="server wants a token")
    f("Notification", 4, notification_type="idle_prompt", message="waiting")
    assert f.attention() == [("permission", "allow WebFetch: https://e.x"), ("elicitation", "server wants a token")]
    f("UserPromptSubmit", 5, prompt="never mind")
    assert f.attention() == [] and {a["answer"] for a in f.asks()} == {"closed: new prompt"}


def test_elicitation_resolution_and_session_start_sources(tmp_path):
    f = Feed(tmp_path)
    f("Notification", 1, notification_type="elicitation_dialog", title="auth")
    f("Notification", 2, notification_type="elicitation_complete")
    assert f.asks()[0]["answer"] == "answered"
    f("PermissionRequest", 3, tool_name="Bash", tool_input={"command": "x"})
    f("SessionStart", 4, source="compact")
    assert len(f.attention()) == 1
    f("SessionStart", 5, source="resume")
    assert f.attention() == []


def test_replayed_events_apply_once(tmp_path):
    f = Feed(tmp_path)
    f("PermissionRequest", 1, name="a.json", tool_name="Bash", tool_input={"command": "1"})
    f("PostToolUse", 2, name="b.json", tool_name="Bash")
    f("PermissionRequest", 3, name="c.json", tool_name="Bash", tool_input={"command": "2"})
    change = f.store.change
    assert f("PostToolUse", 2, name="b.json", tool_name="Bash") is False             # replay of b
    assert f.store.change == change
    assert [a["answered"] for a in f.asks()] == [1, 0]                                # c is still open


def test_pi_prompts_and_ended_sessions(tmp_path):
    f = Feed(tmp_path)
    pk = "laptop/pi/p1"
    f.store.upsert_session({"key": pk, "host_id": "laptop", "harness": "pi", "session_id": "p1"})
    f.store.set_liveness("laptop", [{"key": KEY, "status": "busy"}, {"key": pk, "status": "busy"}])
    f("ui_prompt_start", 1, harness="pi", sid="p1", kind="confirm", title="Run rm -rf?")
    f("ui_prompt_start", 2, harness="pi", sid="p1", kind="input")
    assert f.attention() == [("prompt", "confirm: Run rm -rf?"), ("prompt", "input")]
    f("ui_prompt_end", 3, harness="pi", sid="p1", kind="confirm")
    assert f.attention() == [("prompt", "input")]
    f.store.set_liveness("laptop", [{"key": KEY, "status": "busy"}])                  # pi vanished from the registry
    s = f.store.session(pk)
    assert s["live_status"] == "ended" and f.attention() == []
    f("session_shutdown", 4, harness="pi", sid="p1", reason="quit")
    assert [a["answer"] for a in f.asks(pk)] == ["answered", "closed: session ended"]


def test_bad_hook_payloads_are_ignored(tmp_path):
    f = Feed(tmp_path)
    assert f.hooks.apply("laptop", {"harness": "codex", "session_id": "s1", "name": "x", "event": "Stop"}) is False
    assert f.hooks.apply("laptop", {"harness": "claude", "session_id": "", "name": "x", "event": "Stop"}) is False
    assert f("PermissionRequest", tool_name=None, tool_input="oops")
    assert f.attention() == [("permission", "allow tool")]


def test_summarize():
    assert summarize("Edit", {"file_path": "/a/b.py", "old_string": "x"}) == "allow Edit: /a/b.py"
    assert summarize("Weird", {"n": 1, "z": "  spaced\n out  "}) == "allow Weird: spaced out"
    assert summarize("Bash", {"command": "y" * 500}).endswith("…")


def test_hook_ingest_through_the_api(tmp_path):
    from starlette.testclient import TestClient
    from ctxswitch.hub.api import Hub
    hub = Hub(Store(tmp_path / "h.db"))
    c = TestClient(hub.app())
    ev = {"harness": "claude", "session_id": "s1", "name": "1-1.json", "event": "PermissionRequest",
          "ts": "2026-10-28T10:00:00Z", "data": {"tool_name": "Bash", "tool_input": {"command": "ls"}}}
    r = c.post("/v1/ingest", json={"host_id": "laptop", "events": [{"seq": 1, "kind": "hook", "payload": ev}]})
    assert r.json()["applied"] == 1
    s = hub.store.session(KEY)                                                         # placeholder-free: asks still stored
    assert s is None and len(hub.store.db.execute("SELECT * FROM asks WHERE key=?", (KEY,)).fetchall()) == 1
    h = c.get("/v1/health").json()
    assert h["auth"] is True and h["recap"] is None


# -- pi extension, end to end -----------------------------------------------------------------------------------
@pytest.mark.skipif(not shutil.which("pi"), reason="pi not installed")
def test_pi_extension_registers_and_spools(tmp_path):
    ext = Path(hook.__file__).parent / "pi_ext" / "ctxswitch.ts"
    env = {**os.environ, "PI_CODING_AGENT_DIR": str(tmp_path / "agent"), "PI_CODING_AGENT_SESSION_DIR": str(tmp_path / "s"),
           "CTXSWITCH_RUNTIME": str(tmp_path / "run"), "CTXSWITCH_SPOOL": str(tmp_path / "spool"),
           "PI_OFFLINE": "1", "PI_SKIP_VERSION_CHECK": "1"}
    r = subprocess.run(["pi", "--mode", "rpc", "-e", str(ext)], stdin=subprocess.DEVNULL, env=env,
                       capture_output=True, text=True, timeout=45)
    assert r.returncode == 0, r.stderr[-2000:]
    recs = _records(tmp_path / "spool")
    assert [x["event"] for x in recs] == ["session_start", "session_shutdown"]
    assert recs[0]["harness"] == "pi" and recs[0]["data"]["reason"] == "startup"
    assert list((tmp_path / "run" / "ctxswitch").glob("pi-*.json")) == []              # unregistered on quit


def test_claude_waiting_status_is_live_and_needs_attention(tmp_path):
    from test_liveness_gitinfo_state import _fake_proc
    from ctxswitch.collector.liveness import read_claude_registry
    proc = _fake_proc(tmp_path, 100, "5")
    reg = tmp_path / "sessions"; reg.mkdir()
    (reg / "100.json").write_text(json.dumps({"pid": 100, "sessionId": "w1", "procStart": "5", "status": "waiting",
                                               "waitingFor": "permission prompt", "statusUpdatedAt": 1793241496121}))
    live = read_claude_registry(reg, "laptop", proc=str(proc))
    assert live["laptop/claude/w1"].status == "waiting"
    store = Store(tmp_path / "h.db")
    store.set_liveness("laptop", [live["laptop/claude/w1"].to_dict()])
    att = store.attention()
    assert [(a["key"], a["item"]) for a in att] == [("laptop/claude/w1", "waiting")]
    assert [s["key"] for s in store.sessions(include_ended=False)] == ["laptop/claude/w1"]


def test_idle_claude_closes_dismissed_hook_asks(tmp_path):
    f = Feed(tmp_path)
    f("PermissionRequest", 10, tool_name="Bash", tool_input={"command": "x"})
    f.store.set_liveness("laptop", [{"key": KEY, "status": "waiting", "status_since": "2026-10-28T10:00:10.100Z"}])
    f.store.set_liveness("laptop", [{"key": KEY, "status": "idle", "status_since": "2026-10-28T10:00:09Z"}])  # stale: older
    assert len(f.attention()) == 1
    f.store.set_liveness("laptop", [{"key": KEY, "status": "idle", "status_since": "2026-10-28T10:00:20Z"}])  # Esc'd
    assert f.attention() == [] and f.asks()[0]["answer"] == "closed: dismissed"
