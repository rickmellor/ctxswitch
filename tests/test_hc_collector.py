"""herdr-core (``hc``) through the collector: which terminal each live session runs in (hc_at), the agent state the
core sees (its OSC 7501 status, richer than a registry file's busy/idle), the ctx.key label that keeps a mapping
across a restarted core, and ``hc events`` as a liveness source of its own.

Every ``hc`` call here is a fake ``run``: these tests never need herdr-core installed, and a host without it is the
normal case the code has to keep working on."""
import io
import json
import os
import subprocess

import pytest

from ctxswitch.collector import liveness as L
from ctxswitch.collector.liveness import HcLoop, hc_status_of, hc_terminals, fill_hc_at, label_sessions
from ctxswitch.common.schema import Liveness
from ctxswitch.hub.store import Store


@pytest.fixture(autouse=True)
def clean_hc_caches(tmp_path, monkeypatch):
    """The get-cache and the label memo live for the process: start every test with both empty. And no real hc in
    sight (an empty HOME, a PATH without it), so the fakes see the bare "hc" the code falls back to; the lookup
    itself has its own test below."""
    L._hc_gets.clear()
    L._hc_labelled.clear()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")


class _Done:
    def __init__(self, out="", rc=0):
        self.stdout, self.returncode = out, rc


def _fake_proc(tmp_path, pid: int, ppid: int = 1):
    """A /proc with just what the walk reads: stat field 4, the parent pid (comm holds a space, as a real one can)."""
    d = tmp_path / "proc" / str(pid)
    d.mkdir(parents=True)
    fields = ["0"] * 52
    fields[3] = str(ppid)
    (d / "stat").write_text(f"{pid} (claude (code)) " + " ".join(fields[2:]) + "\n")
    return tmp_path / "proc"


def _ls(terminals):
    return json.dumps({"hidden_finished": 0, "terminals": terminals})


def _rec(addr="t1", state="live", status=None, pid=None, labels=None, turn=0, title="", agent="claude-code",
         foreground=None):
    r = {"address": addr, "state": state, "turn": turn, "title": title, "name": title, "agent": agent,
         "labels": labels or {}, "status": status, "argv": ["/bin/sh"], "cwd": "/srv/app"}
    if pid is not None:
        r["pid"] = pid
    if foreground is not None:
        r["foreground"] = foreground
    return r


def _run(ls_rows=(), gets=None, rc=0, raises=None):
    """A fake hc: ``ls_rows`` for ``hc ls --json``, ``gets`` = {address: record} for ``hc get`` — and a log of every
    call, so a test can see what was asked for and how often."""
    calls = []

    def run(cmd, **kw):
        calls.append(list(cmd))
        if raises:
            raise raises
        if rc:
            return _Done(rc=rc)
        if cmd[:3] == ["hc", "ls", "--json"]:
            return _Done(_ls(list(ls_rows)))
        if cmd[:2] == ["hc", "get"]:
            rec = (gets or {}).get(cmd[2])
            return _Done(json.dumps(rec)) if rec else _Done("", rc=1)
        return _Done("{}")
    return run, calls


# -- hc_terminals ---------------------------------------------------------------------------------------------------

def test_hc_terminals_is_the_ls_rows_expanded_with_their_get_records():
    run, calls = _run(ls_rows=[{"address": "t1", "state": "live", "turn": 0},
                               {"address": "t2", "state": "finished", "turn": 3}],
                     gets={"t1": _rec("t1", pid=400, labels={"ctx.key": "laptop/claude/s1"})})
    assert hc_terminals(run=run) == [_rec("t1", pid=400, labels={"ctx.key": "laptop/claude/s1"})]
    assert calls == [["hc", "ls", "--json"], ["hc", "get", "t1", "--json"]]      # t2 finished: nothing to place


def test_hc_terminals_falls_back_to_the_ls_row_when_its_get_fails():
    row = {"address": "t1", "state": "live", "turn": 2, "agent": "pi"}
    run, _ = _run(ls_rows=[row])
    assert hc_terminals(run=run) == [row]


