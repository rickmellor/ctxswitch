"""Workstream messages (to whichever session is, or next is, live on a repo + branch), retiring an instruction from
the CLI and the board."""
from types import SimpleNamespace

from starlette.testclient import TestClient
from test_phase45 import _session

from ctxswitch.client import fmt
from ctxswitch.client.hubclient import HubClient
from ctxswitch.client.tui import app as tui
from ctxswitch.common.config import Config
from ctxswitch.hub.api import Hub
from ctxswitch.hub.store import Store

H = {"Authorization": "Bearer t"}
WS = "me/proj@main"
LAP, DEV, IDLE, WIN = "laptop/claude/s1", "devbox/pi/p1", "devbox/claude/s2", "laptop/claude-win/w1"


def _live(store, host, *keys, status="idle"):
    store.set_liveness(host, [{"key": k, "status": st, "status_since": "2026-10-29T10:00:00Z", "pid": 1, "socket": "/x"}
                              for k, st in (k if isinstance(k, tuple) else (k, status) for k in keys)])


def test_a_workstream_message_goes_to_the_first_session_live_on_that_work(tmp_path):
    store = Store(tmp_path / "hub.db")
    for k in (LAP, DEV, IDLE, WIN):
        _session(store, k, live=False)
    _session(store, "devbox/claude/other", live=False, remote="git@github.com:me/other.git", when="2026-10-29T11:00:00Z")   # newest
    c = TestClient(Hub(store, token="t").app())
    post = lambda **b: c.post("/v1/workstreams/messages", headers=H, json=b)
    assert post(workstream="nobody/works@here", body="x").status_code == 404
    assert post(workstream=WS, body="  ").status_code == 400
    mid = post(workstream=WS, body="the hub token moved", from_host="laptop").json()["id"]
    assert [m["id"] for m in store.session(LAP)["workstream_messages"]] == [mid]           # shown on every session of it
    assert store.session("devbox/claude/other")["workstream_messages"] == []
    _live(store, "laptop", WIN)                                       # Windows Claude can't take one
    assert store.outbox("laptop") == [] and store.session(LAP)["workstream_messages"] != []
    _live(store, "devbox", (DEV, "busy"), (IDLE, "idle"), ("devbox/claude/other", "idle"))
    (m,) = store.outbox("devbox")
    assert (m["id"], m["key"], m["body"]) == (mid, IDLE, "the hub token moved")             # idle before busy, same work only
    assert store.session(LAP)["workstream_messages"] == [] and store.messages(IDLE)[0]["workstream"] == WS
    _live(store, "laptop", LAP)
    assert store.outbox("laptop") == []                               # taken already: not twice
    store.set_message_status(mid, "devbox", "sent")
    assert store.messages(IDLE)[0]["status"] == "sent"
    gone = post(workstream=WS, body="never mind").json()["id"]
    assert store.cancel_message(gone) and store.session(LAP)["workstream_messages"] == []
    _live(store, "devbox", (DEV, "busy"))
    later = post(workstream=WS, body="later").json()["id"]
    assert [x["key"] for x in store.outbox("devbox") if x["id"] == later] == [DEV]          # busy is still live


def test_the_client_and_cli_send_to_a_sessions_work(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path / "hub.db")
    _session(store, LAP)
    _session(store, "devbox/claude/norepo", remote="", root="")
    cfg = Config(); cfg.hub.token = "t"
    client = HubClient(cfg); client._c = TestClient(Hub(store, token="t").app(), headers=H)
    from ctxswitch.client import cli
    monkeypatch.setattr(cli, "_client", lambda: client)
    args = lambda key, *text: SimpleNamespace(key=key, text=list(text), cancel=None, buffer=False, work=True)
    assert cli.cmd_send(args("s1", "hello", "there")) == 0
    assert "queued for proj@main" in capsys.readouterr().out
    assert [m["body"] for m in store.workstream_messages(WS)] == ["hello there"]
    assert cli.cmd_send(args("norepo", "x")) == 1 and "no workstream" in capsys.readouterr().out
    store.apply_extraction(LAP, {"idx": 0}, [{"kind": "instruction", "text": "don't push", "quote": "q", "extra": {}}], [], [], None)
    (row,) = fmt.instructions(store.session(LAP))
    assert cli.cmd_retire(SimpleNamespace(key="s1", id=row["id"])) == 0 and fmt.instructions(store.session(LAP)) == []
    assert cli.cmd_retire(SimpleNamespace(key="s1", id=row["id"])) == 1 and "no active row" in capsys.readouterr().out.splitlines()[-1]


