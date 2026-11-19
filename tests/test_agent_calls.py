"""Call records from the agents' own transcripts — what the board's cloud rows are built from. The Claude parser
turns one API reply (several lines, one message.id) into one record spanning trigger → last block; the indexer puts
the account's name on it (its Claude profile, "claude" where profiles don't matter, "agy" for Antigravity) and keeps
only what is still inside the hub's window."""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from claude_rows import BASE, S, assistant, system, text, tool_result, tool_use, user
from conftest import feed_all

from ctxswitch.collector.adapters.claude import ClaudeParser
from ctxswitch.collector.indexer import Indexer
from ctxswitch.collector.state import CollectorState
from ctxswitch.common.config import ClaudeProfile, CollectorConfig

def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def epoch(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def reply(blocks, ts, mid="msg_1", out=None, model="claude-opus-5-5"):
    """One line of an API reply: every line of it repeats the reply's usage, output_tokens final on the last."""
    msg = {"id": mid, "role": "assistant", "model": model, "content": blocks, "stop_reason": "end_turn"}
    if out is not None:
        msg["usage"] = {"input_tokens": 7, "output_tokens": out, "cache_read_input_tokens": 0}
    return dict(BASE, type="assistant", uuid=f"a{ts}{mid}", timestamp=ts, message=msg)


def thinking(t, ts, mid="msg_1", out=None):
    return reply([{"type": "thinking", "thinking": t, "signature": "sig"}], ts, mid, out)


# -- the Claude parser ---------------------------------------------------------------------------------------------

def test_one_reply_is_one_call_from_its_trigger_to_its_last_block():
    # the real shape: a tool result at 18:28:21.1 triggered the request, its thinking landed at 18:28:30.2 and its
    # text at 18:28:37.1 with output_tokens 1427 — a 16.0 s call
    rows = [user("check the tests", "2026-11-18T18:28:00.000Z"),
            assistant([tool_use("Bash", "t1", {"command": "pytest"})], "2026-11-18T18:28:10.000Z", ),
            tool_result("t1", "1 passed", "2026-11-18T18:28:21.100Z"),
            thinking("looking", "2026-11-18T18:28:30.200Z", out=1427),
            reply([text("One passed.")], "2026-11-18T18:28:37.100Z", out=1427),
            tool_result("t2", "ok", "2026-11-18T18:28:40.000Z")]
    p = feed_all(ClaudeParser(), rows)
    calls = p.drain_calls()
    assert len(calls) == 1
    c = calls[0]
    assert "agent" not in c and epoch(c["end"]) == epoch("2026-11-18T18:28:37.100Z")
    assert round(c["dur_s"], 3) == 16.0 and c["out"] == 1427 and c["model"] == "claude-opus-5-5"
    assert p.drain_calls() == []                                       # drained once, never twice


def test_a_reply_written_as_three_lines_counts_once_with_its_final_token_count():
    rows = [user("refactor it", "2026-11-18T18:00:00.000Z"),
            thinking("hmm", "2026-11-18T18:00:04.000Z", out=210),
            reply([tool_use("Edit", "t1")], "2026-11-18T18:00:06.000Z", out=340),
            reply([text("Done.")], "2026-11-18T18:00:09.000Z", out=512),
            user("thanks", "2026-11-18T18:01:00.000Z")]
    calls = feed_all(ClaudeParser(), rows).drain_calls()
    assert [(round(c["dur_s"], 3), c["out"]) for c in calls] == [(9.0, 512)]


def test_two_replies_with_a_tool_result_between_are_two_calls_with_their_own_starts():
    rows = [user("two steps", "2026-11-18T18:00:00.000Z"),                      # reply 1: 18:00:00 -> 18:00:02
            reply([tool_use("Read", "t1")], "2026-11-18T18:00:02.000Z", out=300),
            tool_result("t1", "contents", "2026-11-18T18:00:05.000Z"),           # reply 2 starts here
            reply([text("Now I know.")], "2026-11-18T18:00:11.000Z", mid="msg_2", out=400),   # its own API reply
            system("turn_duration", "2026-11-18T18:00:12.000Z")]
    calls = feed_all(ClaudeParser(), rows).drain_calls()
    assert [(round(c["dur_s"], 3), c["out"]) for c in calls] == [(2.0, 300), (6.0, 400)]
    assert epoch(calls[1]["end"]) == epoch("2026-11-18T18:00:11.000Z")


def test_a_sidechain_reply_is_not_this_session_s_call():
    rows = [user("delegate it", "2026-11-18T18:00:00.000Z"),
            reply([tool_use("Agent", "t1")], "2026-11-18T18:00:02.000Z", out=120),
            dict(BASE, type="assistant", isSidechain=True, timestamp="2026-11-18T18:00:30.000Z",
                 message={"role": "assistant", "model": "claude-haiku-5-5", "id": "msg_sub",
                          "content": [{"type": "text", "text": "subagent done"}],
                          "usage": {"input_tokens": 1, "output_tokens": 900}}),
            tool_result("t1", "subagent finished", "2026-11-18T18:01:00.000Z")]
    calls = feed_all(ClaudeParser(), rows).drain_calls()
    assert [(round(c["dur_s"], 3), c["out"]) for c in calls] == [(2.0, 120)]


def test_a_reply_open_when_the_collector_stopped_continues_from_its_state():
    p = ClaudeParser()
    p.feed(user("keep going", "2026-11-18T18:00:00.000Z"), 100)
    p.feed(thinking("halfway", "2026-11-18T18:00:03.000Z", out=200), 200)
    assert p.drain_calls() == []                                       # still streaming: nothing to report
    q = ClaudeParser.from_state(json.loads(json.dumps(p.state())))
    q.feed(reply([text("Finished.")], "2026-11-18T18:00:08.000Z", out=640), 300)
    q.feed(system("turn_duration", "2026-11-18T18:00:09.000Z"), 400)
    calls = q.drain_calls()
    assert [(round(c["dur_s"], 3), c["out"]) for c in calls] == [(8.0, 640)]
    assert q.drain_calls() == []


def test_a_reply_that_produced_no_tokens_is_not_a_call():
    rows = [user("say nothing", "2026-11-18T18:00:00.000Z"),
            reply([text("")], "2026-11-18T18:00:05.000Z", out=0),
            user("and now?", "2026-11-18T18:00:06.000Z")]
    assert feed_all(ClaudeParser(), rows).drain_calls() == []


def test_the_session_closing_finishes_the_reply_it_left_open():
    p = ClaudeParser()
    p.feed(user("last word", "2026-11-18T18:00:00.000Z"), 100)
    p.feed(reply([text("Bye.")], "2026-11-18T18:00:04.000Z", out=250), 200)
    p.finish()
    assert [(round(c["dur_s"], 3), c["out"]) for c in p.drain_calls()] == [(4.0, 250)]


# -- the indexer's labelling ---------------------------------------------------------------------------------------

def ago(s: float) -> str:
    """An instant ``s`` seconds before now: the indexer only keeps what the hub's window still covers."""
    return iso(time.time() - s)


def _setup(tmp_path, profiles=None, root=None):
    root = root or tmp_path / "claude" / "-home-rick-repos-demo"
    root.mkdir(parents=True, exist_ok=True)
    cfg = CollectorConfig(claude_roots=[str(root.parent)], pi_roots=[], backfill_days=30)
    ix = Indexer(cfg, "devbox", CollectorState(tmp_path / "c.db"), lambda k, p: None, profiles)
    return root / f"{S}.jsonl", ix


def _write(path: Path, rows):
    with open(path, "wb") as f:
        for r in rows:
            f.write((json.dumps(r) + "\n").encode())


def test_a_claude_call_is_labelled_by_its_host_s_profile(tmp_path):
    # a Max-only host: its collector names the account, so a board elsewhere can show "max"
    maxroot = tmp_path / "cm" / "projects" / "-home-rick-repos-demo"
    f, ix = _setup(tmp_path, [ClaudeProfile(name="max", config_dir=str(tmp_path / "cm"))], root=maxroot)
    _write(f, [user("go", ago(20)), reply([text("done")], ago(6), out=800), user("ok", ago(1))])
    ix.process(f)
    assert [(c["agent"], c["out"]) for c in ix.drain_calls()] == [("max", 800)]


def test_a_host_whose_profiles_dont_matter_labels_every_claude_call_claude(tmp_path):
    f, ix = _setup(tmp_path)
    _write(f, [user("go", ago(20)), reply([text("done")], ago(6), out=800), user("ok", ago(1))])
    ix.process(f)
    assert [c["agent"] for c in ix.calls] == ["claude"]


def test_a_call_that_ended_two_hours_ago_is_dropped_so_a_backfill_never_floods_the_hub(tmp_path):
    f, ix = _setup(tmp_path)
    _write(f, [user("an old session", "2026-10-30T15:00:00.000Z"),
               reply([text("ancient")], "2026-10-30T15:00:09.000Z", out=900),
               user("and then", "2026-10-30T15:00:20.000Z")])
    ix.process(f)
    assert ix.calls == [] and ix.drain_calls() == []


def test_drain_calls_returns_and_clears(tmp_path):
    f, ix = _setup(tmp_path)
    _write(f, [user("go", ago(20)), reply([text("done")], ago(6), out=800), user("ok", ago(1))])
    ix.process(f)
    assert len(ix.drain_calls()) == 1 and ix.drain_calls() == []


def test_pi_transcripts_produce_no_calls(tmp_path):
    # a pi reply's timings aren't read as call records: only the Claude and agy parsers report any, and the
    # indexer only drains those two
    root = tmp_path / "pi" / "--home-rick--"; root.mkdir(parents=True)
    f = root / "1793384917_01a1-id.jsonl"
    _write(f, [{"type": "session", "version": 3, "id": "01a1-id", "timestamp": ago(30), "cwd": "/home/rick"},
               {"type": "message", "id": "u1", "parentId": None, "timestamp": ago(20),
                "message": {"role": "user", "content": [{"type": "text", "text": "hi"}], "timestamp": ago(20)}},
               {"type": "message", "id": "a1", "parentId": "u1", "timestamp": ago(19),
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}], "timestamp": ago(19),
                            "usage": {"output": 12}}}])
    cfg = CollectorConfig(claude_roots=[], pi_roots=[str(tmp_path / "pi")], backfill_days=30)
    events = []
    ix = Indexer(cfg, "devbox", CollectorState(tmp_path / "c.db"), lambda k, p: events.append((k, p)))
    assert ix.process(f) and ix.drain_calls() == []
    assert [k for k, _ in events][:2] == ["session", "turns"]           # the turns themselves still arrive


