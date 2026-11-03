"""The local-pool switch on the board: a host's collector pushes its ``~/.claude/saint-pool`` state, each Claude
session carries what it was last told (its meta ``pool`` / ``pool_at`` / ``pool_how``), and the board shows a ⌂ on the
row — yellow, with its account, when the session's belief and the host's switch no longer agree — with the host's switch in the header and
both sides in the detail pane.

The collector side lives on another branch, so every test here hands the hub a hand-made ``pool`` event and the client
a hand-made session dict: nothing touches a real Claude transcript or a real switch file."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from starlette.testclient import TestClient

from ctxswitch.client import fmt
from ctxswitch.client.tui import app as tui
from ctxswitch.common.config import Config
from ctxswitch.hub.api import Hub
from ctxswitch.hub.store import Store

H = {"Authorization": "Bearer t"}
P1, C1 = "laptop/claude/p1", "laptop/claude/c1"
NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)


def _ago(**d) -> str:
    """An ISO instant ``now`` minus this much — so a line's age doesn't drift with the clock it's read on."""
    return (datetime.now(timezone.utc) - timedelta(**d)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sess(key, *, host="laptop", harness="claude", pool="", pool_at="", pool_how="", parent="", model="", **kw):
    s = {"key": key, "host_id": host, "harness": harness, "session_id": key.rsplit("/", 1)[-1],
         "title": "webapp@main work", "live_status": "idle", "status_since": "2026-10-30T09:00:00Z",
         "last_activity": "2026-10-30T09:00:00Z", "started_at": "2026-10-30T09:00:00Z", "repo_root": "/r/webapp",
         "git_branch": "main", "turn_count": 3, "open_asks": [], "turns": [], "parent_key": parent,
         "job": "", "job_status": "", "job_brief": "",
         "meta": {"pool": pool, "pool_at": pool_at, "pool_how": pool_how, "model": model}}
    return {**s, **kw}


def _job(key, parent, **kw):
    return _sess(key, parent=parent, title=f"pool job {key}", job=f"sa-{key.rsplit('/', 1)[-1]}",
                 job_status="ok", job_brief="job1-runtime.md", **kw)


def _pool(host="laptop", state="on", **kw):
    return {**{"host_id": host, "state": state, "url": "http://saint:8787", "checked_at": _ago(minutes=2)}, **kw}


class FakeClient:
    """The hub as the contract describes it: the rows it hands over, and one host row per host with its switch (null
    when that host never reported one). No spend: the header's second line stays "today —"."""

    def __init__(self, rows, pools=None):
        self.rows, self.pools = rows, pools or {}

    def sessions(self, host=None, live_only=False):
        return {"sessions": [dict(x) for x in self.rows], "change": 1}

    def session(self, key, turns=50):
        return next((dict(x) for x in self.rows if x["key"] == key), None)

    def attention(self, host=None):
        return {"items": []}

    def hosts(self):
        return [{"host_id": h, "last_seen": 1.0, "pool": self.pools.get(h)}
                for h in sorted({s["host_id"] for s in self.rows} | set(self.pools))]

    def spend(self, frm, to, host=None):
        return None

    def viewed(self, key, at):
        pass

    def ws_url(self):
        return "ws://x/v1"


def _board(monkeypatch, rows, pools=None) -> tui.CtxApp:
    cfg = Config(); cfg.hub.token = "t"; cfg.collector.host_id = "laptop"
    monkeypatch.setattr(tui.tmux, "in_tmux", lambda: False)
    monkeypatch.setattr(tui.CtxApp, "listen_ws", lambda self: None)
    monkeypatch.setattr(tui.CtxApp, "join_tmux", lambda self: None)
    app = tui.CtxApp(cfg, None)
    app.client = FakeClient(rows, pools)
    return app


def _styles(app, key: str) -> tuple[str, str]:
    """(the account label's style, the ⌂'s style) on a session's row."""
    t = app.query_one("#table")
    cell = next(t.get_row_at(i)[3] for i in range(t.row_count) if t.coordinate_to_cell_key((i, 0)).row_key.value == key)
    def at(i):
        return " ".join(" ".join(sorted(str(sp.style).split())) for sp in cell.spans if sp.start <= i < sp.end) if i >= 0 else ""
    return at(cell.plain.find("cc·laptop")), at(cell.plain.find("⌂"))


def _labels(app) -> dict[str, str]:
    t = app.query_one("#table")
    return {t.coordinate_to_cell_key((i, 0)).row_key.value: t.get_row_at(i)[3].plain
            for i in range(t.row_count) if t.coordinate_to_cell_key((i, 0)).row_key.value.startswith("laptop")
            or t.coordinate_to_cell_key((i, 0)).row_key.value.startswith("devbox")}


# -- the hub's side ------------------------------------------------------------------------------------------------

def _hub(tmp_path):
    store = Store(tmp_path / "hub.db")
    return store, TestClient(Hub(store, token="t").app())


def _push(c, d, host="laptop", seq=1):
    return c.post("/v1/ingest", json={"host_id": host, "events": [{"seq": seq, "kind": "pool", "payload": d}]}, headers=H)


def test_a_pushed_pool_state_rides_the_host_rows(tmp_path):
    store, c = _hub(tmp_path)
    c.post("/v1/ingest", json={"host_id": "laptop", "events": []}, headers=H)      # the host itself joins the list
    assert c.get("/v1/hosts", headers=H).json()[0]["pool"] is None                 # it hasn't said anything yet
    assert _push(c, _pool()).status_code == 200
    row = c.get("/v1/hosts", headers=H).json()[0]
    assert (row["pool"]["state"], row["pool"]["url"]) == ("on", "http://saint:8787")
    assert fmt.parse_iso(row["pool"]["checked_at"]) is not None
    assert store.host_pools()["laptop"]["state"] == "on"


def test_the_latest_push_of_a_host_is_the_only_one_the_hub_keeps(tmp_path):
    store, c = _hub(tmp_path)
    _push(c, _pool(state="on"))
    _push(c, _pool(state="off", url=""), seq=2)                                    # the switch moved: it replaces
    assert [p["state"] for p in store.host_pools().values()] == ["off"]
    assert c.get("/v1/hosts", headers=H).json()[0]["pool"]["state"] == "off"


def test_a_host_that_never_reported_its_switch_says_so_with_null(tmp_path):
    store, c = _hub(tmp_path)
    _push(c, _pool(), host="laptop")
    _push(c, _pool(host="devbox"), host="devbox")
    c.post("/v1/ingest", json={"host_id": "server7", "events": []}, headers=H)   # a host that has never reported
    rows = {h["host_id"]: h for h in c.get("/v1/hosts", headers=H).json()}
    assert rows["devbox"]["pool"]["state"] == "on" and rows["server7"]["pool"] is None
    assert sorted(store.host_pools()) == ["devbox", "laptop"]


def test_a_pool_event_with_no_host_in_its_payload_still_lands_on_the_host_that_sent_it(tmp_path):
    store, c = _hub(tmp_path)
    _push(c, {"state": "unreachable", "url": "", "checked_at": _ago()}, host="devbox")
    assert store.host_pools()["devbox"]["state"] == "unreachable"


def test_ingest_and_hosts_both_refuse_a_caller_without_the_token(tmp_path):
    store, c = _hub(tmp_path)
    assert c.post("/v1/ingest", json={"host_id": "laptop",
                                      "events": [{"seq": 1, "kind": "pool", "payload": _pool()}]}).status_code == 401
    assert c.get("/v1/hosts").status_code == 401
    assert store.host_pools() == {}


# -- fmt's words ---------------------------------------------------------------------------------------------------

def test_the_badge_says_what_the_session_was_told_and_shouts_when_that_stale_is_wrong():
    on, off, gone, quiet = {"pool": "on"}, {"pool": "off"}, {"pool": "unreachable"}, {"pool": ""}
    host_on, host_off, host_gone = ({"laptop": {"state": "on"}}, {"laptop": {"state": "off"}},
                                    {"laptop": {"state": "unreachable"}})
    assert fmt.pool_mark({"harness": "claude", "host_id": "laptop", "meta": quiet}, host_on) == ("", "")  # told nothing
    assert fmt.pool_mark({"harness": "claude", "host_id": "laptop", "meta": quiet}, {}) == ("", "")
    assert fmt.pool_mark({"harness": "pi", "host_id": "laptop", "meta": on}, host_on) == ("", "")
    assert fmt.pool_mark({"harness": "claude", "host_id": "laptop", "meta": on}, host_on) == ("⌂", "bold green")
    assert fmt.pool_mark({"harness": "claude", "host_id": "laptop", "meta": gone}, host_on) == ("⌂", "dim green")
    assert fmt.pool_mark({"harness": "claude", "host_id": "laptop", "meta": on}, {}) == ("⌂", "bold green")
    assert fmt.pool_mark({"harness": "claude", "host_id": "laptop", "meta": on}, host_off) == ("⌂", "bold yellow")
    assert fmt.pool_mark({"harness": "claude", "host_id": "laptop", "meta": off}, host_on) == ("⌂", "bold yellow")
    assert fmt.pool_mark({"harness": "claude", "host_id": "laptop", "meta": off}, host_gone) == ("⌂", "bold yellow")
    assert fmt.pool_mark({"harness": "claude", "host_id": "laptop", "meta": off}, host_off) == ("⌂", "grey50")
    assert fmt.pool_mark({"harness": "claude", "host_id": "laptop", "meta": quiet}, host_off) == ("", "")
    assert fmt.pool_mark({"harness": "claude", "host_id": "laptop", "meta": off}, {}) == ("⌂", "grey50")   # host silent
    assert fmt.pool_mark({"harness": "claude", "host_id": "laptop", "meta": on},
                         {"other": {"state": "off"}}) == ("⌂", "bold green")   # another host's switch says nothing
    assert fmt.pool_mark({"harness": "claude", "host_id": "laptop", "meta": {}}, None) == ("", "")   # an old hub


def test_a_session_nobody_told_and_a_host_that_never_said_are_never_stale():
    told, quiet = {"pool": "on"}, {"pool": ""}
    assert fmt.pool_stale({"harness": "claude", "host_id": "laptop", "meta": told}, {"laptop": {"state": "off"}})
    assert not fmt.pool_stale({"harness": "claude", "host_id": "laptop", "meta": quiet}, {"laptop": {"state": "off"}})
    assert not fmt.pool_stale({"harness": "claude", "host_id": "laptop", "meta": told}, {})           # the host is silent
    assert not fmt.pool_stale({"harness": "claude", "host_id": "laptop", "meta": told}, {"laptop": None})
    assert not fmt.pool_stale({"harness": "claude", "host_id": "laptop", "meta": told}, {"laptop": {"state": "on"}})
    assert not fmt.pool_stale({"harness": "claude", "host_id": "laptop", "meta": quiet}, {})


def test_the_detail_line_says_both_sides_and_how_the_session_learned_them():
    s = _sess(P1, pool="on", pool_at=(NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"), pool_how="resume")
    assert fmt.pool_line(s, {"laptop": {"state": "on"}}, NOW) == "pool: on (told at resume, 2h00 ago) · host switch: on"
    for how in ("startup", "clear", "compact"):
        assert f"(told at {how}, 2h00 ago)" in fmt.pool_line({**s, "meta": {**s["meta"], "pool_how": how}},
                                                             {"laptop": {"state": "on"}}, NOW)
    cmd = _sess(P1, pool="off", pool_at=(NOW - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"), pool_how="command")
    assert fmt.pool_line(cmd, {"laptop": {"state": "on"}}, NOW) == ("pool: off (told by a command, 5m ago) · host "
                                                                    f"switch: on — {fmt.POOL_STALE}")
    assert fmt.pool_line(_sess(P1), {}, NOW) == ""                                  # neither side has anything to say
    assert fmt.pool_line(_sess(P1, pool="on", pool_at=_ago(hours=1), pool_how="startup"), {}, NOW) \
        .endswith("· host switch: —")                                               # the host never reported
    assert fmt.pool_line(_sess(P1, pool="on", pool_how="resume"), {"laptop": {"state": "on"}}, NOW) == "pool: on · host switch: on"


def test_the_header_tags_the_host_it_is_about_and_only_when_that_host_said():
    pools = {"laptop": {"state": "on"}, "devbox": {"state": "off"}}
    assert fmt.header_pool(pools, "laptop") == " · pool on"
    assert fmt.header_pool(pools, "devbox") == " · pool off"
    assert fmt.header_pool({"laptop": {"state": "unreachable"}}, "laptop") == " · pool unreachable"
    assert fmt.header_pool(pools, "server7") == "" and fmt.header_pool(pools, None) == ""
    assert fmt.header_pool({}, "laptop") == "" and fmt.header_pool(None, "laptop") == ""
    rows = [_sess(P1, pool="on")]
    assert tui.summary_line(rows, "laptop", pools, "laptop").plain == "ctx · laptop · 1 live · nothing waiting · pool on"
    assert tui.summary_line(rows, None, pools, "laptop").plain == "ctx · all · 1 live · nothing waiting · pool on"
    assert tui.summary_line([], "devbox", pools, "devbox").plain == "ctx · devbox · 0 live · nothing waiting · pool off"
    assert tui.summary_line(rows, "laptop", {}).plain == "ctx · laptop · 1 live · nothing waiting"   # an old hub
    assert tui.summary_line(rows, "laptop").plain == "ctx · laptop · 1 live · nothing waiting"


def test_the_legend_and_the_help_both_say_what_a_gear_means():
    row = [g for _, rows in tui.legend_sections() for g in rows if g[0] == "⌂"]
    assert [g[0] for g in row] == ["⌂", "⌂", "⌂"]
    assert [g[1] for g in row] == ["bold green", "grey50", "bold yellow"]
    assert "local pool on" in row[0][2] and "local pool off" in row[1][2] and "/local-pool" in row[2][2]
    tip = next(w for t, w in tui.HELP_TIPS if t == "Local pool")
    assert "⌂" in tip and "yellow" in tip and "/local-pool" in tip and "transcript" in tip


# -- the board -----------------------------------------------------------------------------------------------------

async def test_a_row_wears_its_pool_badge_after_the_host_and_flags_a_stale_one(monkeypatch):
    on = _sess(P1, pool="on", pool_at=_ago(hours=2), pool_how="resume")
    app = _board(monkeypatch, [on], pools={"laptop": _pool(state="on")})
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(1)
        assert app.host_pools == {"laptop": app.client.pools["laptop"]}
        assert "cc·laptop ⌂" in _labels(app)[P1]                       # right after the harness·host label
        assert _styles(app, P1) == ("dim", "bold green")                  # agreeing: the account stays dim
        head = app.query_one("#summary").render().plain.splitlines()[0]
        assert head.endswith(" · pool on")                                  # this host's switch, in the header

    app = _board(monkeypatch, [on], pools={"laptop": _pool(state="off")})   # the switch moved under it
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(1)
        assert "cc·laptop ⌂" in _labels(app)[P1]
        assert _styles(app, P1) == ("yellow", "bold yellow")                # stale: the account and its ⌂ go yellow
        assert "⌂" not in app.query_one("#detail_text").render().plain      # the detail says it in words, not a glyph
        assert fmt.POOL_STALE in app.query_one("#detail_text").render().plain

    app = _board(monkeypatch, [_sess(P1, pool="off", pool_at=_ago(minutes=5), pool_how="command")],
                 pools={"laptop": _pool(state="on")})                        # it thinks off, the file says on
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(1)
        assert "cc·laptop ⌂" in _labels(app)[P1]
        assert _styles(app, P1) == ("yellow", "bold yellow")

    app = _board(monkeypatch, [_sess(P1, pool="unreachable")], pools={"laptop": _pool(state="unreachable")})
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(1)
        assert "cc·laptop ⌂" in _labels(app)[P1]
        assert _styles(app, P1) == ("dim", "dim green")

    app = _board(monkeypatch, [_sess(P1, pool="off", pool_how="command")], pools={"laptop": _pool(state="off")})
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(1)
        assert "cc·laptop ⌂" in _labels(app)[P1] and _styles(app, P1) == ("dim", "grey50")   # off, agreeing: grey

    app = _board(monkeypatch, [_sess(P1), _sess("devbox/pi/p2", host="devbox", harness="pi", pool="on")])
    async with app.run_test(size=(100, 30)) as pilot:                        # told nothing, or not a Claude: nothing
        await pilot.pause(1)
        assert all("⌂" not in label for label in _labels(app).values())


async def test_the_detail_pane_shows_both_sides_under_the_model_line(monkeypatch):
    s = _sess(P1, pool="on", pool_at=_ago(hours=2), pool_how="resume", model="claude-opus-5-5")
    app = _board(monkeypatch, [s], pools={"laptop": _pool(state="on")})
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(1)
        app.render_detail(s)
        lines = app.query_one("#detail_text").render().plain.splitlines()
        i = lines.index("claude-opus-5-5 ")                                # the model / permission-mode line
        assert lines[i + 1] == f"pool: on (told at resume, 2h00 ago) · host switch: on"
        assert str(app.query_one("#detail_text").render().spans[-1].style) == "dim"


async def test_a_stale_detail_line_is_painted_yellow(monkeypatch):
    s = _sess(P1, pool="on", pool_at=_ago(hours=2), pool_how="startup")
    app = _board(monkeypatch, [s], pools={"laptop": _pool(state="off")})
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(1)
        app.render_detail(s)
        t = app.query_one("#detail_text").render()
        at = t.plain.index("pool:")
        assert t.plain.splitlines()[t.plain[:at].count("\n")].endswith(fmt.POOL_STALE)
        assert any({"bold"} <= set(str(sp.style).split()) and "yellow" in str(sp.style) for sp in t.spans if sp.start <= at < sp.end), \
        [(sp.start, sp.end, str(sp.style)) for sp in t.spans if sp.start <= at < sp.end]


async def test_a_pool_job_row_wears_no_badge_of_its_own(monkeypatch):
    """The worker inherits the switch of the host it ran on; its parent's row is where that shows."""
    parent = _sess(P1, pool="on")
    kid = _job(C1, P1, meta={"pool": "off"})
    app = _board(monkeypatch, [parent, kid], pools={"laptop": _pool(state="on")})
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(1)
        labels = _labels(app)
        assert "cc·laptop ⌂" in labels[P1]
        assert "⌂" not in labels[C1] and labels[C1].startswith("  ↳ ")


async def test_a_hub_that_sends_no_pool_state_boards_exactly_as_it_did(monkeypatch):
    """An older hub: /v1/hosts rows have no "pool", a session's meta has no "pool", so nothing new appears."""
    app = _board(monkeypatch, [_sess(P1)])
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(1)
        assert app.host_pools == {}
        assert "pool" not in app.query_one("#summary").render().plain
        assert "⌂" not in "".join(_labels(app).values())
        app.render_detail(_sess(P1))
        assert "pool" not in app.query_one("#detail_text").render().plain
