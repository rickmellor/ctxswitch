"""The misplaced-prompt detector, board side: ↯ on a row that has one, a MISPLACED? section in the detail saying
where the prompt looks like it belongs, and the two keys that settle it — t sends it there, o keeps it here.

The hub's ``misplaced`` table, its /v1/prompt-check matcher and the hook are on other branches, so everything here
feeds the client the contract's rows directly (a fake client): session() carries "misplaced", the list rows carry
"misplaced_open", and misplaced_state() records what was chosen. A hub that returns neither must change nothing."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from ctxswitch.client import fmt
from ctxswitch.client.hubclient import HubClient
from ctxswitch.client.tui import app as tui
from ctxswitch.common.config import Config

NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)
A, B = "gpu-host/claude/aaaa", "devbox/claude/bbbb"


def _ago(mins: int) -> str:
    return (NOW - timedelta(minutes=mins)).isoformat().replace("+00:00", "Z")


def _sess(key, *, title="webapp@main work", cwd="/r/webapp", status="idle", host=None, **kw):
    host = host or key.split("/", 1)[0]
    s = {"key": key, "host_id": host, "harness": "claude", "session_id": key.rsplit("/", 1)[-1], "title": title,
         "cwd": cwd, "live_status": status, "status_since": _ago(20), "last_activity": _ago(2),
         "started_at": _ago(60), "repo_root": "/r/webapp", "git_branch": "main", "turn_count": 3, "meta": {},
         "open_asks": [], "parent_key": "", "job": "", "job_status": "", "job_brief": "", "turns": [], "asks": [],
         "ledger": [], "messages": [], "misplaced": []}
    return {**s, **kw}


def _row(id=7, key=A, verdict="other", target=B, target_title="auth-service@main work", source="pre",
         prompt="rename the retry helper in the auth service", why="only that session's repo has a retry helper",
         state="open", at=None):
    """One ``misplaced`` row, every column the contract lists."""
    return {"id": id, "key": key, "prompt_id": f"p{id}", "prompt": prompt, "source": source, "verdict": verdict,
            "target_key": target or "", "target_title": target_title or "", "why": why, "confidence": 0.9,
            "state": state, "at": at or _ago(3), "resolved_at": None if state == "open" else _ago(1)}


class FakeClient:
    """The hub as the contract describes it: the rows a test hands it, and a record of what the board asked for."""

    def __init__(self, rows, detail=None):
        self.rows = rows
        self.detail = detail or {}
        self.sent: list[tuple[str, str]] = []
        self.states: list[tuple[int, str]] = []

    def sessions(self, host=None, live_only=False):
        return {"sessions": [dict(x) for x in self.rows], "change": 1}

    def session(self, key, turns=50):
        s = self.detail.get(key)
        if s is None:
            s = next((dict(x) for x in self.rows if x["key"] == key), None)
            if s is not None:
                s.update(turns=[], asks=[], ledger=[], messages=[])
        return s

    def attention(self, host=None):
        return {"items": []}

    def hosts(self):
        return [{"host_id": "gpu-host"}, {"host_id": "devbox"}]

    def send(self, key, body, from_host=None, answers=None, from_key=None):
        self.sent.append((key, body))
        return len(self.sent)

    def misplaced_state(self, id, state):
        self.states.append((id, state))
        return True

    def viewed(self, key, at):
        pass

    def spend(self, frm, to, host=None):
        raise ValueError("HTTP 404")

    def ws_url(self):
        return "ws://x/v1"


def _board(monkeypatch, rows, detail=None) -> tui.CtxApp:
    cfg = Config(); cfg.hub.token = "t"; cfg.collector.host_id = "gpu-host"
    monkeypatch.setattr(tui.tmux, "in_tmux", lambda: False)
    monkeypatch.setattr(tui.tmux, "reap_viewer", lambda *a, **k: None)
    monkeypatch.setattr(tui.tmux, "panes", lambda *a, **k: [])
    monkeypatch.setattr(tui.tmux, "visible_key", lambda ps=None, sessions=None: None)
    monkeypatch.setattr(tui.tmux, "on_screen_keys", lambda ps=None, sessions=None: set())
    monkeypatch.setattr(tui.tmux, "reach_all", lambda sessions, host=None, ssh=None: {})
    monkeypatch.setattr(tui.CtxApp, "listen_ws", lambda self: None)
    monkeypatch.setattr(tui.CtxApp, "join_tmux", lambda self: None)
    monkeypatch.setattr(tui, "datetime", _Frozen)
    app = tui.CtxApp(cfg, None)
    app.client = FakeClient(rows, detail)
    return app


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


def _rows_of(t) -> list[tuple[str, str, str]]:
    return [(t.get_row_at(i)[0].plain, t.get_row_at(i)[3].plain, t.coordinate_to_cell_key((i, 0)).row_key.value)
            for i in range(t.row_count)]


def _confirm(app):
    """The board's y/n, if one is up."""
    return next((s for s in app.screen_stack if isinstance(s, tui.Confirm)), None)