def test_a_line_written_beside_the_timeline_does_not_split_a_reply():
    # a title or a cost-state line can land between an assistant's blocks: it is no trigger and ends nothing
    rows = [user("go", "2026-11-18T18:00:00.000Z"),
            thinking("hmm", "2026-11-18T18:00:03.000Z", out=200),
            {"type": "ai-title", "sessionId": S, "aiTitle": "Go"},
            reply([text("Done.")], "2026-11-18T18:00:07.000Z", out=420),
            user("nice", "2026-11-18T18:00:08.000Z")]
    calls = feed_all(ClaudeParser(), rows).drain_calls()
    assert [(round(c["dur_s"], 3), c["out"]) for c in calls] == [(7.0, 420)]


def test_a_tool_result_written_mid_reply_does_not_split_it_into_a_phantom_call():
    """Parallel tool calls: Claude Code writes each tool's result as it finishes, while the same reply is still
    streaming its next tool_use. That reply is ONE call, from its trigger to the line before that first result — not a
    real call plus a 0.2 s phantom carrying the same tokens (the real transcript that showed 26 100 tok/s)."""
    rows = [tool_result("t0", "ok", "2026-11-19T04:04:12.022Z"),
            reply([text("Two things to check.")], "2026-11-19T04:04:12.532Z", mid="msg_p", out=4698),
            thinking("hm", "2026-11-19T04:04:34.511Z", mid="msg_p", out=4698),
            reply([tool_use("Bash", "t1")], "2026-11-19T04:04:34.847Z", mid="msg_p", out=4698),
            tool_result("t1", "a", "2026-11-19T04:04:34.918Z"),
            reply([tool_use("Bash", "t2")], "2026-11-19T04:04:35.098Z", mid="msg_p", out=4698),
            tool_result("t2", "b", "2026-11-19T04:04:35.173Z"),
            reply([text("Both fine.")], "2026-11-19T04:04:40.173Z", mid="msg_q", out=300),
            user("thanks", "2026-11-19T04:05:00.000Z")]
    calls = feed_all(ClaudeParser(), rows).drain_calls()
    assert [(c["out"], round(c["dur_s"], 3)) for c in calls] == [(4698, 22.825), (300, 5.0)]   # 12.022 → 34.847