def test_hc_terminals_is_empty_when_hc_is_missing_or_answers_nothing():
    for bad in (lambda cmd, **kw: (_ for _ in ()).throw(OSError("no hc on PATH")),
                lambda cmd, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, 3)),
                lambda cmd, **kw: _Done(rc=1),                       # no core running on this host
                lambda cmd, **kw: _Done("not JSON")):
        assert hc_terminals(run=bad) == []


def test_hc_terminals_gets_a_terminal_only_once_the_core_says_it_moved():
    row = {"address": "t1", "state": "live", "turn": 1, "status": "working"}
    run, calls = _run(ls_rows=[row], gets={"t1": _rec("t1", pid=400, turn=1, status={"state": "working"})})
    for _ in range(3):
        assert len(hc_terminals(run=run)) == 1
    assert calls.count(["hc", "get", "t1", "--json"]) == 1           # (address, turn, status) unchanged between polls
    run2, calls2 = _run(ls_rows=[{**row, "turn": 2}], gets={"t1": _rec("t1", pid=400, turn=2)})
    assert len(hc_terminals(run=run2)) == 1
    assert calls2.count(["hc", "get", "t1", "--json"]) == 1          # its turn grew: the record is due again


def test_hc_terminals_forgets_a_terminal_the_core_no_longer_lists():
    row = {"address": "t1", "state": "live", "turn": 1}
    run, calls = _run(ls_rows=[row], gets={"t1": _rec("t1", pid=400, turn=1)})
    hc_terminals(run=run)
    hc_terminals(run=lambda cmd, **kw: _Done(_ls([{"address": "t9", "state": "live", "turn": 0}])))
    hc_terminals(run=run)
    assert calls.count(["hc", "get", "t1", "--json"]) == 2           # gone, then back: fetched again


# -- fill_hc_at -----------------------------------------------------------------------------------------------------

def test_fill_hc_at_gives_a_session_the_terminal_whose_process_hosts_it(tmp_path):
    proc = _fake_proc(tmp_path, 300, 200)                            # 300 → 200 → 1 (init)
    _fake_proc(tmp_path, 200, 1)
    mine = Liveness(key="laptop/claude/s1", status="busy", pid=300)
    nowhere = Liveness(key="laptop/pi/p1", status="idle", pid=999)
    headless = Liveness(key="laptop/shell/s", status="idle", pid=None)
    fill_hc_at([mine, nowhere, headless], [_rec("t1", pid=200)], proc=str(proc))
    assert mine.hc_at == "t1"                                        # the terminal whose shell is its parent
    assert nowhere.hc_at is None and headless.hc_at is None          # under no terminal; no pid to start from


def test_fill_hc_at_reads_a_headless_agents_own_pid_and_its_foreground(tmp_path):
    item = Liveness(key="laptop/claude/s2", status="busy", pid=777)
    fill_hc_at([item], [_rec("t2", pid=10, foreground={"pid": 777})], proc=str(tmp_path))
    assert item.hc_at == "t2"                                        # the terminal runs its own program: no walk
    other = Liveness(key="laptop/pi/p2", status="idle", pid=10)
    fill_hc_at([other], [_rec("t3", pid=10, foreground={"pid": 777})], proc=str(tmp_path))
    assert other.hc_at == "t3"                               # the terminal's own process, its shell long gone


def test_fill_hc_at_uses_the_ctx_key_label_and_leaves_a_placed_session_alone(tmp_path):
    proc = _fake_proc(tmp_path, 300, 1)
    by_label = Liveness(key="laptop/claude/s3", status="idle", pid=300)
    placed = Liveness(key="laptop/claude/s4", status="idle", pid=300, hc_at="t1")
    fill_hc_at([by_label, placed], [_rec("t4", pid=900, labels={"ctx.key": "laptop/claude/s3"}),
                                    _rec("t1", pid=300)], proc=str(proc))
    assert by_label.hc_at == "t4"                                    # labelled for this session: no walk needed
    assert placed.hc_at == "t1"                                      # already placed, by the walk or its registry


# -- label_sessions -------------------------------------------------------------------------------------------------