def test_show_lists_work_messages_nobody_has_taken(tmp_path):
    store = Store(tmp_path / "hub.db")
    _session(store, LAP)
    mid = store.add_workstream_message(WS, "the token moved")
    lines = fmt.show_lines(store.session(LAP), 0)
    i = lines.index("  for this work (proj@main), not taken yet:")
    assert lines[i + 1] == f"    {fmt.MSG_GLYPH['queued']} [{mid}] the token moved" and lines[i + 2].strip() == fmt.WORK_NOTE


def _board(**kw):
    seen = []
    b = SimpleNamespace(input_mode="filter", pending_rules=None, selected=LAP, detail=None, sessions=[],
                        _hide_input=lambda: None, query_one=lambda *a: SimpleNamespace(value=""),
                        _show_input=lambda mode, prompt, *a: seen.append(("input", mode, prompt)),
                        notify=lambda msg, **k: seen.append(("notify", msg)),
                        retire_row=lambda k, i: seen.append(("retire", k, i)), send_work=lambda ws, body: seen.append(("work", ws, body)))
    b._selected_session = lambda: next((x for x in b.sessions if x["key"] == b.selected), None)
    b.action_message_work = lambda: tui.CtxApp.action_message_work(b)
    for k, v in kw.items():
        setattr(b, k, v)
    return b, seen


def test_x_retires_an_instruction_by_its_number():
    rules = [{"kind": "instruction", "active": 1, "id": 11, "text": "a"}, {"kind": "instruction", "active": 0, "id": 12, "text": "b"},
             {"kind": "instruction", "active": 1, "id": 13, "text": "c"}]
    b, seen = _board(detail={"key": "other", "ledger": rules})
    tui.CtxApp.action_retire(b)
    assert seen == [("notify", "no instructions in force to retire")]   # the detail shown is another session's
    b.detail["key"] = LAP; seen.clear()
    tui.CtxApp.action_retire(b)
    assert seen == [("input", "retire", "retire which instruction (1-2)? enter")] and b.pending_rules == (LAP, [11, 13])
    b.input_mode = "retire"
    tui.CtxApp.on_input_submitted(b, SimpleNamespace(value=" 2 "))
    assert seen[-1] == ("retire", LAP, 13) and b.pending_rules is None and b.input_mode == "filter"
    for bad in ("3", "0", "x"):
        b.input_mode, b.pending_rules = "retire", (LAP, [11, 13])
        tui.CtxApp.on_input_submitted(b, SimpleNamespace(value=bad))
        assert seen[-1] == ("notify", f"no instruction {bad!r}")


def test_m_on_a_workstream_header_and_W_message_the_work():
    b, seen = _board(selected="ws:" + WS)
    tui.CtxApp.action_message(b)
    assert seen[-1] == ("input", "work:" + WS, "message to proj@main (the first session live on it, any host), enter to send")
    b.selected, b.sessions = LAP, [{"key": LAP, "workstream": "me/other@dev"}]
    tui.CtxApp.action_message_work(b)
    assert seen[-1][1] == "work:me/other@dev"
    b.sessions = [{"key": LAP, "workstream": None}]
    tui.CtxApp.action_message_work(b)
    assert seen[-1] == ("notify", "not in a repo: no workstream to message")
    b.input_mode = "work:" + WS
    tui.CtxApp.on_input_submitted(b, SimpleNamespace(value="go"))
    assert seen[-1] == ("work", WS, "go")


def test_starting_a_new_agent_logs_and_reports_it(monkeypatch):
    logged, seen = [], []
    monkeypatch.setattr(tui, "popup_log", logged.append)
    monkeypatch.setattr(tui.tmux, "new_agent", lambda harness, cwd: f"started {harness}")
    b = SimpleNamespace(call_from_thread=lambda fn, *a, **k: fn(*a, **k), notify=lambda msg, **k: seen.append(msg))
    tui.CtxApp.start_agent.__wrapped__(b, "pi", "/r")                 # was a NameError, raised in the worker
    assert logged == ["new pi in /r: started pi"] and seen == ["started pi"]
