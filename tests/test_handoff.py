"""Handoff: continuing a session's work in a different agent (claude -> agy, pi -> claude …)."""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from starlette.testclient import TestClient

from ctxswitch.client import move as mv
from ctxswitch.client.tui.app import new_session_options
from ctxswitch.common.config import ClaudeProfile, Config
from ctxswitch.hub.api import Hub
from ctxswitch.hub.handoff import MARK, marker_in, prompt, render
from ctxswitch.hub.store import Store

NOW = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
OLD = "laptop/claude/s1"
NEW = "laptop/agy/c9"


def _session(store, key, title="OAuth refresh", cwd="/r"):
    h, harness, sid = key.split("/")
    store.upsert_session({"key": key, "host_id": h, "harness": harness, "session_id": sid, "title": title, "cwd": cwd,
                          "repo_remote": "git@x:y.git", "git_branch": "main", "last_activity": NOW})


def _turn(idx, user, agent, complete=True, tally=None):
    return {"idx": idx, "started_at": NOW, "ended_at": NOW, "user_text": user, "user_origin": "human", "assistant_text": agent,
            "tally": tally or {}, "asks": [], "events": [], "complete": complete, "offset_end": idx * 100 + 10}


def _seed(store):
    _session(store, OLD)
    store.upsert_turns(OLD, [_turn(0, "fix the token refresh", "Done: refresh retries once.", tally={"edits": 2, "bash": 1}),
                             _turn(1, "don't touch the billing endpoints", "Understood.")], _turn(2, "now add tests", "", complete=False))
    store.apply_extraction(OLD, _turn(1, "x", "y"), [{"kind": "instruction", "text": "don't touch the billing endpoints", "quote": "don't touch the billing endpoints"},
                                                     {"kind": "claim", "text": "refresh retries once", "quote": "refresh retries once"}],
                           [], [], "make the OAuth refresh robust")


def test_render_has_purpose_rules_claims_and_the_conversation(tmp_path):
    store = Store(tmp_path / "h.db"); _seed(store)
    s = store.session(OLD)
    doc = render(s, "agy", full=True)
    assert doc.startswith("# Handoff: OAuth refresh") and "continues in Google Antigravity" in doc
    assert "## What this work is for\n\nmake the OAuth refresh robust" in doc
    assert "- don't touch the billing endpoints" in doc and "## What the previous agent reported\n\n- refresh retries once" in doc
    assert "**Operator:** fix the token refresh" in doc and "**Agent:** Done: refresh retries once." in doc and "_Tools: 2 edits, 1 bash_" in doc
    assert "**Operator:** now add tests" in doc                       # the open turn too: it's what to do next
    brief = render(s, "pi", full=False)
    assert "## The last 3 exchanges (3 exchanges in all)" in brief
    p = prompt("/tmp/h.md", s, "agy")
    assert "Read /tmp/h.md first" in p and p.endswith(MARK.format(key=OLD)) and marker_in(p) == OLD
    assert marker_in("plain prompt") is None


def test_new_sessions_first_prompt_links_it_and_inherits_instructions_and_purpose(tmp_path):
    store = Store(tmp_path / "h.db"); _seed(store)
    store.note_handoff(OLD, "agy")
    assert store.session(OLD)["handed_off"]["to"] == "agy" and store.session(OLD)["continued_by"] == []
    _session(store, NEW, title="OAuth refresh (agy)")
    store.upsert_turns(NEW, [], _turn(0, prompt("/x.md", store.session(OLD), "agy"), "", complete=False))
    new, old = store.session(NEW), store.session(OLD)
    assert new["continues"] == OLD and old["continued_by"] == [NEW] and old["handed_off"] is None
    rules = [r for r in new["ledger"] if r["kind"] == "instruction" and r["active"]]
    assert [r["text"] for r in rules] == ["don't touch the billing endpoints"] and rules[0]["extra"]["inherited_from"] == OLD
    assert new["purpose"] == "make the OAuth refresh robust"
    # the same prompt arriving again (a re-index) links nothing twice
    store.upsert_turns(NEW, [_turn(0, prompt("/x.md", old, "agy"), "ok")], None)
    assert len([r for r in store.session(NEW)["ledger"] if r["kind"] == "instruction"]) == 1
    # a marker naming a session the hub doesn't know is ignored
    _session(store, "laptop/pi/p1")
    store.upsert_turns("laptop/pi/p1", [], _turn(0, "go " + MARK.format(key="laptop/claude/nope"), "", complete=False))
    assert store.session("laptop/pi/p1")["continues"] is None