def test_label_sessions_labels_a_terminal_once_and_never_one_already_labelled():
    it = Liveness(key="laptop/claude/s1", status="idle", pid=1, hc_at="t1")
    other = Liveness(key="laptop/pi/p1", status="idle", pid=2, hc_at="t2")
    recs = [_rec("t1", pid=1), _rec("t2", pid=2, labels={"ctx.key": "laptop/pi/p1"})]
    run, calls = _run()
    for _ in range(3):
        label_sessions([it, other], recs, run=run)
    assert calls == [["hc", "label", "t1", "ctx.key=laptop/claude/s1"]]     # t2 already carries it


def test_label_sessions_leaves_a_terminal_it_does_not_know_about_alone():
    it = Liveness(key="laptop/claude/s1", status="idle", pid=1, hc_at="t7")
    run, calls = _run()
    label_sessions([it], [_rec("t1", pid=1)], run=run)
    assert calls == []                                               # t7 is in none of this poll's records


def test_label_sessions_does_not_overwrite_another_sessions_label():
    it = Liveness(key="laptop/claude/s1", status="idle", pid=1, hc_at="t1")
    run, calls = _run()
    label_sessions([it], [_rec("t1", pid=1, labels={"ctx.key": "gpu-host/claude/s9"})], run=run)
    assert calls == []


# -- the status mapping ---------------------------------------------------------------------------------------------

def test_the_herdr_status_maps_to_ctx_statuses():
    assert hc_status_of({"state": "working"}) == ("busy", None)
    assert hc_status_of({"state": "idle"}) == ("idle", None) and hc_status_of({"state": "done"}) == ("idle", None)
    assert hc_status_of({"state": "error"}) == ("unknown", None)
    assert hc_status_of(None) == (None, None)                        # never reported: it says nothing about itself
    assert hc_status_of({"state": "blocked", "kind": "question", "msg": "answer: Continue? (Yes · No)"}) == \
        ("waiting", {"kind": "question", "text": "answer: Continue? (Yes · No)"})
    assert hc_status_of({"state": "blocked", "kind": "permission", "msg": "edit src/a.py?"})[1]["kind"] == "permission"
    assert hc_status_of({"state": "blocked", "msg": "an unlabelled dialog"})[1] == \
        {"kind": "plain", "text": "an unlabelled dialog"}


# -- the events stream ----------------------------------------------------------------------------------------------

def _snap(terminals):
    return json.dumps({"channel": None, "seq": 1, "terminals": terminals})


def _ev(typ, addr, data):
    return json.dumps({"jsonrpc": "2.0", "method": "event",
                       "params": {"seq": 2, "type": typ, "terminal": addr, "data": data}})


def _loop(lines, host_id="laptop"):
    """An HcLoop whose ``hc events --json`` prints exactly these lines and ends: stream() runs it to EOF and the
    second pass stops the loop, so what it kept is the answer — no thread, no timing."""
    class _P:
        def __init__(self, out):
            self.stdout = out

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    started = []

    def popen(argv, **kw):
        started.append(argv)
        if len(started) > 1:
            loop.stop()
            return _P(io.StringIO(""))
        return _P(io.StringIO("".join(l if l.endswith("\n") else l + "\n" for l in lines)))

    loop = HcLoop(host_id, popen=popen, which=lambda _: "/usr/bin/hc", sleep=lambda s: None)
    loop.stream()
    assert started == [["/usr/bin/hc", "events", "--json"]] * 2, "the stream never ended by itself"   # the hc `which` found
    return loop


def test_the_events_stream_keeps_the_snapshot_and_the_status_turn_finished_and_removed_lines():
    loop = _loop([_snap([_rec("t1", pid=10, status={"app": "claude-code", "state": "working"}, turn=2),
                         _rec("t2", pid=20, agent="pi", turn=0)]),
                  _ev("status", "t1", {"status": {"app": "claude-code", "state": "blocked", "kind": "question",
                                                  "msg": "answer: Continue?"}, "records": {}, "reported": True}),
                  _ev("turn", "t1", {"state": "blocked", "turn": 3}),
                  _ev("finished", "t2", {"exit_code": 0, "reason": "exited", "signal": None, "finished_ms": 1}),
                  "not JSON at all",
                  _ev("foreground", "t1", {"pid": 99, "name": "claude"})])
    t1 = loop.state["t1"]
    assert (t1["state"], t1["kind"], t1["msg"], t1["app"]) == ("blocked", "question", "answer: Continue?", "claude-code")
    assert t1["turn"] == 3 and t1["reported"] and t1["pid"] == 10 and t1["title"] == "" and t1["ts"]
    assert "t2" not in loop.state                                    # finished: the core says it is gone