def _key_press(widget, key: str) -> None:
    from textual.events import Key
    widget.post_message(Key(key, character=None))


# -- the row's ↯ --------------------------------------------------------------------------------------------------
async def test_a_session_with_an_open_flag_gets_the_mark_and_a_session_without_one_does_not(monkeypatch):
    a = _sess(A, misplaced_open=1)
    b = _sess(B, misplaced_open=0)
    app = _board(monkeypatch, [a, b])
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        rows = _rows_of(app.query_one("#table"))
        assert rows[0][2] == A and rows[0][1].endswith("↯") and rows[0][1].count("↯") == 1
        assert rows[1][2] == B and "↯" not in rows[1][1]


async def test_the_mark_comes_from_the_detail_rows_when_the_list_only_has_a_count(monkeypatch):
    """A row resolved since the last list refresh: the detail decides, and a session with only resolved rows is clean."""
    row = _row(state="kept")
    app = _board(monkeypatch, [_sess(A, misplaced_open=1)], detail={A: {**_sess(A), "misplaced": [row]}})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert "↯" not in _rows_of(app.query_one("#table"))[0][1]


async def test_a_pool_job_row_never_shows_the_mark(monkeypatch):
    """Its prompts belong to the session that launched it; a job's row stays one line with no marks."""
    parent = _sess(A)
    kid = _sess("gpu-host/claude/kk", parent_key=A, job="sa-1-j1", job_brief="b.md", job_status="ok",
                misplaced_open=2)
    app = _board(monkeypatch, [parent, kid])
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        rows = _rows_of(app.query_one("#table"))
        assert rows[0][2] == A and "↯" not in rows[0][1]
        assert rows[1][2] == "gpu-host/claude/kk" and "↯" not in rows[1][1] and "\n" not in rows[1][1]


# -- the detail's MISPLACED? section ------------------------------------------------------------------------------
async def test_the_detail_says_where_each_prompt_looks_like_it_belongs(monkeypatch):
    other = _row(id=7, source="pre")
    drift = _row(id=8, verdict="drift", target="", target_title="", source="llm", at=_ago(9),
                 prompt="also add a png to the release notes", why="nothing here touches release notes")
    detail = {**_sess(A), "misplaced": [drift, other],
              "asks": [{"ref": "0:plain", "kind": "plain", "text": "Tag it?", "answered": False, "asked_at": _ago(4)}]}
    app = _board(monkeypatch, [{**_sess(A), "misplaced_open": 2}, _sess(B)], detail={A: detail})
    async with app.run_test(size=(70, 40)) as pilot:
        await pilot.pause(1)
        t = app.query_one("#detail_text").render().plain
        want = ['MISPLACED?',
                '↯ 3m  "rename the retry helper in the auth service"',
                "   looks like it's for auth-service@main work — only that session's repo has a retry helper",
                "   blocked before the agent saw it",
                "   t move it there · o keep it here",
                '↯ 9m  "also add a png to the release notes"',
                "   off this session's purpose — nothing here touches release notes",
                "   the agent may have started on it",
                "   t start a new session with it · o keep it here"]
        for ln in want:
            assert ln in t.splitlines(), (ln, t)
        assert t.index("WAITING ON YOU") < t.index("MISPLACED?")   # right after it, before the state and short-key lines
        assert t.index("MISPLACED?") < t.index("gpu-host/cc/aaaa")


