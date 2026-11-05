"""tmux_at: where each live session runs in a tmux on its OWN host ("<session>:<window_id>.<pane_id>"), so a board
on another host can attach to it over ssh. The Claude registry names it; everything else is found by walking up
/proc to the tmux pane whose pane_pid is an ancestor of its pid."""
import json
import sqlite3
import subprocess
from pathlib import Path

from ctxswitch.collector import liveness as L
from ctxswitch.collector.liveness import read_claude_registry, tmux_panes
from ctxswitch.common.schema import Liveness
from ctxswitch.hub.store import Store


def _fake_proc(tmp: Path, pid: int, start: str, ppid: int = 1) -> Path:
    d = tmp / "proc" / str(pid); d.mkdir(parents=True)
    fields = ["0"] * 52
    fields[3] = str(ppid)                    # stat field 4: the parent pid (fields[2] would be the state letter)
    fields[21] = start                       # field 22: the start time the registry's procStart must match
    (d / "stat").write_text(f"{pid} (claude (code)) " + " ".join(fields[2:]) + "\n")
    return tmp / "proc"


# -- the registry's own "tmux" field -------------------------------------------------------------------------------

def test_a_claude_registry_tmux_field_is_the_sessions_tmux_at(tmp_path):
    reg = tmp_path / "sessions"; reg.mkdir()
    for pid, tmux in ((100, "ctx:@1.%2"), (200, ""), (300, "no-delimiter"), (400, 5)):
        proc = _fake_proc(tmp_path, pid, str(pid))
        body = {"pid": pid, "sessionId": f"s{pid}", "procStart": str(pid), "status": "idle"}
        if tmux is not None:
            body["tmux"] = tmux
        (reg / f"{pid}.json").write_text(json.dumps(body))
    live = read_claude_registry(reg, "laptop", proc=str(proc))
    assert live["laptop/claude/s100"].tmux_at == "ctx:@1.%2"
    assert [live[f"laptop/claude/s{p}"].tmux_at for p in (200, 300, 400)] == [None, None, None]


def test_a_parked_window_names_the_pane_its_background_job_shows_in(tmp_path):
    proc = _fake_proc(tmp_path, 100, "11"); _fake_proc(tmp_path, 200, "22"); _fake_proc(tmp_path, 300, "33")
    reg = tmp_path / "sessions"; reg.mkdir()
    (reg / "100.json").write_text(json.dumps({"pid": 100, "sessionId": "s-window", "procStart": "11", "status": "busy",
                                              "kind": "interactive", "parkedJobId": "7c4a8b08", "tmux": "ctx:@1.%2"}))
    (reg / "200.json").write_text(json.dumps({"pid": 200, "sessionId": "s-job", "procStart": "22", "status": "busy",
                                              "kind": "bg", "jobId": "7c4a8b08"}))
    (reg / "300.json").write_text(json.dumps({"pid": 300, "sessionId": "s-other", "procStart": "33", "status": "idle",
                                              "kind": "interactive", "parkedJobId": "nosuchjob"}))
    live = read_claude_registry(reg, "laptop", proc=str(proc))
    assert live["laptop/claude/s-job"].tmux_at == "ctx:@1.%2"      # the job runs headless: the window's pane shows it
    assert live["laptop/claude/s-other"].tmux_at is None           # never parked, and it names no tmux of its own


# -- the pane walk --------------------------------------------------------------------------------------------------