def test_the_events_stream_stops_caring_when_a_terminal_stops_reporting():
    loop = _loop([_ev("status", "t1", {"status": None, "records": {}, "reported": True}),
                  _ev("created", "t5", _rec("t5", pid=50, agent="codex", turn=0)),
                  _ev("removed", "t5", {"reason": "removed"})])
    assert loop.state["t1"]["state"] is None
    assert "t5" not in loop.state


def test_a_herdr_agent_with_no_session_of_its_own_becomes_one():
    loop = _loop([_snap([_rec("t3", pid=30, agent="codex", turn=4, title="refactor the hub",
                              status={"app": "codex", "state": "blocked", "kind": "permission",
                                      "msg": "run make test?"})])])
    live = {}
    loop.merge(live)
    it = live["laptop/herdr/t3"]
    assert (it.status, it.name, it.pid, it.hc_at) == ("waiting", "refactor the hub", 30, "t3")
    assert it.hc_ask == {"kind": "permission", "text": "run make test?"}
    assert it.status_since                                           # when this collector saw the change


def test_a_terminal_that_reports_no_status_is_not_a_session():
    loop = _loop([_snap([_rec("t4", pid=40, agent="sleep", turn=0)])])   # a plain command: it reports nothing
    live = {}
    loop.merge(live)
    assert live == {}


def test_a_mapped_session_takes_only_the_ask_and_never_a_downgrade():
    loop = _loop([_snap([_rec("t1", pid=10, status={"app": "claude-code", "state": "idle"}, turn=1),
                         _rec("t2", pid=20, status={"app": "claude-code", "state": "idle"}, turn=1),
                         _rec("t3", pid=30, status={"app": "claude-code", "state": "blocked", "kind": "question",
                                                    "msg": "answer: Continue?"}, turn=1)])])
    busy = Liveness(key="laptop/claude/busy", status="busy", pid=10, hc_at="t1")
    idle = Liveness(key="laptop/claude/idle", status="idle", pid=20, hc_at="t2")
    ask = Liveness(key="laptop/claude/ask", status="idle", pid=30, hc_at="t3")
    live = {i.key: i for i in (busy, idle, ask)}
    loop.merge(live)
    assert (busy.status, idle.status, ask.status) == ("busy", "idle", "waiting")     # blocked upgrades idle only
    assert ask.hc_ask == {"kind": "question", "text": "answer: Continue?"}
    assert busy.hc_ask is None and idle.hc_ask is None


def test_the_reader_restarts_with_a_backoff_and_stops_when_hc_is_gone():
    """A stream that dies (the core restarted, hc upgraded) comes back after 5 s, then 10, 20 … up to 60; a host
    with no hc on PATH runs no reader at all."""
    started, slept = [], []

    class _P:
        stdout = io.StringIO("")

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    def popen(argv, **kw):
        started.append(argv)
        if len(started) == 4:
            loop.stop()
        return _P()

    loop = HcLoop("laptop", popen=popen, which=lambda _: "/usr/bin/hc", sleep=slept.append)
    loop.stream()
    assert started == [["/usr/bin/hc", "events", "--json"]] * 4 and slept == [5, 10, 20]

    gone = HcLoop("laptop", popen=lambda argv, **kw: (_ for _ in ()).throw(AssertionError("must not start")),
                  which=lambda _: None, sleep=lambda s: None)
    gone.stream()                                                    # returns quietly: no herdr-core on this host


# -- through the poll -----------------------------------------------------------------------------------------------

def _collector(tmp_path, monkeypatch, host="laptop", status="idle"):
    import ctxswitch.collector.main as M
    from ctxswitch.common.config import Config
    cfg = Config()
    cfg.collector.host_id, cfg.collector.queue_path = host, str(tmp_path / "q.db")
    cfg.collector.claude_sessions_dir = str(tmp_path / "sessions")
    monkeypatch.setattr(M, "runtime_dir", lambda: tmp_path / "rt")
    monkeypatch.setattr(L, "proc_start", lambda pid, p="/proc": "11" if pid == 100 else None)
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "100.json").write_text(json.dumps(
        {"pid": 100, "sessionId": "s1", "procStart": "11", "status": status}))
    return M, M.Collector(cfg)


