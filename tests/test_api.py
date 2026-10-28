from starlette.testclient import TestClient

from ctxswitch.hub.api import Hub
from ctxswitch.hub.store import Store

META = {"key": "devbox/pi/p1", "host_id": "devbox", "harness": "pi", "session_id": "p1", "title": "pi thing", "cwd": "/r",
        "repo_root": "/r", "repo_remote": "", "git_branch": "main", "started_at": "2026-10-28T10:00:00Z", "last_activity": "2026-10-28T10:00:00Z"}
TURN = {"idx": 0, "started_at": "2026-10-28T10:00:00Z", "ended_at": "2026-10-28T10:00:09Z", "user_text": "hi", "user_origin": "human",
        "assistant_text": "Which one?", "tally": {"edits": 0, "bash": 0, "reads": 0, "agents": 0, "other": 0, "errors": 0},
        "asks": [{"kind": "plain", "text": "Which one?", "ref": "0:plain", "answered": False, "answer": None, "answered_at": None}],
        "events": [], "complete": True, "offset_end": 10}


def _ingest(c, events, host="devbox", token=None):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    return c.post("/v1/ingest", json={"host_id": host, "events": events}, headers=h)


def test_ingest_query_attention_viewed_and_auth(tmp_path):
    hub = Hub(Store(tmp_path / "h.db"), token="s3cret")
    c = TestClient(hub.app())
    assert c.get("/v1/health").json()["ok"]
    assert _ingest(c, []).status_code == 401
    assert c.get("/v1/sessions").status_code == 401
    r = _ingest(c, [{"seq": 1, "kind": "session", "payload": META},
                    {"seq": 2, "kind": "turns", "payload": {"key": "devbox/pi/p1", "turns": [TURN], "open": None}},
                    {"seq": 3, "kind": "bogus", "payload": {}},
                    {"seq": 4, "kind": "liveness", "payload": {"host_id": "devbox", "sessions": [{"key": "devbox/pi/p1", "status": "idle", "status_since": "2026-10-28T10:00:10Z", "pid": 1, "socket": None}]}}],
                token="s3cret")
    assert r.status_code == 200 and r.json()["acked"] == 4 and r.json()["applied"] == 4
    H = {"Authorization": "Bearer s3cret"}
    s = c.get("/v1/sessions", params={"host": "devbox"}, headers=H).json()["sessions"]
    assert len(s) == 1 and s[0]["live_status"] == "idle" and s[0]["open_asks"][0]["text"] == "Which one?"
    assert c.get("/v1/sessions", params={"host": "laptop"}, headers=H).json()["sessions"] == []
    att = c.get("/v1/attention", headers=H).json()["items"]
    assert len(att) == 1 and att[0]["item"] == "ask"
    one = c.get("/v1/sessions/devbox/pi/p1", headers=H).json()
    assert one["turns"][0]["assistant_text"] == "Which one?" and one["title"] == "pi thing"
    assert c.get("/v1/sessions/nope/x/y", headers=H).status_code == 404
    assert c.post("/v1/sessions/devbox/pi/p1/viewed", json={"at": "2026-10-28T11:00:00Z"}, headers=H).json()["ok"]
    assert c.get("/v1/sessions/devbox/pi/p1", headers=H).json()["last_viewed_at"] == "2026-10-28T11:00:00Z"
    assert [h["host_id"] for h in c.get("/v1/hosts", headers=H).json()] == ["devbox"]
    with c.websocket_connect("/v1/ws?token=s3cret") as ws:
        first = ws.receive_json()
        assert first["change"] == hub.store.change
