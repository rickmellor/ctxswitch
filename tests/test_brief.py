"""Resume briefing: the hub's hard-signal card, its endpoint, and ctx-hook's brief modes (Claude + pi)."""
import io
import json
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ctxswitch import hook
from ctxswitch.common.config import Config
from ctxswitch.hub.brief import brief
from ctxswitch.hub.store import Store

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def _session(**kw):
    s = {"key": "laptop/claude/s1", "title": "fix the flaky test", "last_activity": "2026-10-29T08:48:00Z", "recap": None,
         "turns": [{"user_text": "make the test pass", "user_origin": "human", "assistant_text": "Done, 12/12 green."},
                   {"user_text": "<task-notification>", "user_origin": "task", "assistant_text": "Noted the CI run."}],
         "asks": [{"kind": "plan", "text": "approve the plan?", "asked_at": "2026-10-29T09:00:00Z", "answered": 0},
                  {"kind": "question", "text": "which branch?", "asked_at": "2026-10-29T08:00:00Z", "answered": 1}]}
    return s | kw


def test_brief_card():
    assert brief(_session(), NOW).splitlines() == [
        "ctx · fix the flaky test · away 3h12",
        "you    : make the test pass",                             # the human prompt, not the task notification
        "agent  : Noted the CI run.",
        "open   : [plan] approve the plan? (3h00 ago)",           # answered asks are not repeated
    ]
    assert "recap  : on track" in brief(_session(recap="on track"), NOW)


def test_brief_carries_the_trackers_purpose_and_your_standing_instructions():
    rule = lambda i, text, active=True: {"id": i, "kind": "instruction", "text": text, "active": active}
    led = [rule(1, "use the pool"), *[rule(i, f"rule {i}") for i in range(4, 7)], {"id": 3, "kind": "claim", "text": "done", "active": True},
           rule(7, "never push", active=False)]
    lines = brief(_session(purpose="Fix the flaky test", recap="on track", ledger=led), NOW).splitlines()
    assert lines[1:6] == ["for    : Fix the flaky test", "recap  : on track",
                          "said   : rule 4", "said   : rule 5", "said   : rule 6"]       # newest MAX_RULES still in force


def test_brief_says_nothing_when_there_is_nothing_to_say():
    assert brief(None, NOW) is None
    assert brief(_session(last_activity="2026-10-29T11:50:00Z"), NOW) is None             # away 10 min: you remember
    assert brief(_session(last_activity="2026-10-29T11:50:00Z"), NOW, after_s=0)
    assert brief(_session(turns=[]), NOW) is None


def test_brief_caps_asks_and_trims():
    asks = [{"kind": "permission", "text": f"allow Bash: {i}", "asked_at": "2026-10-29T09:00:00Z", "answered": 0} for i in range(5)]
    lines = brief(_session(asks=asks, title="t" * 300), NOW).splitlines()
    assert len(lines[0]) < 200 and "…" in lines[0]
    assert sum(l.startswith("open") for l in lines) == 3 and lines[-1].strip() == "… and 2 more (ctx show laptop/claude/s1)"


def test_brief_endpoint(tmp_path):
    from starlette.testclient import TestClient
    from ctxswitch.hub.api import Hub
    hub = Hub(Store(tmp_path / "h.db"), token="t")
    c = TestClient(hub.app())
    hub.store.upsert_session({"key": "laptop/claude/s1", "host_id": "laptop", "harness": "claude", "session_id": "s1",
                              "title": "old work", "last_activity": "2026-10-01T10:00:00Z"})
    hub.store.upsert_turns("laptop/claude/s1", [{"idx": 0, "user_text": "hi", "user_origin": "human", "assistant_text": "hello",
                                                 "complete": True, "offset_end": 10, "started_at": "2026-10-01T10:00:00Z"}], None)
    auth = {"Authorization": "Bearer t"}
    assert c.get("/v1/sessions/laptop/claude/s1/brief").status_code == 401
    text = c.get("/v1/sessions/laptop/claude/s1/brief", headers=auth).json()["text"]
    assert text.startswith("ctx · old work · away ") and "you    : hi" in text
    assert c.get("/v1/sessions/laptop/claude/nope/brief", headers=auth).json() == {"text": None}


class _Hub(BaseHTTPRequestHandler):
    seen: list = []
    reply = {"text": "ctx · t · away 1d"}
    delay = 0.0

    def do_GET(self):
        import time
        time.sleep(self.delay)
        _Hub.seen.append((self.path, self.headers.get("Authorization")))
        body = json.dumps(self.reply).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_hub(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _Hub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = Config(); cfg.hub.url = f"http://127.0.0.1:{srv.server_port}/"; cfg.hub.token = "tok"; cfg.collector.host_id = "lap top"
    monkeypatch.setattr("ctxswitch.common.config.load", lambda *a: cfg)
    _Hub.seen, _Hub.reply, _Hub.delay = [], {"text": "ctx · t · away 1d"}, 0.0
    yield _Hub
    srv.shutdown()


def _main(argv, stdin, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    assert hook.main(argv) == 0
    return capsys.readouterr().out


def test_hook_brief_claude_prints_a_system_message(fake_hub, capsys, monkeypatch):
    ev = {"hook_event_name": "SessionStart", "session_id": "s1", "source": "resume"}
    out = _main(["brief", "claude"], json.dumps(ev), capsys, monkeypatch)
    assert json.loads(out) == {"systemMessage": "ctx · t · away 1d"}
    assert fake_hub.seen == [("/v1/sessions/lap%20top/claude/s1/brief", "Bearer tok")]
    assert _main(["brief", "claude"], json.dumps(ev | {"source": "startup"}), capsys, monkeypatch) == ""
    assert len(fake_hub.seen) == 1                                                       # startup: hub not even asked
    fake_hub.reply = {"text": None}
    assert _main(["brief", "claude"], json.dumps(ev), capsys, monkeypatch) == ""


def test_hook_brief_pi_and_a_slow_hub(fake_hub, capsys, monkeypatch):
    assert _main(["brief", "pi", "p1"], "", capsys, monkeypatch) == "ctx · t · away 1d\n"
    assert fake_hub.seen[-1][0] == "/v1/sessions/lap%20top/pi/p1/brief"
    fake_hub.delay = hook.BRIEF_TIMEOUT_S + 0.5
    assert _main(["brief", "pi", "p1"], "", capsys, monkeypatch) == ""                   # too slow: say nothing, exit 0


def test_hook_brief_without_a_hub(monkeypatch, capsys):
    cfg = Config(); cfg.hub.url = "http://127.0.0.1:9"
    monkeypatch.setattr("ctxswitch.common.config.load", lambda *a: cfg)
    ev = {"hook_event_name": "SessionStart", "session_id": "s1", "source": "resume"}
    assert _main(["brief", "claude"], json.dumps(ev), capsys, monkeypatch) == ""
