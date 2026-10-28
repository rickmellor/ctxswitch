import json
import os
from pathlib import Path

from ctxswitch.collector.gitinfo import repo_info
from ctxswitch.collector.liveness import proc_start, read_claude_registry
from ctxswitch.collector.state import CollectorState


def _fake_proc(tmp: Path, pid: int, start: str) -> Path:
    d = tmp / "proc" / str(pid); d.mkdir(parents=True)
    fields = ["0"] * 52
    fields[21] = start
    (d / "stat").write_text(f"{pid} (claude (code)) " + " ".join(fields[2:]) + "\n")
    return tmp / "proc"


def test_registry_live_vs_stale_vs_reused_pid(tmp_path):
    proc = _fake_proc(tmp_path, 100, "5555")
    _fake_proc(tmp_path, 300, "9999")           # pid reused by another process
    reg = tmp_path / "sessions"; reg.mkdir()
    base = {"version": "2.1.280", "peerProtocol": 1, "kind": "interactive"}
    (reg / "100.json").write_text(json.dumps({**base, "pid": 100, "sessionId": "s-live", "procStart": "5555",
                                               "status": "idle", "statusUpdatedAt": 1793217597130,
                                               "messagingSocketPath": "/run/user/1000/cc-socks/100.sock"}))
    (reg / "200.json").write_text(json.dumps({**base, "pid": 200, "sessionId": "s-dead", "procStart": "1", "status": "busy"}))
    (reg / "300.json").write_text(json.dumps({**base, "pid": 300, "sessionId": "s-reused", "procStart": "1234", "status": "busy"}))
    (reg / "bad.json").write_text("{not json")
    live = read_claude_registry(reg, "laptop", proc=str(proc))
    assert set(live) == {"laptop/claude/s-live"}
    l = live["laptop/claude/s-live"]
    assert l.status == "idle" and l.pid == 100 and l.socket.endswith("100.sock")
    assert l.status_since == "2026-10-28T19:59:57.130000Z"
    assert proc_start(100, str(proc)) == "5555" and proc_start(999, str(proc)) is None


def test_repo_info_plain_and_linked_worktree(tmp_path):
    repo = tmp_path / "repo"; (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text('[core]\n\tbare = false\n[remote "origin"]\n\turl = git@github.com:acme/demo.git\n')
    (repo / "src").mkdir()
    assert repo_info(str(repo / "src")) == (str(repo.resolve()), "git@github.com:acme/demo.git")
    wt = tmp_path / "wt"; wt.mkdir()
    gd = repo / ".git" / "worktrees" / "wt"; gd.mkdir(parents=True)
    (gd / "commondir").write_text("../..\n")
    (wt / ".git").write_text(f"gitdir: {gd}\n")
    assert repo_info(str(wt)) == (str(wt.resolve()), "git@github.com:acme/demo.git")
    assert repo_info(str(tmp_path / "nowhere")) == ("", "")


def test_state_files_and_outbox_roundtrip(tmp_path):
    st = CollectorState(tmp_path / "c.db")
    assert st.file("/x") is None
    st.save_file("/x", "claude", "h/claude/s", 1234, {"n": 1}, 5000, 1.5, 3)
    f = st.file("/x")
    assert f["offset"] == 1234 and f["parser_state"] == {"n": 1} and f["turns_emitted"] == 3 and not f["ended"]
    st.save_file("/x", "claude", "h/claude/s", 2000, None, 6000, 2.5, 4, ended=True)
    assert st.file("/x")["offset"] == 2000 and st.file("/x")["ended"]
    s1 = st.enqueue("turns", {"a": 1}); s2 = st.enqueue("liveness", {"b": 2})
    assert st.pending_count() == 2 and [p[0] for p in st.pending()] == [s1, s2]
    st.ack(s1)
    assert [p[1] for p in st.pending()] == ["liveness"]
    st.set("hub_seq", "42"); assert st.get("hub_seq") == "42" and st.get("nope", "d") == "d"
    st.close()