async def test_a_detail_with_nothing_flagged_says_nothing(monkeypatch):
    app = _board(monkeypatch, [_sess(A)], detail={A: {**_sess(A), "misplaced": []}})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert "MISPLACED?" not in app.query_one("#detail_text").render().plain


# -- o: keep it ---------------------------------------------------------------------------------------------------
async def test_o_keeps_the_newest_open_row(monkeypatch):
    older, newer = _row(id=7, at=_ago(30)), _row(id=8, at=_ago(2))
    app = _board(monkeypatch, [_sess(A, misplaced_open=2)], detail={A: {**_sess(A), "misplaced": [older, newer]}})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        await pilot.press("o"); await pilot.pause(0.6)
        assert app.client.states == [(8, "kept")] and app.client.sent == []


async def test_o_on_a_session_with_nothing_flagged_records_nothing(monkeypatch):
    app = _board(monkeypatch, [_sess(A)])
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        await pilot.press("o"); await pilot.pause(0.5)
        assert app.client.states == [] and app.client.sent == []


# -- t: move it to the session it names ---------------------------------------------------------------------------
async def test_t_on_a_prompt_the_agent_never_saw_sends_it_and_records_moved(monkeypatch):
    row = _row(id=7, source="pre")
    app = _board(monkeypatch, [_sess(A, misplaced_open=1), _sess(B)], detail={A: {**_sess(A), "misplaced": [row]}})
    interrupted: list[str] = []
    monkeypatch.setattr(tui.tmux, "interrupt", lambda pane: interrupted.append(pane))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        await pilot.press("t"); await pilot.pause(0.5)
        c = _confirm(app)
        assert c is not None and "move this prompt to auth-service@main work?" in c.prompt
        _key_press(c, "y"); await pilot.pause(1)
        assert interrupted == []                                      # the agent never had it: nothing to stop
        assert app.client.sent == [(B, row["prompt"])]                # the prompt itself, to the target
        assert app.client.states == [(7, "moved")]


async def test_t_on_a_prompt_the_agent_may_have_started_on_interrupts_it_first(monkeypatch):
    row = _row(id=9, source="llm")
    app = _board(monkeypatch, [_sess(A, misplaced_open=1, pid=4242), _sess(B)], detail={A: {**_sess(A), "misplaced": [row]}})
    log: list[str] = []
    monkeypatch.setattr(tui.tmux, "in_tmux", lambda: True)
    monkeypatch.setattr(tui.tmux, "interrupt", lambda pane: log.append(f"esc {pane}"))
    monkeypatch.setattr(tui.tmux, "agent_pane",
                        lambda key, ps=None: SimpleNamespace(id="%7", key=key) if key == A else None)
    _wrap_send(app, log)
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        await pilot.press("t"); await pilot.pause(0.5)
        _key_press(_confirm(app), "y"); await pilot.pause(1.2)
        assert log[0] == "esc %7"                                     # Escape into its pane, before anything else
        assert log[1] == ("send", A) and log[1] and log[2] == ("send", B)
        assert app.client.states == [(9, "moved")]
        disregard = [b for k, b in app.client.sent if k == A]
        assert len(disregard) == 1 and disregard[0].startswith('Disregard my last prompt ("rename the retry helper')
        assert disregard[0].endswith("it was meant for another session. Stop any work on it and don't undo anything unless I ask.")
        assert app.client.sent[-1] == (B, row["prompt"])


