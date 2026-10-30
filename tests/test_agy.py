"""Google Antigravity (agy): the SQLite trajectory store read as raw protobuf, presence-lock liveness, resume, picker."""
import fcntl
import json
import os
import sqlite3
from pathlib import Path

from ctxswitch.collector.adapters import agy
from ctxswitch.collector.adapters.agy import AgyParser, USER_INPUT, PLANNER_RESPONSE
from ctxswitch.collector.indexer import Indexer
from ctxswitch.collector.liveness import read_agy_presence
from ctxswitch.collector.state import CollectorState
from ctxswitch.common.config import CollectorConfig, Config, load


# -- a tiny protobuf encoder, enough to build the blobs agy writes ------------------------------------------------
def _vint(n: int) -> bytes:
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def V(f: int, n: int) -> bytes:
    return _vint(f << 3) + _vint(n)


def S(f: int, s: bytes | str) -> bytes:
    b = s.encode() if isinstance(s, str) else s
    return _vint((f << 3) | 2) + _vint(len(b)) + b


def TS(f: int, secs: int) -> bytes:
    return S(f, V(1, secs) + V(2, 123))


def user_step(text: str, secs: int) -> tuple[bytes, bytes]:
    return TS(1, secs), V(1, USER_INPUT) + S(19, S(2, text) + S(3, "\n8" + text))


def planner_step(text: str, secs: int) -> tuple[bytes, bytes]:
    return TS(1, secs), V(1, PLANNER_RESPONSE) + S(20, S(1, text) + S(8, text))


def tool_step(name: str, secs: int, args: str = "{}") -> tuple[bytes, bytes]:
    return TS(1, secs) + S(4, S(1, "call_1") + S(2, name) + S(3, args)), V(1, 132)


def make_store(root: Path, cid: str, steps, title="Features overview", cwd="/home/rick", status="CASCADE_RUN_STATUS_IDLE", busy=0):
    conv = root / "conversations"; conv.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(conv / f"{cid}.db")
    c.execute("CREATE TABLE IF NOT EXISTS steps (idx integer, step_type integer NOT NULL DEFAULT 0, status integer NOT NULL DEFAULT 0, "
              "has_subtrajectory numeric NOT NULL DEFAULT false, metadata blob, error_details blob, permissions blob, task_details blob, "
              "render_info blob, step_payload blob, step_format integer NOT NULL DEFAULT 0, PRIMARY KEY (idx))")
    c.execute("DELETE FROM steps")
    for i, (t, (meta, payload)) in enumerate(steps):
        c.execute("INSERT INTO steps(idx, step_type, status, metadata, step_payload) VALUES(?,?,3,?,?)", (i, t, meta, payload))
    c.commit(); c.close()
    s = sqlite3.connect(root / "conversation_summaries.db")
    s.execute("CREATE TABLE IF NOT EXISTS conversation_summaries (conversation_id text, title text NOT NULL DEFAULT '', preview text, "
              "step_count integer, last_modified_time datetime NOT NULL, workspace_uris text NOT NULL, status text NOT NULL DEFAULT '', "
              "not_fully_idle numeric NOT NULL DEFAULT false, killed numeric NOT NULL DEFAULT false, last_user_input_time datetime NOT NULL, "
              "PRIMARY KEY (conversation_id))")
    s.execute("INSERT OR REPLACE INTO conversation_summaries VALUES(?,?,?,?,?,?,?,?,?,?)",
              (cid, title, title, len(steps), "2026-10-30 18:30:54.292766045+00:00", json.dumps([f"file://{cwd}"]), status, busy, 0,
               "2026-10-30 18:30:49.88527015+00:00"))
    s.commit(); s.close()
    return conv / f"{cid}.db"


STEPS = [(USER_INPUT, user_step("first time using antigravity. tell me about its features", 1793384968)),
         (PLANNER_RESPONSE, planner_step("", 1793384970)),
         (132, tool_step("read_file", 1793384971, '{"Path":"/x/SKILL.md"}')),
         (132, tool_step("run_command", 1793384972, '{"CommandLine":"ls"}')),
         (PLANNER_RESPONSE, planner_step("Welcome to **Google Antigravity**! Want the CLI tour or the IDE one?", 1793384975)),
         (USER_INPUT, user_step("the CLI please", 1793385000)),
         (132, tool_step("write_to_file", 1793385001)),
         (PLANNER_RESPONSE, planner_step("Here is the CLI tour.", 1793385005))]


def test_parser_reads_prompts_replies_and_tools_from_the_raw_blobs():
    p = AgyParser()
    rows = [{"idx": i, "step_type": t, "status": 3, "metadata": m, "step_payload": pl} for i, (t, (m, pl)) in enumerate(STEPS)]
    for r in rows:
        p.feed(r, r["idx"] + 1)
    done = p.drain()
    assert [t.user_text for t in done] == ["first time using antigravity. tell me about its features"]
    t0 = done[0]
    assert t0.assistant_text.startswith("Welcome to **Google Antigravity**") and t0.complete
    assert (t0.tally.reads, t0.tally.bash, t0.tally.edits) == (1, 1, 0) and t0.offset_end == 5
    assert t0.started_at == "2026-10-30T18:29:28Z" and t0.ended_at == "2026-10-30T18:29:35Z"
    assert [a.kind for a in t0.asks] == ["plain"] and t0.asks[0].answered and t0.asks[0].answer == "the CLI please"
    cur = p.current
    assert cur.user_text == "the CLI please" and cur.assistant_text == "Here is the CLI tour." and cur.tally.edits == 1 and not cur.complete
    assert p.meta["first_prompt"] == "first time using antigravity. tell me about its features"
    # the parser's state round-trips, so the collector resumes mid-turn
    q = AgyParser.from_state(json.loads(json.dumps(p.state())))
    assert q.current.user_text == "the CLI please" and q.state()["n"] == 2