def _snapshot(col):
    return [p for s, k, p in col.state.pending() if k == "liveness"][-1]


def test_the_poll_puts_hc_at_and_the_agents_state_on_the_snapshot(tmp_path, monkeypatch):
    """A live Claude session running in a herdr terminal: hc_at from its pid, its own ctx.key label written once,
    and the dialog the core sees turning idle into waiting."""
    M, col = _collector(tmp_path, monkeypatch)
    rec = _rec("t9", pid=100, turn=1, status={"app": "claude-code", "state": "blocked", "kind": "question",
                                              "msg": "answer: Continue?"})
    run, calls = _run(ls_rows=[{"address": "t9", "state": "live", "turn": 1, "status": "blocked"}], gets={"t9": rec})
    proc = _fake_proc(tmp_path, 100, 1)
    monkeypatch.setattr(M, "hc_bin", lambda: "/usr/bin/hc")
    monkeypatch.setattr(M, "hc_terminals", lambda: hc_terminals(run=run))
    monkeypatch.setattr(M, "fill_hc_at", lambda items, recs: L.fill_hc_at(items, recs, proc=str(proc)))
    monkeypatch.setattr(M, "label_sessions", lambda items, recs: L.label_sessions(items, recs, run=run))
    col.hc.state = {"t9": {"state": "blocked", "kind": "question", "msg": "answer: Continue?", "app": "claude-code",
                           "turn": 1, "ts": "2026-11-04T00:00:00Z", "title": "", "pid": 100, "reported": True}}
    col.poll_liveness()
    s = {i["key"]: i for i in _snapshot(col)["sessions"]}["laptop/claude/s1"]
    assert s["hc_at"] == "t9" and s["status"] == "waiting"
    assert s["hc_ask"] == {"kind": "question", "text": "answer: Continue?"}
    assert ["hc", "label", "t9", "ctx.key=laptop/claude/s1"] in calls
    col.poll_liveness()
    assert len([c for c in calls if c[:2] == ["hc", "label"]]) == 1  # labelled once for this process


def test_a_host_with_no_hc_reports_sessions_exactly_as_it_did(tmp_path, monkeypatch):
    """A host without herdr-core is the normal case: it asks nothing, stores nothing, and ends nothing."""
    M, col = _collector(tmp_path, monkeypatch, host="devbox", status="busy")
    monkeypatch.setattr(M.shutil, "which", lambda c: None)           # no hc binary at all
    monkeypatch.setattr(M, "hc_terminals", lambda: (_ for _ in ()).throw(AssertionError("must not ask")))
    col.poll_liveness()
    s = {i["key"]: i for i in _snapshot(col)["sessions"]}["devbox/claude/s1"]
    assert s["status"] == "busy" and s["hc_at"] is None and s["hc_ask"] is None


def test_a_core_that_is_not_running_changes_nothing(tmp_path, monkeypatch):
    M, col = _collector(tmp_path, monkeypatch, status="busy")
    run, _ = _run(rc=1)                                              # `hc ls` fails: no server on this host
    proc = _fake_proc(tmp_path, 100, 1)
    monkeypatch.setattr(M, "hc_bin", lambda: "/usr/bin/hc")
    monkeypatch.setattr(M, "hc_terminals", lambda: hc_terminals(run=run))
    monkeypatch.setattr(M, "fill_hc_at", lambda items, recs: L.fill_hc_at(items, recs, proc=str(proc)))
    monkeypatch.setattr(M, "label_sessions", lambda items, recs: L.label_sessions(items, recs, run=run))
    col.poll_liveness()
    s = {i["key"]: i for i in _snapshot(col)["sessions"]}["laptop/claude/s1"]
    assert s["status"] == "busy" and s["hc_at"] is None


# -- the hub's side -------------------------------------------------------------------------------------------------

