"""Per-turn token usage (contract A) and the saint-agent pool-job link (contract B)."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_rows import S, assistant, system, text, tool_use, user
from conftest import feed_all

from ctxswitch.collector.adapters.claude import ClaudeParser
from ctxswitch.collector.adapters.pi import PiParser
from ctxswitch.collector.indexer import Indexer
from ctxswitch.collector.jobs import Jobs, read_ledger
from ctxswitch.collector.state import CollectorState
from ctxswitch.common.config import CollectorConfig
from ctxswitch.common.schema import Turn

# -- A: usage on turns -----------------------------------------------------------------------------------------

U = {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 700}


def ausage(blocks, ts, usage=None, model="claude-opus-5-5"):
    """An assistant row carrying a message.usage (or none at all, as a mid-turn API error row has)."""
    row = assistant(blocks, ts, model=model)
    if usage is not None:
        row["message"] = dict(row["message"], usage=usage)
    return row


def test_claude_usage_sums_the_turns_assistant_entries():
    rows = [user("go", "2026-10-28T10:00:00Z"),
            ausage([text("Looking."), tool_use("Read", "t1")], "2026-10-28T10:00:01Z", U),
            ausage([text("Done.")], "2026-10-28T10:00:02Z", dict(U, input_tokens=1, output_tokens=2)),
            ausage([text("Still here.")], "2026-10-28T10:00:03Z"),          # no usage on this entry: leave it be
            system("turn_duration", "2026-10-28T10:00:04Z", durationMs=4000)]
    p = feed_all(ClaudeParser(), rows)
    p.finish()                                   # turn_duration marks the turn closed but leaves it open for a follow-up
    t = p.drain()[0]
    assert t.usage == {"input": 101, "output": 22, "cache_read": 10000, "cache_creation": 1400}
    assert t.model == "claude-opus-5-5" and t.cost_usd is None
    assert p.meta["model"] == "claude-opus-5-5"                      # the session's model is still recorded too
    switched = feed_all(ClaudeParser(), [user("go", "2026-10-28T10:00:00Z"),
                                         ausage([text("a")], "2026-10-28T10:00:01Z", U, model="claude-opus-5-5"),
                                         ausage([text("b")], "2026-10-28T10:00:02Z", U, model="claude-haiku-4-5")])
    assert switched.current.model == "claude-haiku-4-5"               # the turn's last assistant entry names it


def test_claude_usage_counts_a_non_int_as_zero_and_stays_none_without_an_assistant():
    p = feed_all(ClaudeParser(), [user("go", "2026-10-28T10:00:00Z"),
                                  ausage([text("ok")], "2026-10-28T10:00:01Z",
                                         {"input_tokens": 5, "output_tokens": "12", "cache_read_input_tokens": None}),
                                  user("next", "2026-10-28T10:01:00Z"),
                                  user("and more", "2026-10-28T10:02:00Z")])
    done = p.drain()
    assert done[0].usage == {"input": 5, "output": 0, "cache_read": 0, "cache_creation": 0}
    assert done[1].usage is None and done[1].model == "" and done[1].cost_usd is None      # a turn with no assistant


def test_turn_dict_roundtrip_with_and_without_the_new_keys():
    t = Turn(idx=0, started_at="2026-10-28T10:00:00Z", ended_at=None, user_text="q", user_origin="human",
             assistant_text="a", usage={"input": 1, "output": 2, "cache_read": 3, "cache_creation": 4},
             model="claude-opus-5-5", cost_usd=0.25)
    d = json.loads(json.dumps(t.to_dict()))
    assert Turn.from_dict(d) == t
    old = {k: v for k, v in d.items() if k not in ("usage", "model", "cost_usd")}     # a stored turn from before
    back = Turn.from_dict(old)
    assert (back.usage, back.model, back.cost_usd) == (None, "", None) and back.user_text == "q"


def pi_msg(role, i, parent, sec, content=None, **extra):
    m = dict(extra, role=role, content=content or [])
    return {"type": "message", "id": i, "parentId": parent, "timestamp": f"2026-10-28T10:00:{sec:02d}.000Z", "message": m}


def pi_usage(total):
    return {"input": 10, "output": 4, "cacheRead": 900, "cacheWrite": 30, "cost": {"total": total}}


def test_pi_usage_model_and_cost():
    head = {"type": "session", "version": 3, "id": "01a1-id", "timestamp": "2026-10-28T10:00:00.000Z",
            "cwd": "/home/rick/repos/demo"}
    ask = pi_msg("user", "u1", None, 1, [{"type": "text", "text": "go"}])
    rows = [head, ask,
            pi_msg("assistant", "a1", "u1", 2, [{"type": "toolCall", "id": "c1", "name": "read", "arguments": {}}],
                   model="saint-auto", provider="saint", usage=pi_usage(0.01), stopReason="toolUse"),
            pi_msg("assistant", "a2", "a1", 3, [{"type": "text", "text": "Done."}],
                   model="claude-sonnet-5", provider="anthropic", usage=pi_usage(0.02), stopReason="stop")]
    t = feed_all(PiParser(), rows).current
    assert t.usage == {"input": 20, "output": 8, "cache_read": 1800, "cache_creation": 60}
    assert t.model == "claude-sonnet-5" and t.cost_usd == round(0.01 + 0.02, 10)     # the last entry names the turn
    assert feed_all(PiParser(), rows[:3]).current.model == "saint/saint-auto"        # pi names its own providers
    free = feed_all(PiParser(), [head, ask,
                                 pi_msg("assistant", "a1", "u1", 2, [{"type": "text", "text": "hi"}],
                                        model="saint-auto", provider="saint", usage=pi_usage(0), stopReason="stop")])
    assert free.current.cost_usd is None and free.current.usage["input"] == 10       # 0 is not a price


# -- B: the saint-agent ledger ---------------------------------------------------------------------------------

T0 = datetime(2026, 10, 30, 0, 0, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ledger(p: Path, *lines) -> Path:
    """Write ledger lines (dicts or raw text) to p."""
    with open(p, "w") as f:
        for d in lines:
            f.write(d if isinstance(d, str) else json.dumps(d))
            f.write("\n")
    return p


START = {"event": "start", "job": "J1", "sid": "S1", "session": "P", "origin": "laptop",
         "workdir": "/home/rick/repos/demo", "brief": "/tmp/sa/demo/brief.md", "profile": "pool"}


def test_jobs_start_line_alone_is_running(tmp_path):
    j = Jobs(ledger(tmp_path / "ledger.jsonl", dict(START, ts=iso(T0))))
    got = j.for_session("S1", "/home/rick/repos/demo", iso(T0))
    assert got == {"job": "J1", "job_status": "running", "job_brief": "brief.md", "job_dur_s": None,
                   "job_turns": None, "job_tokens": None, "parent_session": "P"}
    assert j.for_session("other", "/home/rick/repos/demo", iso(T0)) is None


def test_jobs_end_line_sets_status_tokens_and_duration(tmp_path):
    end = dict(START, ts=iso(T0 + timedelta(minutes=30)), sid="S2", job="J2", status="ok", turns=72, dur_s=1800,
               tokens={"input": 100, "cache_read": 9000, "cache_creation": 0, "output": 30})
    del end["event"]                                            # an end line as the script writes it today
    j = Jobs(ledger(tmp_path / "ledger.jsonl", dict(START, sid="S2", job="J2"), end))
    got = j.for_session("S2")
    assert got["job_status"] == "ok" and got["job"] == "J2" and got["job_dur_s"] == 1800 and got["job_turns"] == 72
    assert got["job_tokens"] == {"input": 100, "cache_read": 9000, "cache_creation": 0, "output": 30}
    assert got["job_brief"] == "brief.md" and got["parent_session"] == "P"


def test_jobs_fallback_matches_an_old_end_line_by_workdir_and_time(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    link = tmp_path / "link"
    link.symlink_to(work)
    end = {"ts": iso(T0), "job": "J3", "session": "P", "workdir": str(work), "status": "ok", "turns": 5, "dur_s": 100,
           "tokens": {"input": 1, "cache_read": 2, "cache_creation": 3, "output": 4}}
    j = Jobs(ledger(tmp_path / "ledger.jsonl", end))
    assert j.for_session("S9", str(work), iso(T0 - timedelta(seconds=150)))["job"] == "J3"
    assert j.for_session("S9", str(link), iso(T0 - timedelta(seconds=150)))["job"] == "J3"    # a symlinked cwd
    assert j.for_session("S9", str(work), iso(T0 - timedelta(seconds=900))) is None           # before the run began
    assert j.for_session("S9", str(tmp_path), iso(T0 - timedelta(seconds=150))) is None       # a different checkout
    assert j.for_session("S9", str(work), None) is None                                       # no timestamp to place it


def test_jobs_skips_malformed_lines_and_returns_none_for_a_normal_session(tmp_path):
    p = ledger(tmp_path / "ledger.jsonl", "not json at all", "[1,2]", json.dumps({"ts": iso(T0)}),   # a line with no job id
               '{"ts": "2026-10-30T00:00:00Z", "job": "J4", "sid": "S4", "session": null, "workdir": "/x", '
               '"status": "error: stalled", "turns": "-", "dur_s": 12, "tokens": "-"}')
    j = Jobs(p)
    assert len(read_ledger(p)) == 2                                    # unparsable and non-object lines are dropped
    got = j.for_session("S4")
    assert got == {"job": "J4", "job_status": "error: stalled", "job_brief": "", "job_dur_s": 12, "job_turns": None,
                   "job_tokens": None, "parent_session": None}
    assert Jobs(ledger(tmp_path / "ledger.jsonl", dict(START, sid="S1"))).for_session("someone-elses-session", "/tmp", iso(T0)) is None


def test_jobs_refreshes_when_the_ledger_grows(tmp_path):
    p = ledger(tmp_path / "ledger.jsonl", dict(START, ts=iso(T0)))
    j = Jobs(p)
    assert j.for_session("S1")["job_status"] == "running"
    with open(p, "a") as f:
        f.write(json.dumps(dict(START, ts=iso(T0 + timedelta(minutes=1)), status="ok", turns=3, dur_s=60)) + "\n")
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
    assert j.for_session("S1")["job_status"] == "ok"


# -- the indexer ties the two together -------------------------------------------------------------------------

def test_indexer_links_a_pool_job_to_the_session_that_launched_it(tmp_path, monkeypatch):
    root = tmp_path / "claude" / "-home-rick-repos-demo"
    root.mkdir(parents=True)
    cfg = CollectorConfig(claude_roots=[str(tmp_path / "claude")], pi_roots=[], backfill_days=30)
    events = []
    ix = Indexer(cfg, "laptop", CollectorState(tmp_path / "c.db"), lambda k, p: events.append((k, p)))
    assert ix.jobs.for_session(S) is None                              # nothing in the ledger: a plain session
    ledger_path = ledger(tmp_path / "ledger.jsonl", dict(START, ts=iso(T0), sid=S))
    ix.jobs = Jobs(ledger_path)                                        # $CTXSWITCH_SAINT_LEDGER for this test
    f = root / f"{S}.jsonl"
    with open(f, "wb") as h:
        for r in [user("do the thing", "2026-10-30T00:00:00Z"),
                  ausage([text("ok")], "2026-10-30T00:00:01Z", U)]:
            h.write((json.dumps(r) + "\n").encode())
    ix.live_keys = {f"laptop/claude/{S}"}
    assert ix.scan_all() == 1
    sess = [p for k, p in events if k == "session"][0]
    assert sess["parent_key"] == "laptop/claude/P" and sess["job"] == "J1" and sess["job_status"] == "running"
    assert sess["job_brief"] == "brief.md"
    assert events[1][1]["turns"] == [] and events[1][1]["open"]["usage"]["cache_read"] == 5000
    # a job that finished: the end line's status, tokens and duration ride along
    with open(ledger_path, "a") as h:
        h.write(json.dumps(dict(START, sid=S, ts=iso(T0 + timedelta(minutes=10)), status="ok", turns=9, dur_s=600,
                               tokens={"input": 7, "cache_read": 8, "cache_creation": 9, "output": 10})) + "\n")
    os.utime(ledger_path, (ledger_path.stat().st_atime, ledger_path.stat().st_mtime + 5))
    events.clear()
    with open(f, "ab") as h:
        h.write((json.dumps(user("more", "2026-10-30T00:10:00Z")) + "\n").encode())
    assert ix.process(f)
    sess = [p for k, p in events if k == "session"][0]
    assert sess["job_status"] == "ok" and sess["job_turns"] == 9 and sess["job_dur_s"] == 600
    assert sess["job_tokens"] == {"input": 7, "cache_read": 8, "cache_creation": 9, "output": 10}


def test_indexer_leaves_a_normal_session_unlinked(tmp_path):
    root = tmp_path / "claude" / "-home-rick-repos-demo"
    root.mkdir(parents=True)
    cfg = CollectorConfig(claude_roots=[str(tmp_path / "claude")], pi_roots=[], backfill_days=30)
    events = []
    ix = Indexer(cfg, "laptop", CollectorState(tmp_path / "c.db"), lambda k, p: events.append((k, p)))
    ix.jobs = Jobs(ledger(tmp_path / "unused.jsonl", dict(START, sid="elsewhere")))
    f = root / f"{S}.jsonl"
    with open(f, "wb") as h:
        for r in [user("hi", "2026-10-30T00:00:00Z"), assistant([text("hello")], "2026-10-30T00:00:01Z")]:
            h.write((json.dumps(r) + "\n").encode())
    ix.live_keys = {f"laptop/claude/{S}"}
    ix.scan_all()
    sess = [p for k, p in events if k == "session"][0]
    assert sess["parent_key"] == "" and sess["job"] == "" and sess["job_status"] == "" and sess["job_brief"] == ""
    assert sess["job_dur_s"] is None and sess["job_turns"] is None and sess["job_tokens"] is None
