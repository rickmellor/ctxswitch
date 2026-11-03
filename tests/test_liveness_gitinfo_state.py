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


def test_registry_user_name_survives_to_the_hub_title(tmp_path):
    # a resumed session's /rename name lives in the registry before the transcript has a custom-title
    from ctxswitch.hub.store import Store
    proc = tmp_path / "proc"
    reg = tmp_path / "sessions"; reg.mkdir()
    names = {"user": ("Netflow", "user"), "legacy": ("brownfield", None), "derived": ("rick-cd", "derived"),
             "auto": ("zephyr-plan", "auto"), "blank": ("  ", "user")}
    for i, (sid, (name, src)) in enumerate(names.items(), start=100):
        _fake_proc(tmp_path, i, str(i))
        (reg / f"{i}.json").write_text(json.dumps({"pid": i, "sessionId": sid, "procStart": str(i), "status": "idle",
                                                   "name": name, **({"nameSource": src} if src else {})}))
    live = read_claude_registry(reg, "h", proc=str(proc))
    assert {k.rsplit("/", 1)[1]: l.name for k, l in live.items()} == \
        {"user": "Netflow", "legacy": "brownfield", "derived": None, "auto": None, "blank": None}

    store = Store(tmp_path / "hub.db")
    key = "h/claude/user"
    store.upsert_session({"key": key, "host_id": "h", "harness": "claude", "session_id": "user",
                          "title": "Ubiquiti Dream Wall network monitoring"})
    store.set_liveness("h", [live[key].to_dict()])
    assert store.session(key)["title"] == "Netflow"
    assert [s["title"] for s in store.sessions("h")] == ["Netflow"]     # listed (live, no turns) under the name
    store.set_liveness("h", [])                                         # exited: back to the transcript's title
    assert store.session(key)["title"] == "Ubiquiti Dream Wall network monitoring"


def test_a_window_parked_into_a_background_job_is_reported_as_the_job_shown_by_the_window(tmp_path):
    """Claude Code can park a window's conversation into a background job: the job forks it under a new session id
    (its entry: kind bg, jobId J), the window's entry keeps the old id with parkedJobId J and only shows the job.
    One conversation: report the job, with the window's process as the one showing it, and not the window's."""
    proc = _fake_proc(tmp_path, 100, "11")
    _fake_proc(tmp_path, 200, "22"); _fake_proc(tmp_path, 300, "33")
    reg = tmp_path / "sessions"; reg.mkdir()
    (reg / "100.json").write_text(json.dumps({"pid": 100, "sessionId": "s-window", "procStart": "11", "status": "busy",
                                               "kind": "interactive", "parkedJobId": "7c4a8b08", "tmux": "ctx:@1.%2"}))
    (reg / "200.json").write_text(json.dumps({"pid": 200, "sessionId": "s-job", "procStart": "22", "status": "busy",
                                               "kind": "bg", "jobId": "7c4a8b08"}))
    (reg / "300.json").write_text(json.dumps({"pid": 300, "sessionId": "s-other", "procStart": "33", "status": "idle",
                                               "kind": "interactive", "parkedJobId": "nosuchjob"}))
    live = read_claude_registry(reg, "laptop", proc=str(proc))
    assert set(live) == {"laptop/claude/s-job", "laptop/claude/s-other"}       # the frozen window session is gone
    assert live["laptop/claude/s-job"].pid == 200 and live["laptop/claude/s-job"].via_pid == 100
    assert live["laptop/claude/s-other"].via_pid is None                         # its job isn't running: it stays