async def test_t_outside_tmux_sends_the_same_thing_and_says_the_interrupt_was_skipped(monkeypatch):
    row = _row(id=9, source="llm")
    app = _board(monkeypatch, [_sess(A, misplaced_open=1), _sess(B)], detail={A: {**_sess(A), "misplaced": [row]}})
    log: list[str] = []
    monkeypatch.setattr(tui.tmux, "interrupt", lambda pane: log.append(f"esc {pane}"))
    _wrap_send(app, log)
    seen: list[str] = []
    monkeypatch.setattr(app, "notify", lambda msg, **k: seen.append(msg))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        await pilot.press("t"); await pilot.pause(0.5)
        _key_press(_confirm(app), "y"); await pilot.pause(1.2)
        assert "esc" not in log                                   # no pane here to send Escape to
        assert [k for k, _ in app.client.sent] == [A, B]
        assert app.client.states == [(9, "moved")]
        assert any("interrupt skipped" in m and "moved to" in m for m in seen), seen


async def test_t_then_any_key_but_y_sends_and_records_nothing(monkeypatch):
    row = _row(id=7)
    app = _board(monkeypatch, [_sess(A, misplaced_open=1), _sess(B)], detail={A: {**_sess(A), "misplaced": [row]}})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        await pilot.press("t"); await pilot.pause(0.5)
        _key_press(_confirm(app), "n"); await pilot.pause(0.6)
        assert app.client.sent == [] and app.client.states == []


async def test_t_on_a_target_that_has_left_the_board_starts_it_somewhere_new(monkeypatch):
    """verdict "other" but that session is gone: it has no "there" to move to, so it takes the drift path."""
    row = _row(id=7, target="devbox/claude/gone", target_title="old auth work")
    app = _board(monkeypatch, [_sess(A, misplaced_open=1)], detail={A: {**_sess(A), "misplaced": [row]}})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        await pilot.press("t"); await pilot.pause(0.5)
        assert _confirm(app) is None
        assert app.input_mode.startswith("newm:") and app.query_one("#filter").value == "/r/webapp"


# -- t on a drift: a session of its own ---------------------------------------------------------------------------
async def test_t_on_a_drift_asks_for_a_directory_and_starts_it_with_the_prompt(monkeypatch):
    row = _row(id=11, verdict="drift", target="", target_title="", source="pre",
               prompt="also add a png to the release notes")
    app = _board(monkeypatch, [_sess(A, misplaced_open=1)], detail={A: {**_sess(A), "misplaced": [row]}})
    started: list[tuple] = []
    monkeypatch.setattr(tui.CtxApp, "start_agent",
                        lambda self, harness, cwd, profile=None, first="": started.append((harness, cwd, profile, first)))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        await pilot.press("t"); await pilot.pause(0.5)
        f = app.query_one("#filter")
        assert app.input_mode == "newm:claude:" and f.visible and f.value == "/r/webapp"
        tui.CtxApp.on_input_submitted(app, type("E", (), {"value": "/r/notes"})())
        await pilot.pause(1.2)
        assert started == [("claude", "/r/notes", None, "also add a png to the release notes")]
        assert app.client.states == [(11, "moved")] and app.client.sent == []