def test_api_serves_the_document_and_records_a_started_handoff(tmp_path):
    store = Store(tmp_path / "h.db"); _seed(store)
    c = TestClient(Hub(store, token="t").app())
    h = {"Authorization": "Bearer t"}
    r = c.get(f"/v1/sessions/{OLD}/handoff", params={"to": "pi", "full": "0"}, headers=h)
    assert r.status_code == 200 and r.json()["text"].startswith("# Handoff") and "{path}" in r.json()["prompt"]
    assert c.get("/v1/sessions/laptop/claude/none/handoff", headers=h).status_code == 404
    assert c.post(f"/v1/sessions/{OLD}/handoff", json={"to": "pi"}, headers=h).status_code == 200
    assert c.get(f"/v1/sessions/{OLD}", headers=h).json()["handed_off"]["to"] == "pi"
    assert c.get(f"/v1/sessions/{OLD}/handoff", headers={}).status_code == 401


class FakeClient:
    def __init__(self):
        self.noted = []

    def handoff(self, key, to=None, full=True):
        return {"text": f"# Handoff from {key} to {to} full={full}", "prompt": "Read {path} first. " + MARK.format(key=key)}

    def note_handoff(self, key, to, profile=None):
        self.noted.append((key, to, profile))


def test_client_writes_the_document_and_starts_the_agent_with_it(tmp_path, monkeypatch):
    monkeypatch.setattr(mv, "handoff_dir", lambda: tmp_path)
    s = {"key": OLD, "harness": "claude", "host_id": "laptop", "cwd": str(tmp_path), "live_status": "ended",
         "meta": {"transcript_path": "/home/rick/.claude/projects/-r/s1.jsonl"}}
    started = []
    start = lambda h, cwd, extra, prof: started.append((h, cwd, extra, prof)) or f"new {h} in {cwd}"
    client = FakeClient()
    msg = mv.handoff(s, "agy", None, Config(), client, start)
    (h, cwd, extra, prof), = started
    doc = next(tmp_path.glob("laptop-claude-s1-*.md"))
    assert h == "agy" and cwd == str(tmp_path) and prof is None
    assert extra == ["--add-dir", str(tmp_path), "-i", f"Read {doc} first. {MARK.format(key=OLD)}"]
    assert doc.read_text() == f"# Handoff from {OLD} to agy full=True" and client.noted == [(OLD, "agy", None)]
    assert msg.startswith("handoff to agy: new agy in") and doc.name in msg
    # pi and claude get the prompt as the positional argument; claude also carries the account
    started.clear()
    mv.handoff(s, "pi", None, Config(), client, start, full=False)
    assert started[-1][0] == "pi" and started[-1][2][0].startswith("Read ") and len(started[-1][2]) == 1
    s_pi = {**s, "key": "laptop/pi/p1", "harness": "pi"}
    mv.handoff(s_pi, "claude", "max", Config(), client, start)
    assert started[-1][0] == "claude" and started[-1][3] == "max"
    try:
        mv.handoff(s_pi, "pi", None, Config(), client, start)
        assert False, "same agent"
    except mv.CannotMove as e:
        assert "already a pi session" in str(e)


def test_x_offers_every_other_agent_and_account_but_not_what_it_already_is():
    cfg = Config(); cfg.claude_profiles = [ClaudeProfile(name="bedrock"), ClaudeProfile(name="max", config_dir="/tmp/cm")]
    options = new_session_options(cfg, {"claude": "x", "pi": "x", "agy": "x"}.get)
    corp = {"harness": "claude", "meta": {"transcript_path": "/home/rick/.claude/projects/-r/s1.jsonl"}}
    assert [o[0] for o in mv.targets(corp, options, cfg.claude_profiles)] == ["claude · max", "pi", "antigravity"]
    pi = {"harness": "pi", "meta": {}}
    assert [o[0] for o in mv.targets(pi, options, cfg.claude_profiles)] == ["claude · bedrock", "claude · max", "antigravity"]
    agy = {"harness": "agy", "meta": {}}
    assert [o[0] for o in mv.targets(agy, options, cfg.claude_profiles)] == ["claude · bedrock", "claude · max", "pi"]
    one = Config()
    assert [o[0] for o in mv.targets(corp, new_session_options(one, {"claude": "x", "pi": "x"}.get), one.claude_profiles)] == ["pi"]
