"""Shell sessions (``ctx shell``): the transcript parser, the output tail, the registry, the Indexer path."""
import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from ctxswitch.collector.adapters.shell import ShellParser, output_tail
from ctxswitch.collector.indexer import Indexer, session_id_from_path
from ctxswitch.collector.liveness import proc_start, read_shell_registry
from ctxswitch.collector.state import CollectorState
from ctxswitch.common.config import CollectorConfig

T0 = datetime(2026, 10, 30, 22, 1, 2, tzinfo=timezone.utc).timestamp()


def iso(offset: float) -> str:
    """The ISO the parser is expected to derive from open.ts + offset."""
    return datetime.fromtimestamp(T0 + offset, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def rec(t: str, **kw) -> dict:
    return {"t": t, **kw}


def open_(offset=0.0, cwd="/home/rick/repos/demo"):
    return rec("open", v=1, ts=T0 + offset, cwd=b64(cwd), shell="bash")


def start(n=1, offset=1.0, cwd="/home/rick/repos/demo", cmd="ls", out=0):
    return rec("start", n=n, ts=T0 + offset, cwd=b64(cwd), cmd=b64(cmd), out=out)


def end(n=1, offset=2.0, rc=0, out=100):
    return rec("end", n=n, ts=T0 + offset, rc=rc, out=out)


def feed_all(parser, rows):
    for i, r in enumerate(rows):
        parser.feed(r, (i + 1) * 100)
    return parser


# -- turns -------------------------------------------------------------------------------------------------------
def test_shell_turns_commands_exits_and_durations():
    cwd = "/home/rick/工作/demo"
    p = feed_all(ShellParser(), [
        open_(cwd=cwd),
        start(n=1, offset=1.0, cwd=cwd, cmd="ls -la", out=0), end(n=1, offset=1.4, rc=0, out=40),
        start(n=2, offset=5.0, cwd=cwd, cmd="git status", out=40), end(n=2, offset=17.0, rc=128, out=90),
        start(n=7, offset=20.0, cwd=cwd, cmd="make", out=90), end(n=7, offset=205.0, rc=0, out=300),
    ])
    p.feed(rec("close", ts=T0 + 300.0), 600)
    turns = p.drain()
    assert [t.idx for t in turns] == [0, 1, 2]
    assert [t.user_text for t in turns] == ["ls -la", "git status", "make"]
    assert [t.user_origin for t in turns] == ["shell"] * 3
    assert [t.assistant_text for t in turns] == ["exit 0 · 0.4s", "exit 128 · 12s", "exit 0 · 3m05s"]
    assert [t.complete for t in turns] == [True] * 3
    assert turns[0].started_at == iso(1.0) and turns[0].ended_at == iso(1.4)
    assert turns[2].started_at == iso(20.0) and turns[2].ended_at == iso(205.0)
    assert p.meta["cwd"] == "/home/rick/工作/demo"
    assert p.meta["started_at"] == iso(0.0) and p.meta["last_activity"] == iso(300.0)
    assert p.meta["harness_version"] == "bash" and p.meta["title"] == ""
    assert p.bad_lines == 0


def test_shell_command_text_survives_quotes_backslashes_and_newlines():
    nasty = 'grep -rn "a\\\\b" src | awk \'NR==1\'\n  | head -3'
    p = ShellParser()
    p.feed(open_(), 50)
    p.feed(start(n=3, cmd=nasty), 100)
    p.feed(end(n=3, out=0), 200)
    assert p.drain()[0].user_text == nasty


def test_shell_open_turn_is_incomplete_until_its_end():
    p = ShellParser()
    p.feed(open_(), 50)
    p.feed(start(n=1, cmd="sleep 10"), 100)
    assert p.current is not None and not p.current.complete and p.drain() == []
    p.feed(end(n=1, offset=11.0, rc=0), 200)
    t = p.drain()[0]
    assert t.complete and t.assistant_text == "exit 0 · 10s" and p.current is None


# -- the output tail ---------------------------------------------------------------------------------------------
def test_shell_output_tail_last_twenty_cleaned_lines(tmp_path):
    out = tmp_path / "s.out"
    body = "".join(f"\x1b[32mstep {i}\x1b[0m\rstep {i} done\n" for i in range(40))
    content = '\x1b]0;rick@host:~/demo\x07\x1b[1m$ make\x1b[0m\n' + body + "warning: noisy\n\n  \n\n"
    out.write_bytes(content.encode())
    start_off, end_off = 0, len(content.encode())
    tail = output_tail(out, start_off, end_off)
    lines = tail.split("\n")
    assert len(lines) == 20                                     # 40 + 2 content lines, last 20 only
    assert lines[-1] == "warning: noisy" and lines[-2] == "step 39 done"
    assert lines[0] == "step 21 done"                           # \r overwrite keeps the text after the last \r
    assert "\x1b" not in tail and "\x07" not in tail and "rick@host" not in tail


def test_shell_output_tail_between_two_offsets(tmp_path):
    out = tmp_path / "s.out"
    out.write_bytes(b"first command\nsecond command\n\rcarriage\x1b[K\nlast\n")
    raw = out.read_bytes()
    cut = raw.index(b"second command")
    assert output_tail(out, cut, len(raw)) == "second command\ncarriage\nlast"
    assert output_tail(out, 0, 0) == ""                         # nothing printed


def test_shell_output_tail_is_capped(tmp_path):
    out = tmp_path / "s.out"
    out.write_bytes(( "".join(f"line {i} {'x' * 60}\n" for i in range(60))).encode())
    tail = output_tail(out, 0, out.stat().st_size)
    assert len(tail) <= 2000 and tail.endswith("x" * 60)


def test_shell_output_tail_gives_up_on_missing_or_truncated(tmp_path):
    out = tmp_path / "s.out"
    out.write_bytes(b"some output\n")
    assert output_tail(tmp_path / "gone.out", 0, 100) == ""     # no .out: the shell ran outside tmux
    assert output_tail(out, 500, 600) == ""                     # end < start: .out was truncated past 16 MiB
    assert output_tail(out, 0, 10_000) == ""                    # end past the end: the file was rewritten
    assert output_tail(None, 0, 10) == ""


# -- interrupted commands ----------------------------------------------------------------------------------------
def test_shell_close_and_finish_mark_the_running_command_interrupted():
    p = feed_all(ShellParser(), [open_(), start(n=1, cmd="make world"), rec("close", ts=T0 + 9.0)])
    t = p.drain()[0]
    assert t.complete and t.assistant_text == "interrupted" and t.user_text == "make world"
    assert t.ended_at == iso(9.0)

    q = feed_all(ShellParser(), [open_(), start(n=1, cmd="make world", offset=1.0)])
    q.finish()                                                  # the session ended with no close record
    u = q.drain()[0]
    assert u.complete and u.assistant_text == "interrupted" and u.ended_at == iso(1.0)
    assert q.drain() == [] and q.current is None


# -- persisted state ---------------------------------------------------------------------------------------------
def test_shell_state_roundtrip_mid_command(tmp_path):
    out = tmp_path / "s.out"
    out.write_bytes(b"head\n" + b"tail line\n" * 2)
    size = out.stat().st_size
    rows = [open_(), start(n=4, cmd="pytest -q", out=5), end(n=4, offset=13.0, rc=1, out=size)]

    one = feed_all(ShellParser(out_path=out), rows)
    one.finish()
    expected = one.drain()

    half = feed_all(ShellParser(out_path=out), rows[:2])        # the collector stops between start and end
    st = json.loads(json.dumps(half.state()))
    two = ShellParser.from_state(st, out_path=out)
    two.feed(rows[2], 300)
    two.finish()
    got = two.drain()
    assert [t.to_dict() for t in got] == [t.to_dict() for t in expected]
    assert got[0].assistant_text.startswith("exit 1 · 12s\n") and got[0].assistant_text.endswith("tail line")
    assert got[0].offset_end == 300                             # the transcript offset, the hub's idempotence key


# -- malformed records -------------------------------------------------------------------------------------------
def test_shell_bad_records_count_and_never_raise():
    p = ShellParser()
    p.feed(open_(), 50)
    p.feed({"t": "compaction", "ts": T0}, 60)                  # unknown record
    p.feed({"nope": True}, 70)                                  # no type at all
    p.feed({}, 80)
    p.feed({"t": "start", "ts": T0 + 1}, 90)                    # no n
    p.feed({"t": "start", "n": "four", "ts": T0 + 1}, 100)      # n not a number
    p.feed({"t": "end", "n": 99, "ts": T0 + 2, "rc": 0}, 110)   # an end with no start
    p.feed({"t": "open", "v": 1, "ts": "not-a-time", "cwd": "@not base64@"}, 120)
    assert p.bad_lines == 6
    p.feed(start(n=1, cmd="echo ok"), 130)
    p.feed(end(n=1, rc=0, out=0), 140)
    turns = p.drain()
    assert [t.user_text for t in turns] == ["echo ok"] and turns[0].complete


def test_shell_resumed_shell_reuses_a_history_number():
    p = ShellParser()
    p.feed(open_(), 50)
    p.feed(start(n=1, offset=1.0, cmd="make"), 100)             # this shell was killed mid-command
    p.feed(open_(offset=10.0), 150)                             # resumed: history counts again from 1
    p.feed(start(n=1, offset=11.0, cmd="ls"), 200)
    p.feed(end(n=1, offset=12.0, rc=0, out=0), 250)
    p.finish()
    a, b = p.drain()
    assert (a.assistant_text, a.complete) == ("interrupted", True) and b.assistant_text == "exit 0 · 1.0s"
    assert b.ended_at == iso(12.0) and p.current is None


def test_shell_close_with_nothing_running_is_a_no_op():
    p = feed_all(ShellParser(), [open_(), end(n=5, rc=0), rec("close", ts=T0 + 5.0)])
    assert p.drain() == [] and p.current is None and p.bad_lines == 1


# -- Indexer -----------------------------------------------------------------------------------------------------
def _indexer(tmp_path, events):
    root = tmp_path / "shells"; root.mkdir(parents=True)
    cfg = CollectorConfig(host_id="laptop", claude_roots=[], pi_roots=[], agy_roots=[], shell_roots=[str(root)])
    st = CollectorState(tmp_path / "c.db")
    return root, Indexer(cfg, "laptop", st, lambda k, p: events.append((k, p))), st


def test_shell_indexer_emits_session_then_turns(tmp_path):
    sid = "3f2b1c4a-0000-4000-8000-000000000001"
    root, ix, st = _indexer(tmp_path, events := [])
    f = root / f"{sid}.jsonl"
    out = root / f"{sid}.out"
    out.write_bytes(b"\x1b[36m$ \x1b[0mhi there\n")
    body = [open_(cwd=str(tmp_path)), start(n=1, cwd=str(tmp_path), cmd="echo hi there", out=0),
            end(n=1, rc=0, out=out.stat().st_size)]
    with open(f, "wb") as fh:
        for r in body:
            fh.write((json.dumps(r) + "\n").encode())
    assert ix.scan_all() == 1
    kinds = [k for k, _ in events]
    assert kinds == ["session", "turns"]
    sess = events[0][1]
    assert sess["key"] == f"laptop/shell/{sid}" and sess["harness"] == "shell" and sess["reset"] is True
    assert sess["cwd"] == str(tmp_path) and sess["harness_version"] == "bash" and sess["session_id"] == sid
    assert sess["started_at"] == iso(0.0) and sess["last_activity"] == iso(2.0)
    turn = events[1][1]["turns"][0]
    assert turn["idx"] == 0 and turn["user_text"] == "echo hi there" and turn["user_origin"] == "shell"
    assert turn["complete"] and turn["assistant_text"] == "exit 0 · 1.0s\n$ hi there"   # the colour codes are gone
    assert events[1][1]["open"] is None and events[1][1]["ended"] is False
    # an appended command: its turn stays open until the end record lands (meta re-sent: last_activity moved)
    events.clear()
    with open(f, "ab") as fh:
        fh.write((json.dumps(start(n=2, offset=30.0, cmd="sleep 1", out=out.stat().st_size)) + "\n").encode())
    assert ix.process(f)
    assert events[-1][0] == "turns" and [p for k, p in events if k == "session"][0]["reset"] is False
    assert events[-1][1]["turns"] == [] and events[-1][1]["open"]["user_text"] == "sleep 1" and not events[-1][1]["ended"]
    events.clear()
    with open(f, "ab") as fh:
        fh.write((json.dumps(end(n=2, offset=31.0, rc=0, out=out.stat().st_size)) + "\n").encode())
    assert ix.process(f)
    done = [t for k, p in events if k == "turns" for t in p["turns"]]
    assert [t["assistant_text"] for t in done] == ["exit 0 · 1.0s"] and done[0]["idx"] == 1   # .out did not grow
    assert st.file(str(f))["harness"] == "shell" and st.file(str(f))["offset"] == f.stat().st_size


def test_shell_indexer_ended_session_resumes_and_closes(tmp_path):
    sid = "3f2b1c4a-0000-4000-8000-000000000002"
    root, ix, st = _indexer(tmp_path, events := [])
    f = root / f"{sid}.jsonl"
    with open(f, "wb") as fh:
        for r in [open_(), start(n=1, cmd="make")]:
            fh.write((json.dumps(r) + "\n").encode())
    old = time.time() - 3600
    os.utime(f, (old, old))
    ix.live_keys = set()                                        # no registry entry: the shell is gone
    assert ix.scan_all() == 1
    ev = [p for k, p in events if k == "turns"][0]
    assert ev["ended"] and ev["turns"][0]["complete"] and ev["turns"][0]["assistant_text"] == "interrupted"
    assert [p for k, p in events if k == "session"][0]["ended"] and st.file(str(f))["ended"]
    # a live shell (registry says so) is not ended, and its command stays open
    events.clear()
    with open(f, "ab") as fh:
        fh.write((json.dumps(start(n=2, offset=2.0, cmd="watch -n1 date")) + "\n").encode())
    os.utime(f, None)
    ix.live_keys = {f"laptop/shell/{sid}"}
    assert ix.process(f)
    assert [p for k, p in events if k == "turns"][0]["ended"] is False


def test_shell_session_id_is_the_stem():
    assert session_id_from_path(Path("/x/3f2b1c4a-0000-4000-8000-000000000001.jsonl"), "shell") == \
        "3f2b1c4a-0000-4000-8000-000000000001"


def test_shell_root_defaults_to_the_state_directory():
    from ctxswitch.common.config import CollectorConfig as C
    assert C().shell_roots == ["~/.local/state/ctxswitch/shells"]


# -- registry liveness -------------------------------------------------------------------------------------------
def _fake_proc(tmp_path, pid, start_ticks):
    proc = tmp_path / "proc"
    (proc / str(pid)).mkdir(parents=True)
    (proc / str(pid) / "stat").write_text(f"{pid} (bash) " + " ".join(["0"] * 19 + [start_ticks] + ["0"] * 30))
    return proc


def test_shell_registry_live_stale_dead_and_bad(tmp_path):
    pid = os.getpid()
    ticks = proc_start(pid)                                     # this process's real start time
    assert ticks, "/proc must be readable for this test"
    proc = _fake_proc(tmp_path, pid, ticks)
    reg = tmp_path / "ctxswitch"; reg.mkdir()
    sid = "3f2b1c4a-0000-4000-8000-000000000003"
    (reg / f"shell-{pid}.json").write_text(json.dumps(
        {"sessionId": sid, "pid": pid, "procStart": ticks, "status": "busy",
         "statusSince": "2026-10-30T22:01:02Z", "cwd": "/home/rick/repos/demo"}))
    (reg / f"shell-{pid + 1}.json").write_text(json.dumps(                       # pid recycled: wrong procStart
        {"sessionId": "dead-start", "pid": pid, "procStart": "1", "status": "idle"}))
    (reg / f"shell-{pid + 2}.json").write_text(json.dumps(                        # no such process
        {"sessionId": "gone", "pid": pid + 2, "procStart": ticks, "status": "idle"}))
    (reg / "shell-9999.json").write_text("{not json")                            # half-written: skipped
    live = read_shell_registry(tmp_path, "laptop", proc=str(proc))
    k = f"laptop/shell/{sid}"
    assert set(live) == {k}
    assert live[k].status == "busy" and live[k].status_since == "2026-10-30T22:01:02Z"
    assert live[k].pid == pid and live[k].socket is None
    # idle, and a status the collector doesn't know, read as idle / unknown
    (reg / f"shell-{pid}.json").write_text(json.dumps({"sessionId": sid, "pid": pid, "procStart": ticks, "status": "idle"}))
    assert read_shell_registry(tmp_path, "laptop", proc=str(proc))[k].status == "idle"
    (reg / f"shell-{pid}.json").write_text(json.dumps({"sessionId": sid, "pid": pid, "procStart": ticks, "status": "weird"}))
    assert read_shell_registry(tmp_path, "laptop", proc=str(proc))[k].status == "unknown"


def test_shell_registry_missing_directory_is_empty(tmp_path):
    assert read_shell_registry(tmp_path / "nowhere", "laptop") == {}


def test_the_collector_creates_its_shell_root_so_the_first_shell_is_watched(tmp_path):
    from ctxswitch.collector.main import ensure_shell_roots
    shells, claude = tmp_path / "a" / "shells", tmp_path / "claude"
    ensure_shell_roots([(shells, "shell"), (claude, "claude")])
    assert shells.is_dir() and oct(shells.stat().st_mode & 0o777) == "0o700"
    assert not claude.exists()                                        # only shell roots: the others belong to their harness



def test_a_terminals_crlf_output_keeps_its_lines(tmp_path):
    """What tmux pipe-pane really captures: bracketed-paste toggles, \r\n line ends, a \r progress overwrite."""
    from ctxswitch.collector.adapters.shell import output_tail
    raw = b"\x1b[?2004l\rhello from the shell\r\nstep 1/3\rstep 3/3\r\nls: cannot access '/x': No such file\r\n\x1b[?2004h"
    o = tmp_path / "s.out"; o.write_bytes(raw)
    assert output_tail(o, 0, len(raw)) == "hello from the shell\nstep 3/3\nls: cannot access '/x': No such file"