def test_a_reply_already_counted_never_counts_again_even_across_a_restart():
    rows = [user("go", "2026-11-19T04:00:00.000Z"), reply([text("a")], "2026-11-19T04:00:05.000Z", mid="msg_r", out=500),
            user("next", "2026-11-19T04:00:10.000Z")]
    p = feed_all(ClaudeParser(), rows)
    assert len(p.drain_calls()) == 1
    p = ClaudeParser.from_state(json.loads(json.dumps(p.state())))           # the collector restarted
    feed_all(p, [reply([text("a")], "2026-11-19T04:00:11.000Z", mid="msg_r", out=500),   # a stray line of msg_r
                 user("more", "2026-11-19T04:00:20.000Z")])
    assert p.drain_calls() == []


def test_a_pool_worker_s_qwen_reply_is_not_a_claude_code_call(tmp_path):
    """saint-agent's workers are headless Claude Code too, so they write the same transcripts under the same
    profile — but their model is the local pool's, which the seat rows already count. Only Claude models count."""
    f, ix = _setup(tmp_path)
    _write(f, [user("go", ago(20)), reply([text("done")], ago(10), mid="msg_w", out=900, model="Qwen3.8-Flash-Next-MXFP4"),
               user("ok", ago(8)), reply([text("done")], ago(4), mid="msg_c", out=700, model="claude-opus-5-5"),
               user("ok", ago(1))])
    ix.process(f)
    assert [c["out"] for c in ix.drain_calls()] == [700]