async def test_t_on_a_drift_the_agent_already_has_stops_that_session_first(monkeypatch):
    row = _row(id=12, verdict="drift", target="", target_title="", source="llm",
               prompt="also add a png to the release notes")
    app = _board(monkeypatch, [_sess(A, misplaced_open=1)], detail={A: {**_sess(A), "misplaced": [row]}})
    log: list[str] = []
    monkeypatch.setattr(tui.tmux, "in_tmux", lambda: True)
    monkeypatch.setattr(tui.tmux, "interrupt", lambda pane: log.append("esc"))
    monkeypatch.setattr(tui.tmux, "agent_pane", lambda key, ps=None: SimpleNamespace(id="%3", key=key))
    started: list[tuple] = []
    monkeypatch.setattr(tui.CtxApp, "start_agent",
                        lambda self, harness, cwd, profile=None, first="": started.append((harness, cwd, profile, first)))
    _wrap_send(app, log)
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        await pilot.press("t"); await pilot.pause(0.5)
        tui.CtxApp.on_input_submitted(app, type("E", (), {"value": "/r/notes"})())
        await pilot.pause(1.2)
        assert log[0] == "esc" and _sends(log) == [(A,)]          # it was stopped, then told to drop the prompt
        assert started and started[0][3] == row["prompt"] and app.client.states == [(12, "moved")]


async def test_esc_out_of_the_drift_directory_starts_nothing(monkeypatch):
    row = _row(id=13, verdict="drift", target="", target_title="", source="pre")
    app = _board(monkeypatch, [_sess(A, misplaced_open=1)], detail={A: {**_sess(A), "misplaced": [row]}})
    started: list[tuple] = []
    monkeypatch.setattr(tui.CtxApp, "start_agent",
                        lambda self, harness, cwd, profile=None, first="": started.append(harness))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        await pilot.press("t"); await pilot.pause(0.5)
        await pilot.press("escape"); await pilot.pause(0.5)
        tui.CtxApp.on_input_submitted(app, type("E", (), {"value": "/r/notes"})())
        await pilot.pause(0.6)
        assert started == [] and app.client.states == []


# -- help, legend, pop-ups ----------------------------------------------------------------------------------------
def test_t_and_o_are_bound_and_documented():
    bound = {k.strip(): b.action for b in tui.CtxApp.BINDINGS for k in b.key.split(",")}
    assert bound["t"] == "misplace_move" and bound["o"] == "misplace_keep"
    keys = {keys for keys, _, _ in tui.HELP_KEYS}
    assert ("t",) in keys and ("o",) in keys
    assert any("↯" in words and "move" in words for _, shown, words in tui.HELP_KEYS if shown == "t")
    assert any("keep" in words for _, shown, words in tui.HELP_KEYS if shown == "o")
    assert any("Wrong window?" == title for title, _ in tui.HELP_TIPS)


def test_the_legend_explains_the_mark():
    rows = [g for _, rows in tui.legend_sections() for g in rows if g[0] == "↯"]
    assert any(style == "bold yellow" and "t moves it" in words and "o keeps it" in words for _, style, words in rows)
    assert any(ln.strip().startswith("↯") for ln in tui.legend().plain.splitlines())


def test_an_attention_item_of_that_kind_reads_as_a_marked_session(monkeypatch):
    item = {"key": A, "kind": "misplaced", "ref": "misplaced:7", "text": "looks like it's for auth-service@main work",
            "asked_at": _ago(3), "host_id": "gpu-host", "harness": "claude", "title": "webapp@main work",
            "live_status": "idle"}
    assert tui.popup_line(item) == "ctx · ↯ webapp@main work: looks like it's for auth-service@main work"
    assert tui.popup_status([item, {"key": B, "item": "waiting", "title": "t", "host_id": "devbox"}]).endswith("(+1 more)")
    line = tui.popup_line({"key": B, "item": "waiting", "title": "t", "host_id": "devbox", "status_since": _ago(1)})
    assert line.startswith("ctx · devbox · t:") and "↯" not in line


def test_the_box_lines_for_one_carry_the_mark_and_its_session():
    item = {"key": A, "kind": "misplaced", "ref": "misplaced:7", "text": "off this session's purpose",
            "asked_at": _ago(3), "host_id": "gpu-host", "title": "webapp@main work", "live_status": "idle"}
    from ctxswitch.client import notify
    line = notify.box_lines(tui.popup_items([item]))[0]
    assert "↯ off this session's purpose" in line and "webapp@main work" in line