def test_fill_tmux_at_walks_up_to_the_pane_that_hosts_the_pid(tmp_path):
    proc = _fake_proc(tmp_path, 300, "1", 200)                     # 300 → 200 → 100, and 100 is a pane's shell
    _fake_proc(tmp_path, 200, "2", 100); _fake_proc(tmp_path, 100, "3", 1)
    _fake_proc(tmp_path, 400, "4", 1)                              # under init: in no pane
    panes = [("ctx", "@1", "%2", 100), ("other", "@7", "%9", 500)]
    mine = Liveness(key="h/pi/mine", status="busy", pid=300)
    nowhere = Liveness(key="h/shell/nowhere", status="idle", pid=400)
    named = Liveness(key="h/claude/named", status="busy", pid=300, tmux_at="ctx:@1.%2")
    headless = Liveness(key="h/agy/headless", status="busy", pid=None)
    L.fill_tmux_at([mine, nowhere, named, headless], panes, proc=str(proc))
    assert mine.tmux_at == "ctx:@1.%2"                             # the pane whose shell is its grandparent
    assert nowhere.tmux_at is None and named.tmux_at == "ctx:@1.%2" and headless.tmux_at is None


def test_fill_tmux_at_stops_at_init_and_at_a_pid_that_is_the_pane(tmp_path):
    proc = _fake_proc(tmp_path, 700, "1", 700)                     # a stat that names itself: the walk can't loop
    L.fill_tmux_at([i := Liveness(key="h/pi/a", status="busy", pid=700)], [("ctx", "@3", "%8", 100)], proc=str(proc))
    assert i.tmux_at is None
    proc = _fake_proc(tmp_path, 800, "1", 1)
    L.fill_tmux_at([j := Liveness(key="h/pi/b", status="busy", pid=800)], [("ctx", "@3", "%8", 800)], proc=str(proc))
    assert j.tmux_at == "ctx:@3.%8"                                # the session is the pane's own process


# -- one list-panes call --------------------------------------------------------------------------------------------

class _Done:
    def __init__(self, out="", rc=0):
        self.stdout, self.returncode = out, rc


def test_tmux_panes_parses_one_list_panes_call():
    seen = []

    def run(cmd, **kw):
        seen.append((cmd, kw))
        return _Done("ctx\t@1\t%2\t100\nctx\t@3\t%8\t300\nbad line\nx\t@9\t%9\tnotanint\n")

    assert tmux_panes(run=run) == [("ctx", "@1", "%2", 100), ("ctx", "@3", "%8", 300)]
    cmd, kw = seen[0]
    assert cmd[:3] == ["tmux", "list-panes", "-a"] and "#{pane_pid}" in cmd[-1]
    assert kw["capture_output"] and kw["text"] and kw["timeout"] == 3


def test_tmux_panes_reads_no_panes_when_tmux_is_missing_or_the_server_is_not_running():
    assert tmux_panes(run=lambda cmd, **kw: _Done(rc=1)) == []
    assert tmux_panes(run=lambda cmd, **kw: (_ for _ in ()).throw(OSError("no tmux"))) == []
    assert tmux_panes(run=lambda cmd, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, 3))) == []


def test_poll_liveness_lists_the_panes_only_when_something_still_needs_them(tmp_path, monkeypatch):
    from ctxswitch.collector import main as M
    from ctxswitch.common.config import Config
    cfg = Config()
    cfg.collector.host_id, cfg.collector.queue_path = "work", str(tmp_path / "q.db")
    cfg.collector.claude_sessions_dir = str(tmp_path / "sessions")
    monkeypatch.setattr(M, "runtime_dir", lambda: tmp_path / "rt")
    reg = tmp_path / "sessions"; reg.mkdir()
    proc = _fake_proc(tmp_path, 100, "11"); _fake_proc(tmp_path, 200, "22")
    (reg / "100.json").write_text(json.dumps({"pid": 100, "sessionId": "s-named", "procStart": "11", "status": "idle",
                                              "tmux": "ctx:@1.%2"}))
    (reg / "200.json").write_text(json.dumps({"pid": 200, "sessionId": "s-plain", "procStart": "22", "status": "idle"}))
    monkeypatch.setattr(L, "proc_start", lambda pid, p="/proc": {100: "11", 200: "22"}.get(pid))   # both pids alive
    calls = []
    monkeypatch.setattr(M, "tmux_panes", lambda: (calls.append(1), [("ctx", "@5", "%6", 200)])[1])
    col = M.Collector(cfg)
    col.win_live = {"work/claude-win/w1": Liveness(key="work/claude-win/w1", status="idle", pid=200)}
    col.poll_liveness()
    assert calls == [1]                                            # s-plain has a pid and no tmux_at
    reports = [p[2] for p in col.state.pending() if p[1] == "liveness"]
    live = {s["key"]: s for r in reports for s in r["sessions"]}
    assert live["work/claude/s-named"]["tmux_at"] == "ctx:@1.%2"   # from its own registry entry: no walk needed
    assert live["work/claude/s-plain"]["tmux_at"] == "ctx:@5.%6"   # the pane walk
    assert live["work/claude-win/w1"]["tmux_at"] is None            # a Windows pid is no pane pid on this host
    calls.clear()
    col.win_live = {}
    (reg / "200.json").unlink()
    col.poll_liveness()
    assert calls == []                                             # every live session named its own tmux