def test_the_hub_keeps_hc_at_with_the_live_data_and_clears_it_when_the_session_ends(tmp_path):
    store = Store(tmp_path / "hub.db")
    key = "work/claude/s1"
    store.upsert_session({"key": key, "host_id": "work", "harness": "claude", "session_id": "s1", "title": "T"})
    store.set_liveness("work", [{"key": key, "status": "busy", "pid": 100, "tmux_at": "ctx:@1.%2", "hc_at": "t7"}])
    assert store.session(key)["hc_at"] == "t7"
    assert [s["hc_at"] for s in store.sessions("work")] == ["t7"]
    store.set_liveness("work", [{"key": key, "status": "idle", "pid": 100}])
    assert store.session(key)["hc_at"] is None                       # a snapshot that stops naming it clears it
    store.set_liveness("work", [{"key": key, "status": "busy", "pid": 100, "hc_at": "t7"}])
    store.set_liveness("work", [])                                   # ended
    s = store.session(key)
    assert s["live_status"] == "ended" and s["hc_at"] is None


def test_a_session_that_has_no_transcript_row_yet_keeps_its_hc_at(tmp_path):
    store = Store(tmp_path / "hub.db")
    store.set_liveness("work", [{"key": "work/herdr/t3", "status": "waiting", "pid": 10, "hc_at": "t3"}])
    assert store.session("work/herdr/t3")["hc_at"] == "t3"


def test_an_older_database_gains_the_hc_at_column(tmp_path):
    path = tmp_path / "old.db"
    Store(path).db.execute("ALTER TABLE sessions DROP COLUMN hc_at")  # a db written before hc_at existed
    store = Store(path)
    assert "hc_at" in {r[1] for r in store.db.execute("PRAGMA table_info(sessions)")}
    store.set_liveness("work", [{"key": "work/claude/s1", "status": "busy", "pid": 100, "hc_at": "t7"}])
    assert store.session("work/claude/s1")["hc_at"] == "t7"


def test_a_terminal_on_another_machine_is_that_hosts_to_report(tmp_path):
    """`hc ls` on a core with machines attached lists "gpu-host/t3" too; that host's collector owns it."""
    loop = _loop([_snap([_rec("gpu-host/t3", pid=30, agent="codex", turn=1,
                              status={"app": "codex", "state": "working"})])])
    live = {}
    loop.merge(live)
    assert live == {}


def test_the_poll_asks_the_core_only_when_something_still_needs_placing(tmp_path, monkeypatch):
    M, col = _collector(tmp_path, monkeypatch)
    calls = []

    def recs():
        calls.append(1)
        return [_rec("t9", pid=100)]

    monkeypatch.setattr(M, "hc_bin", lambda: "/usr/bin/hc")
    monkeypatch.setattr(M, "hc_terminals", recs)
    proc = _fake_proc(tmp_path, 100, 1)
    monkeypatch.setattr(M, "fill_hc_at", lambda items, r: L.fill_hc_at(items, r, proc=str(proc)))
    monkeypatch.setattr(M, "label_sessions", lambda items, r: L.label_sessions(items, r, run=lambda cmd, **kw: _Done()))
    col.poll_liveness()
    assert calls == [1]
    (tmp_path / "sessions" / "100.json").unlink()
    col.poll_liveness()
    assert calls == [1]                                              # nothing live on this host: nothing to place


def test_hc_is_found_in_the_operators_bin_when_the_service_path_leaves_it_out(tmp_path, monkeypatch):
    """systemd --user's PATH has no ~/.local/bin, where the installer puts hc: the collector saw no herdr at all."""
    from ctxswitch.collector import liveness as L
    (tmp_path / ".local" / "bin").mkdir(parents=True)
    tool = tmp_path / ".local" / "bin" / "hc"; tool.write_text("#!/bin/sh\n"); tool.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path)); monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert L.hc_bin() == str(tool)
    seen = []

    def run(argv, **kw):
        seen.append(argv[0])
        class P: returncode = 0; stdout = '{"terminals": []}'
        return P()
    L.hc_terminals(run=run)
    assert seen == [str(tool)]                                        # and that is the binary it runs
    monkeypatch.setenv("HOME", str(tmp_path / "nowhere"))
    assert L.hc_bin() is None                                         # nothing anywhere: main.py skips herdr