def test_the_client_puts_a_state_on_the_hub_s_route():
    got: list[tuple] = []

    class R:
        status_code = 200
        json = staticmethod(lambda: {"ok": True})

    c = HubClient(Config())
    c._c = SimpleNamespace(post=lambda path, json=None: (got.append((path, json)), R())[1])
    assert c.misplaced_state(7, "moved") is True
    assert got == [("/v1/misplaced/7/state", {"state": "moved"})]
    c._c = SimpleNamespace(post=lambda path, json=None: SimpleNamespace(status_code=404, json=lambda: {}))
    try:
        c.misplaced_state(7, "kept")
        raise AssertionError("a hub without the route must raise, like spend() does")
    except ValueError as e:
        assert "HTTP 404" in str(e)


def test_an_old_hub_with_none_of_it_leaves_the_board_alone(monkeypatch):
    plain = _sess(A)
    plain.pop("misplaced")
    assert tui.misplaced_mark(plain) == "" and fmt.misopens(plain) == []
    assert fmt.misopens({"misplaced": [_row(state="moved")]}) == []


def _sends(log) -> list[tuple]:
    """Just the messages, in the order they went out, as the log records them."""
    return [x[1:] for x in log if isinstance(x, tuple) and x[0] == "send"]


def _wrap_send(app, log):
    """Record the order the board's client calls land in (a message sent, a state recorded)."""
    real = app.client.send

    def send(key, body, from_host=None, answers=None, from_key=None):
        log.append(("send", key))
        return real(key, body, from_host, answers, from_key)

    app.client.send = send
    real_state = app.client.misplaced_state

    def state(id, st):
        log.append(("state", id, st))
        return real_state(id, st)

    app.client.misplaced_state = state


def app_tmux_panes(module):
    return module.tmux


# -- the wrong-window pop-up (2026-11-12: the operator wants a misplaced prompt to grab their attention) -------------
import subprocess as _sp
from ctxswitch.client import notify as N


def _mis_item(key=A, rid=7, title="webapp@main work"):
    return {"key": key, "kind": "misplaced", "ref": f"misplaced:{rid}", "text": "looks like it's for auth-service@main work",
            "asked_at": _ago(1), "host_id": key.split("/")[0], "harness": "claude", "title": title, "live_status": "busy"}


def test_a_misplaced_prompt_pops_up_on_the_session_you_are_looking_at():
    """Everything else on screen is skipped (you can see it); a wrong-window prompt is always on screen."""
    n = N.Notifier()
    ask = {"key": A, "item": "ask", "ref": "1:plain", "text": "which one?", "asked_at": _ago(1)}
    mis = {**_mis_item(), "item": "ask"}
    assert n.fresh([ask, mis], skip={A}) == [mis]


def test_the_wrong_window_box_says_what_went_where_and_offers_t_and_o():
    other = N.misplaced_box_lines("webapp@main work", _row())
    assert other[0] == "This prompt went into: webapp@main work"
    assert '"rename the retry helper in the auth service"' in other[1]
    assert "It looks like it's for: auth-service@main work" in other and "The agent hasn't seen it (it was blocked)." in other
    assert other[-1].startswith("t move it there · o keep it here")
    drift = N.misplaced_box_lines("notes", _row(verdict="drift", target="", target_title="", source="llm"))
    assert "It looks like it's off this session's purpose." in drift and "The agent may already be working on it." in drift
    assert drift[-1].startswith("t start a new session with it")
    assert all(len(x) <= N.BOX_W - 4 for x in other + drift)


