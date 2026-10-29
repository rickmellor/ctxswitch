"""Windows-native Claude read from WSL: path mapping, tasklist liveness, polled roots, install/doctor, resume hint."""
import json
import os
import time
from pathlib import Path

from claude_rows import S, assistant, text, user

from ctxswitch.client import install as ins
from ctxswitch.client import launch
from ctxswitch.collector.indexer import Indexer
from ctxswitch.collector.liveness import read_claude_win_registry
from ctxswitch.collector.main import Collector
from ctxswitch.collector.state import CollectorState
from ctxswitch.common.config import CollectorConfig, Config
from ctxswitch.common.wsl import wsl_path

WIN_CWD = "C:\\Users\\me\\repos\\keeb-cheat"


def test_wsl_path():
    assert wsl_path(WIN_CWD) == "/mnt/c/Users/me/repos/keeb-cheat"
    assert wsl_path("D:/x/y") == "/mnt/d/x/y" and wsl_path("C:\\") == "/mnt/c/"
    assert wsl_path("\\\\wsl.localhost\\Ubuntu-24.04\\home\\rick") == "/home/rick"
    assert wsl_path("\\\\wsl$\\Ubuntu\\tmp") == "/tmp"
    assert wsl_path("/home/rick") == "/home/rick" and wsl_path("\\\\server\\share") == "\\\\server\\share"


def _reg(d: Path, pid: int, sid: str, status="busy"):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": sid, "status": status, "procStart": "x"}))


def test_win_registry_uses_windows_pids_and_only_asks_when_needed(tmp_path):
    calls = []
    pids = lambda: calls.append(1) or {10, 99}
    assert read_claude_win_registry(tmp_path / "sessions", "laptop", pids) == {}        # no dir: tasklist not run
    (tmp_path / "sessions").mkdir()
    assert read_claude_win_registry(tmp_path / "sessions", "laptop", pids) == {} and calls == []
    _reg(tmp_path / "sessions", 10, "w1", "waiting")
    _reg(tmp_path / "sessions", 20, "w2")                                                 # stale file: pid gone
    live = read_claude_win_registry(tmp_path / "sessions", "laptop", pids)
    assert calls == [1] and set(live) == {"laptop/claude-win/w1"} and live["laptop/claude-win/w1"].status == "waiting"
    assert read_claude_win_registry(tmp_path / "sessions", "laptop", lambda: None) is None  # unknown, not "all ended"


def _ix(tmp_path, seen):
    win = tmp_path / "win" / ".claude" / "projects"
    (win / "C--Users-me-repos-keeb-cheat").mkdir(parents=True)
    cfg = CollectorConfig(claude_roots=[str(tmp_path / "claude")], pi_roots=[], claude_win_roots=[str(win)], backfill_days=30)
    events = []
    ix = Indexer(cfg, "laptop", CollectorState(tmp_path / "c.db"), lambda k, p: events.append((k, p)))
    return win, ix, events


