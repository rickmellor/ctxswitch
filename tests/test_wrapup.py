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


def test_the_legend_explains_every_glyph_the_board_draws():
    from ctxswitch.client import tmux
    text = tui.legend().plain
    glyphs = [*fmt.GLYPH.values(), *(g for r, g in tmux.REACH_GLYPH.items() if r != "none"), *fmt.HARNESS.values(),
              *fmt.MSG_GLYPH.values(), *fmt.CLAIM_GLYPH.values(), "▶", "▾", "+2", "◐4m"]
    lines = text.splitlines()
    for g in set(glyphs) - {"·"}:                                      # "·" is the claim fallback, never drawn alone
        assert any(ln.strip().startswith(g) for ln in lines), g
    for words in tui.STATE_HELP.values():
        assert words in text


def test_a_rows_tooltip_says_it_in_words():
    from datetime import datetime, timezone
    now = datetime(2026, 10, 29, 10, 10, tzinfo=timezone.utc)
    s = {"key": "devbox/pi/p1", "host_id": "devbox", "harness": "pi", "live_status": "idle", "status_since": "2026-10-29T10:07:00Z",
         "repo_root": "/r/proj", "git_branch": "main", "unseen": 2,
         "open_asks": [{"kind": "plain", "text": "Ship it?", "asked_at": "2026-10-29T10:06:00Z"}]}
    tip = tui.row_tip(s, "pane", False, {}, now).splitlines()
    assert tip[0] == "◐ waiting on you: a question, plan, permission prompt or dialog, 3m"
    assert tip[1] == "◐ [plain] waiting 4m: Ship it?"
    assert tip[2].startswith("◆ in ctx's tmux") and tip[3] == "+2 exchanges since you last looked"
    assert tip[4].startswith("pi on devbox · proj")
    ended = {"key": "k", "host_id": "laptop", "harness": "claude", "live_status": "ended", "ended": True, "last_activity": "2026-10-29T08:10:00Z"}
    t2 = tui.row_tip(ended, "resume", True, {}, now).splitlines()
    assert t2[0] == "– ended (shown with e), last active 2h00 ago" and t2[1] == "▶ on the left now" and len(t2) == 3
    b = SimpleNamespace(sessions=[s], reach={s["key"]: "pane"}, visible_key=None, cfg=SimpleNamespace(ssh={}))
    assert tui.CtxApp.tip_for(b, s["key"]).startswith("◐ waiting") and tui.CtxApp.tip_for(b, "gone") is None
    assert tui.CtxApp.tip_for(b, "ws:me/proj@main").startswith("▾ proj@main: the sessions")


# -- numbered rows and Alt+1…9 ----------------------------------------------------------------------------------
def test_every_alt_digit_a_terminal_sends_reaches_its_slot():
    from textual import events
    from textual._xterm_parser import XTermParser
    bound = {k.strip(): b.action for b in tui.CtxApp.BINDINGS for k in b.key.split(",")}
    for n in range(1, tui.SLOTS + 1):
        p = XTermParser()
        (key,) = [e.key for e in [*p.feed(f"\x1b{n}"), *p.feed("")] if isinstance(e, events.Key)]   # what tmux / WT send
        assert bound.get(key) == f"slot({n})", (n, key)


def test_alt_n_opens_the_session_numbered_n():
    opened, said = [], []
    b = SimpleNamespace(slots=["k1", "k2"], open_key=opened.append, notify=lambda m, **k: said.append(m))
    tui.CtxApp.action_slot(b, 2)
    tui.CtxApp.action_slot(b, 3)
    tui.CtxApp.action_slot(b, 0)
    assert opened == ["k2"] and said == ["no session 3 on the board", "no session 0 on the board"]


def test_the_board_binds_alt_digits_to_its_own_pane_in_tmux(monkeypatch):
    calls = []
    monkeypatch.setattr(tui.tmux, "set_role", lambda p, r: calls.append(("role", p, r)))
    monkeypatch.setattr(tui.tmux, "bind_slot_keys", lambda p: calls.append(("bind", p)))
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(tui.tmux, "in_tmux", lambda: False)
    tui.CtxApp.join_tmux(None)
    assert calls == []
    monkeypatch.setattr(tui.tmux, "in_tmux", lambda: True)
    tui.CtxApp.join_tmux(None)
    assert calls == [("role", "%7", "tui"), ("bind", "%7")]


async def test_the_board_numbers_its_session_rows_in_the_order_shown(tmp_path, monkeypatch):
    store = Store(tmp_path / "hub.db")
    extra = [f"laptop/claude/x{i}" for i in range(8)]                  # 11 sessions: only 9 numbers
    for k, st in ((LAP, "idle"), ("laptop/claude/s9", "busy"), (DEV, "idle"), *((x, "idle") for x in extra)):
        _session(store, k)
        store.upsert_turns(k, [{"idx": 0, "started_at": "2026-10-29T10:00:00Z", "ended_at": "2026-10-29T10:00:00Z", "user_text": "go",
                                "user_origin": "human", "assistant_text": "", "tally": {}, "asks": [], "events": [], "complete": True, "offset_end": 1}], None)
    _live(store, "laptop", (LAP, "idle"), ("laptop/claude/s9", "busy"), *((x, "idle") for x in extra)); _live(store, "devbox", (DEV, "idle"))
    cfg = Config(); cfg.hub.token = "t"; cfg.collector.host_id = "laptop"
    monkeypatch.setattr(tui.tmux, "in_tmux", lambda: False)
    app = tui.CtxApp(cfg, None)
    app.client._c = TestClient(Hub(store, token="t").app(), headers=H)
    monkeypatch.setattr(tui.CtxApp, "listen_ws", lambda self: None)
    joined = []
    monkeypatch.setattr(tui.CtxApp, "join_tmux", lambda self: joined.append(1))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert joined == [1]                                              # at start: role + Alt+N bindings
        t = app.query_one("#table")
        nums = [t.get_row_at(i)[0].plain for i in range(t.row_count)]
        assert nums == [str(n) for n in range(1, 10)] + ["", ""] and app.slots[0] == "laptop/claude/s9"   # busy first; 10+ unnumbered
        app.action_toggle_ws(); await pilot.pause(0.3)
        rows = [(t.get_row_at(i)[0].plain, str(t.coordinate_to_cell_key((i, 0)).row_key.value)) for i in range(t.row_count)]
        sessions = [(n, k) for n, k in rows if not k.startswith("ws:")]
        assert [k for _, k in sessions] == app.slots and all(not n for n, k in rows if k.startswith("ws:"))   # headers unnumbered
        assert [n for n, _ in sessions] == [str(i) for i in range(1, 10)] + ["", ""]                         # still 1…9 in order
        opened = []
        monkeypatch.setattr(app, "open_key", opened.append)
        await pilot.press(tui.slot_key(2)); await pilot.pause(0.2)
        assert opened == [app.slots[1]]