def test_the_wrong_window_box_s_keys_are_t_o_and_anything_else():
    for key, rc in (("t", N.MIS_RC_MOVE), ("T", N.MIS_RC_MOVE), ("o", N.MIS_RC_KEEP), ("x", 3), ("\n", 3)):
        p = _sp.run(["bash", "-c", N.MIS_BOX_CMD.removeprefix("exec bash -c ").strip("'")], input=key, text=True,
                    capture_output=True, env={"CTX_POPUP": "hi", "PATH": "/usr/bin:/bin"})
        assert p.returncode == rc, (key, p.returncode, p.stderr)
    seen = []
    N.tmux_misplaced_box("webapp@main work", _row(), popen=lambda argv, **kw: seen.append(argv) or object())
    argv = seen[0]
    assert argv[:2] == ["tmux", "display-popup"] and " ctx: wrong window? " in argv and N.MIS_BOX_STYLE in argv


class _Box:
    def __init__(self, rc): self.rc = rc
    def wait(self): return self.rc
    def poll(self): return self.rc


async def _popped(monkeypatch, rc, row):
    app = _board(monkeypatch, [_sess(A, misplaced_open=1), _sess(B, title="auth-service@main work")],
                 detail={A: {**_sess(A), "misplaced": [row]}})
    monkeypatch.setattr(tui.tmux, "in_tmux", lambda: True)
    opened = []
    monkeypatch.setattr(tui, "tmux_misplaced_box", lambda title, r: opened.append((title, r["id"])) or _Box(rc))
    moved = []
    monkeypatch.setattr(tui.CtxApp, "move_prompt", lambda self, s, r, target: moved.append((s["key"], r["id"], target)))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        app.attention = [_mis_item(rid=row["id"])]
        app.on_screen = {A}                                       # the session it went into is the one on screen
        app.popup_now(); await pilot.pause(1)
        app.filter_value = app.query_one("#filter").value          # read before the app closes
        return app, opened, moved


async def test_t_in_the_box_moves_the_prompt_with_no_second_question(monkeypatch):
    app, opened, moved = await _popped(monkeypatch, N.MIS_RC_MOVE, _row())
    assert opened == [("webapp@main work", 7)] and moved == [(A, 7, B)]


async def test_o_in_the_box_keeps_it_and_any_other_key_leaves_it(monkeypatch):
    app, _, moved = await _popped(monkeypatch, N.MIS_RC_KEEP, _row())
    assert app.client.states == [(7, "kept")] and moved == []
    app, _, moved = await _popped(monkeypatch, 3, _row())
    assert app.client.states == [] and moved == []


async def test_t_in_the_box_on_a_drift_asks_for_the_new_session_s_directory(monkeypatch):
    app, _, moved = await _popped(monkeypatch, N.MIS_RC_MOVE, _row(verdict="drift", target="", target_title="", source="llm"))
    assert moved == [] and app.input_mode.startswith("newm:") and app.filter_value == "/r/webapp"


def test_the_status_line_for_a_misplaced_prompt_has_one_glyph():
    line = tui.popup_line(tui.popup_items([_mis_item()])[0])
    assert line.count(fmt.MISPLACE_GLYPH) == 1, line


async def test_the_box_comes_with_the_eep_unless_it_s_turned_off(monkeypatch):
    played = []
    monkeypatch.setattr(tui.sound, "play", lambda name: played.append(name))
    await _popped(monkeypatch, 3, _row())
    assert played == ["eep"]
    played.clear()
    monkeypatch.setattr(tui.Config, "misplace_sound", "", raising=False)
    app = _board(monkeypatch, [_sess(A, misplaced_open=1)], detail={A: {**_sess(A), "misplaced": [_row()]}})
    app.cfg.misplace_sound = ""
    monkeypatch.setattr(tui.tmux, "in_tmux", lambda: True)
    monkeypatch.setattr(tui, "tmux_misplaced_box", lambda title, r: _Box(3))
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        app.attention = [_mis_item()]; app.on_screen = {A}
        app.popup_now(); await pilot.pause(1)
    assert played == [""]                                          # sound.play("") is the silent path