def test_indexer_treats_the_conversation_db_as_a_transcript(tmp_path):
    root = tmp_path / "agy"
    db = make_store(root, "4abccfc5-7c28-4ad2-bd3c-7c66acbc4384", STEPS[:5], status="CASCADE_RUN_STATUS_IDLE")
    cfg = CollectorConfig(claude_roots=[], pi_roots=[], claude_win_roots=[], agy_roots=[str(root / "conversations")])
    events = []
    ix = Indexer(cfg, "laptop", CollectorState(tmp_path / "c.db"), lambda k, p: events.append((k, p)))
    assert ix.discover() == [(db, "agy")] and ix.harness_for(db) == "agy"
    assert ix.process(db)
    kinds = [k for k, _ in events]
    assert kinds == ["session", "turns"]
    meta = events[0][1]
    assert meta["harness"] == "agy" and meta["session_id"] == "4abccfc5-7c28-4ad2-bd3c-7c66acbc4384" and meta["reset"]
    assert meta["title"] == "Features overview" and meta["cwd"] == "/home/rick" and meta["transcript_path"] == str(db)
    turns = events[1][1]
    assert turns["turns"][0]["complete"] and turns["open"] is None       # agy says idle: the turn closed
    assert turns["turns"][0]["assistant_text"].startswith("Welcome")
    # nothing new: nothing emitted; three more steps: only they are read, from the stored step offset
    events.clear()
    assert ix.poll("agy") == 0 and events == []
    make_store(root, "4abccfc5-7c28-4ad2-bd3c-7c66acbc4384", STEPS, status="CASCADE_RUN_STATUS_BUSY", busy=1)
    os.utime(db, None)
    assert ix.poll("agy") == 1
    t = [p for k, p in events if k == "turns"][-1]
    assert t["open"]["user_text"] == "the CLI please" and t["turns"] == []
    assert ix.state.file(str(db))["offset"] == 8


def test_presence_lock_means_live_and_the_summary_says_busy_or_idle(tmp_path):
    root = tmp_path / "agy"
    cid = "4abccfc5-7c28-4ad2-bd3c-7c66acbc4384"
    make_store(root, cid, STEPS, status="CASCADE_RUN_STATUS_BUSY", busy=1)
    (root / "presence").mkdir()
    lock = root / "presence" / f"{cid}.lock"; lock.touch()
    (root / "presence" / "stale.lock").touch()                       # nobody holds it: not live
    assert read_agy_presence(root / "conversations", "laptop") == {}
    fd = os.open(lock, os.O_RDWR); fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        live = read_agy_presence(root / "conversations", "laptop")
        assert set(live) == {f"laptop/agy/{cid}"}
        assert live[f"laptop/agy/{cid}"].status == "busy" and live[f"laptop/agy/{cid}"].status_since == "2026-10-30T18:30:54Z"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)
    assert read_agy_presence(root / "conversations", "laptop") == {}


def test_agy_store_is_watched_by_default_and_resumes_by_conversation(tmp_path):
    from ctxswitch.client import tmux, fmt, launch
    assert load(tmp_path / "none.toml").collector.agy_roots == ["~/.gemini/antigravity-cli/conversations"]
    (tmp_path / "c.toml").write_text("[collector]\nagy_roots = []\n")
    assert load(tmp_path / "c.toml").collector.agy_roots == []
    s = {"harness": "agy", "session_id": "4abc", "host_id": "laptop"}
    assert tmux.resume_argv(s) == ["agy", "--conversation", "4abc"] == launch.resume_command(s)
    assert fmt.harness_label(s) == "agy" and fmt.can_message(s).startswith("Antigravity")


def test_the_picker_offers_each_claude_account_then_pi_and_antigravity():
    from ctxswitch.client.tui.app import new_session_options
    from ctxswitch.common.config import ClaudeProfile
    cfg = Config(); cfg.claude_profiles = [ClaudeProfile(name="bedrock"), ClaudeProfile(name="max", config_dir="/tmp/cm")]
    have = {"claude": "/usr/bin/claude", "pi": "/usr/bin/pi", "agy": "/usr/bin/agy"}
    assert new_session_options(cfg, have.get) == [("claude · bedrock", "claude", "bedrock"), ("claude · max", "claude", "max"),
                                                  ("pi", "pi", None), ("antigravity", "agy", None)]
    assert new_session_options(Config(), {"claude": "x"}.get) == [("claude", "claude", None)]
    assert new_session_options(Config(), {}.get) == []