# -- the hub's side -------------------------------------------------------------------------------------------------

def test_the_hub_keeps_tmux_at_with_the_live_data_and_clears_it_when_the_session_ends(tmp_path):
    store = Store(tmp_path / "hub.db")
    key = "work/claude/s1"
    store.upsert_session({"key": key, "host_id": "work", "harness": "claude", "session_id": "s1", "title": "T"})
    store.set_liveness("work", [{"key": key, "status": "busy", "pid": 100, "tmux_at": "ctx:@1.%2"}])
    assert store.session(key)["tmux_at"] == "ctx:@1.%2"
    assert [s["tmux_at"] for s in store.sessions("work")] == ["ctx:@1.%2"]
    store.set_liveness("work", [{"key": key, "status": "idle", "pid": 100}])
    assert store.session(key)["tmux_at"] is None                   # a snapshot that stops naming it clears it
    store.set_liveness("work", [{"key": key, "status": "busy", "pid": 100, "tmux_at": "ctx:@1.%2"}])
    store.set_liveness("work", [])                                 # ended
    s = store.session(key)
    assert s["live_status"] == "ended" and s["tmux_at"] is None


def test_a_session_that_has_no_transcript_row_yet_keeps_its_tmux_at(tmp_path):
    store = Store(tmp_path / "hub.db")
    key = "work/claude/s-new"
    store.set_liveness("work", [{"key": key, "status": "busy", "pid": 100, "tmux_at": "ctx:@1.%2"}])
    assert store.session(key)["tmux_at"] == "ctx:@1.%2"


def test_an_older_database_gains_the_tmux_at_column(tmp_path):
    path = tmp_path / "old.db"
    Store(path).db.execute("ALTER TABLE sessions DROP COLUMN tmux_at")     # a db written before tmux_at existed
    store = Store(path)
    assert "tmux_at" in {r[1] for r in store.db.execute("PRAGMA table_info(sessions)")}
    store.set_liveness("work", [{"key": "work/claude/s1", "status": "busy", "pid": 100, "tmux_at": "ctx:@1.%2"}])
    assert store.session("work/claude/s1")["tmux_at"] == "ctx:@1.%2"


def test_a_grouped_pane_is_placed_in_its_own_session_not_a_boards_view_of_it(tmp_path):
    from ctxswitch.collector.liveness import fill_tmux_at
    from ctxswitch.common.schema import Liveness
    for order in ([("ctxview-d82424", "@0", "%0", 500), ("ctx", "@0", "%0", 500)],
                  [("ctx", "@0", "%0", 500), ("ctxview-d82424", "@0", "%0", 500)]):   # whichever tmux lists first
        it = Liveness(key="gpu-host/shell/x", status="idle", pid=500)
        fill_tmux_at([it], order, proc=str(tmp_path))
        assert it.tmux_at == "ctx:@0.%0", order
