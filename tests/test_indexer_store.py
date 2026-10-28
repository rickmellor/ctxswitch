import json
import os
import time
from pathlib import Path

from claude_rows import S, assistant, system, text, tool_use, user

from ctxswitch.collector.indexer import Indexer, session_id_from_path
from ctxswitch.collector.state import CollectorState
from ctxswitch.common.config import CollectorConfig
from ctxswitch.hub.store import Store


def _setup(tmp_path):
    root = tmp_path / "claude" / "-home-rick-repos-demo"; root.mkdir(parents=True)
    piroot = tmp_path / "pi" / "--home-rick--"; piroot.mkdir(parents=True)
    cfg = CollectorConfig(claude_roots=[str(tmp_path / "claude")], pi_roots=[str(tmp_path / "pi")], backfill_days=30)
    events = []
    st = CollectorState(tmp_path / "c.db")
    ix = Indexer(cfg, "laptop", st, lambda k, p: events.append((k, p)))
    return root, piroot, ix, events, st


def _write(path: Path, rows, partial: str | None = None, append=False):
    with open(path, "ab" if append else "wb") as f:
        for r in rows:
            f.write((json.dumps(r) + "\n").encode())
        if partial is not None:
            f.write(partial.encode())


def test_indexer_incremental_partial_line_and_resume(tmp_path):
    root, piroot, ix, events, st = _setup(tmp_path)
    f = root / f"{S}.jsonl"
    _write(f, [user("hello", "2026-10-28T10:00:00Z"), assistant([text("hi, what next?")], "2026-10-28T10:00:01Z")],
           partial='{"type":"system","subtype":"turn_dur')
    ix.live_keys = {f"laptop/claude/{S}"}
    assert ix.scan_all() == 1
    kinds = [k for k, _ in events]
    assert kinds == ["session", "turns"]
    sess = events[0][1]
    assert sess["key"] == f"laptop/claude/{S}" and sess["title"] == "hello" and sess["git_branch"] == "feat/x" and not sess["ended"]
    assert events[1][1]["turns"] == [] and events[1][1]["open"]["assistant_text"] == "hi, what next?"
    rec = st.file(str(f))
    assert rec["offset"] == f.stat().st_size - len('{"type":"system","subtype":"turn_dur')
    # complete the partial line + a new prompt; resume from persisted state in a fresh indexer
    _write(f, [], partial='ation","timestamp":"2026-10-28T10:00:02Z","sessionId":"%s"}\n' % S, append=True)
    _write(f, [user("do more", "2026-10-28T10:01:00Z")], append=True)
    events2 = []
    ix2 = Indexer(ix.cfg, "laptop", CollectorState(tmp_path / "c.db"), lambda k, p: events2.append((k, p)))
    ix2.live_keys = ix.live_keys
    assert ix2.process(f)
    turns_ev = [p for k, p in events2 if k == "turns"][0]
    assert len(turns_ev["turns"]) == 1
    t0 = turns_ev["turns"][0]
    assert t0["complete"] and t0["asks"][0]["kind"] == "plain" and t0["asks"][0]["answer"] == "do more"
    assert turns_ev["open"]["user_text"] == "do more"
    # nothing new -> nothing emitted
    events2.clear()
    assert not ix2.process(f) and events2 == []


def test_indexer_marks_old_non_live_file_ended(tmp_path):
    root, piroot, ix, events, st = _setup(tmp_path)
    f = root / f"{S}.jsonl"
    _write(f, [user("bye", "2026-10-28T10:00:00Z"), assistant([text("done.")], "2026-10-28T10:00:01Z")])
    old = time.time() - 3600
    os.utime(f, (old, old))
    ix.live_keys = set()
    ix.scan_all()
    turns_ev = [p for k, p in events if k == "turns"][0]
    assert turns_ev["ended"] and turns_ev["turns"][0]["complete"] and turns_ev["open"] is None
    assert [p for k, p in events if k == "session"][0]["ended"]
    assert st.file(str(f))["ended"]
    # backfill cutoff: a file older than backfill_days is skipped on discovery
    g = root / "00000000-0000-4000-8000-000000000001.jsonl"
    _write(g, [user("ancient", "2025-01-01T00:00:00Z")]); os.utime(g, (old - 90 * 86400, old - 90 * 86400))
    assert all(p != g for p, _ in ix.discover())


def test_pi_session_id_from_path():
    assert session_id_from_path(Path("/x/--home--/2026-10-22T16-31-51-180Z_01a16474-618b.jsonl"), "pi") == "01a16474-618b"
    assert session_id_from_path(Path(f"/x/-home-/{S}.jsonl"), "claude") == S


def test_store_ingest_idempotent_and_attention(tmp_path):
    store = Store(tmp_path / "hub.db")
    key = "laptop/claude/s1"
    meta = {"key": key, "host_id": "laptop", "harness": "claude", "session_id": "s1", "title": "T", "cwd": "/r", "repo_root": "/r",
            "repo_remote": "git@x:y.git", "git_branch": "main", "started_at": "2026-10-28T10:00:00Z", "last_activity": "2026-10-28T10:00:00Z"}
    store.upsert_session(meta)
    open_turn = {"idx": 0, "started_at": "2026-10-28T10:00:00Z", "ended_at": "2026-10-28T10:00:05Z", "user_text": "q", "user_origin": "human",
                 "assistant_text": "", "tally": {"edits": 0, "bash": 0, "reads": 0, "agents": 0, "other": 1, "errors": 0},
                 "asks": [{"kind": "question", "text": "A or B?", "ref": "t1", "answered": False, "answer": None, "answered_at": None}],
                 "events": [], "complete": False, "offset_end": 200}
    store.upsert_turns(key, [], open_turn)
    store.set_liveness("laptop", [{"key": key, "status": "busy", "status_since": "2026-10-28T10:00:05Z", "pid": 5, "socket": "/s"}])
    att = store.attention()
    assert len(att) == 1 and att[0]["item"] == "ask" and att[0]["text"] == "A or B?" and att[0]["live_status"] == "busy"
    # replay of an OLDER version must not regress the row
    newer = dict(open_turn, assistant_text="chose A", complete=True, offset_end=400,
                 asks=[dict(open_turn["asks"][0], answered=True, answer="A", answered_at="2026-10-28T10:01:00Z")])
    store.upsert_turns(key, [newer], None)
    store.upsert_turns(key, [], open_turn)          # stale replay
    s = store.session(key)
    assert s["turns"][0]["assistant_text"] == "chose A" and s["asks"][0]["answered"] == 1
    assert store.attention() == []
    # liveness snapshot: a session missing from the snapshot becomes unknown; idle shows in attention
    store.set_liveness("laptop", [])
    assert store.sessions()[0]["live_status"] == "unknown"
    store.set_liveness("laptop", [{"key": key, "status": "idle", "status_since": "2026-10-28T10:02:00Z", "pid": 5, "socket": "/s"}])
    assert store.attention()[0]["item"] == "idle"
    # placeholder for a live session whose transcript isn't indexed yet
    store.set_liveness("laptop", [{"key": "laptop/claude/s2", "status": "busy", "status_since": None, "pid": 6, "socket": None}])
    keys = {x["key"] for x in store.sessions()}
    assert "laptop/claude/s2" in keys and store.sessions(host="laptop", include_ended=False)[0]["key"] == "laptop/claude/s2"
    store.upsert_session(dict(meta, ended=True))
    assert store.session(key)["live_status"] == "ended"
    store.mark_viewed(key, "2026-10-28T11:00:00Z")
    assert store.session(key)["last_viewed_at"] == "2026-10-28T11:00:00Z"
    assert store.change > 0