def test_poll_processes_only_changed_windows_transcripts(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr("ctxswitch.collector.indexer.repo_info", lambda cwd: seen.append(cwd) or ("", ""))
    win, ix, events = _ix(tmp_path, seen)
    f = win / "C--Users-me-repos-keeb-cheat" / f"{S}.jsonl"
    rows = [user("hi", "2026-10-28T10:00:00Z", cwd=WIN_CWD), assistant([text("hello")], "2026-10-28T10:00:01Z")]
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    old = win / "C--Users-me-repos-keeb-cheat" / "22222222-2222-4333-8444-555555555555.jsonl"
    old.write_text(json.dumps(user("ancient", "2025-01-01T00:00:00Z", cwd=WIN_CWD)) + "\n")
    os.utime(old, (time.time() - 90 * 86400,) * 2)                                         # past backfill, never seen
    assert ix.poll("claude") == 0 and ix.poll("claude-win") == 1
    (sess,) = [p for k, p in events if k == "session"]
    assert sess["key"] == f"laptop/claude-win/{S}" and sess["harness"] == "claude-win" and sess["cwd"] == WIN_CWD
    assert seen == ["/mnt/c/Users/me/repos/keeb-cheat"]                                    # git asked about the WSL path
    assert ix.poll("claude-win") == 0                                                      # unchanged: skipped
    with open(f, "a") as fh:
        fh.write(json.dumps(user("more", "2026-10-28T10:00:02Z", cwd=WIN_CWD)) + "\n")
    assert ix.poll("claude-win") == 1


def test_collector_polls_windows_off_the_watcher(tmp_path, monkeypatch):
    cfg = Config()
    cfg.collector.queue_path = str(tmp_path / "q.db"); cfg.collector.host_id = "laptop"
    cfg.collector.claude_sessions_dir = str(tmp_path / "none")
    cfg.collector.claude_win_roots = [str(tmp_path / "win" / ".claude" / "projects")]
    monkeypatch.setattr("ctxswitch.collector.main.runtime_dir", lambda: tmp_path / "rt")
    c = Collector(cfg)
    assert c.win_sessions_dirs() == [tmp_path / "win" / ".claude" / "sessions"]
    _reg(tmp_path / "win" / ".claude" / "sessions", 10, "w1")
    monkeypatch.setattr("ctxswitch.collector.main.windows_pids", lambda: {10})
    c.poll_windows(c.read_windows_liveness())
    assert c.ix.live_keys == {"laptop/claude-win/w1"}
    c.poll_windows(None)                                                                   # tasklist failed: keep last
    assert c.ix.live_keys == {"laptop/claude-win/w1"}
    monkeypatch.setattr("ctxswitch.collector.main.windows_pids", lambda: set())
    c.poll_windows(c.read_windows_liveness())
    assert c.ix.live_keys == set()


def test_install_detects_windows_claude_once(tmp_path, monkeypatch):
    import tomllib
    monkeypatch.setenv("HOME", str(tmp_path)); monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CTXSWITCH_CONFIG", raising=False)
    root = tmp_path / "winhome" / ".claude" / "projects"; root.mkdir(parents=True)
    monkeypatch.setattr(ins, "windows_claude_root", lambda: root)
    ins.write_config(None, None, False, lambda m: None)
    p = tmp_path / ".config/ctxswitch/config.toml"
    assert tomllib.loads(p.read_text())["collector"]["claude_win_roots"] == [str(root)]
    import tomli_w
    d = tomllib.loads(p.read_text()); d["collector"]["claude_win_roots"] = []
    p.write_text(tomli_w.dumps(d))
    ins.write_config(None, None, False, lambda m: None)                                     # an explicit [] is an opt-out
    assert tomllib.loads(p.read_text())["collector"]["claude_win_roots"] == []


def test_doctor_line_and_resume_hint(tmp_path, monkeypatch):
    root = tmp_path / "projects"; (root / "C--x").mkdir(parents=True); (root / "C--x" / "a.jsonl").write_text("")
    monkeypatch.setattr("ctxswitch.common.wsl.windows_pids", lambda: {1})
    assert ins._check_claude_win(root).status == "✓" and "1 transcripts" in ins._check_claude_win(root).detail
    monkeypatch.setattr("ctxswitch.common.wsl.windows_pids", lambda: None)
    assert ins._check_claude_win(root).status == "!"
    assert ins._check_claude_win(tmp_path / "gone").status == "✗"
    s = {"harness": "claude-win", "session_id": S, "cwd": WIN_CWD, "host_id": "laptop"}
    hint = f"Windows session: in a Windows terminal, cd {WIN_CWD} && claude --resume {S}"
    assert launch.open_tab(s, "laptop") == hint
    from ctxswitch.client import tmux
    monkeypatch.setattr(tmux, "in_tmux", lambda: True)
    monkeypatch.setattr(tmux, "panes", lambda: [])
    monkeypatch.setattr(tmux, "viewer_pane", lambda ps: object())
    assert tmux.show(s) == hint
