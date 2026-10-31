"""Spend: pricing a session's turns, SAINT's local energy, /v1/spend, and the pool-job parent links behind the jobs."""
import json

import httpx
import pytest
from starlette.testclient import TestClient

from ctxswitch.hub import spend as S
from ctxswitch.hub.api import Hub
from ctxswitch.hub.store import Store
from ctxswitch.common.config import Config, SeatConfig, load

H = {"Authorization": "Bearer t"}
DAY, NEXT = "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"
IN_DAY = "2026-01-02T10:00:00Z"
P, CHILD, PI, SH, OLD = "laptop/claude/p", "laptop/claude/j1", "laptop/pi/x1", "laptop/shell/s1", "laptop/claude/old"


def _sess(store, key, *, profile="", when=IN_DAY, started=None, live=False, **meta):
    h, harness, sid = key.split("/")
    m = {"key": key, "host_id": h, "harness": harness, "session_id": sid, "title": sid, "cwd": "/r", "repo_root": "/r",
         "repo_remote": "git@github.com:me/proj.git", "git_branch": "main", "started_at": started or when,
         "last_activity": when, **({"profile": profile} if profile else {}), **meta}
    store.upsert_session(m)
    if live:
        store.set_liveness(h, [{"key": key, "status": "idle", "status_since": when, "pid": 1, "socket": "/x"}])


def _turn(idx, t=IN_DAY, model="claude-opus-5-5", usage=None, cost=None):
    return {"idx": idx, "started_at": t, "ended_at": t, "user_text": "go", "user_origin": "human", "assistant_text": "ok",
            "tally": {}, "asks": [], "events": [], "complete": True, "offset_end": idx * 100 + 10,
            "usage": usage, "model": model, "cost_usd": cost}


def _u(i=0, o=0, cr=0, cc=0):
    return {"input": i, "output": o, "cache_read": cr, "cache_creation": cc}


def _hub(store, saint=None, prices=None):
    return TestClient(Hub(store, token="t", saint=saint, prices=prices or {}).app(), headers=H)


def _saint(totals=None, jobs=None):
    """A SaintUsage whose window() answers with SAINT's shape (the URL only has to look real)."""
    s = S.SaintUsage("http://saint.local:4000/usage/v1")
    s.window = lambda f, t: {"totals": totals or {}, "jobs": jobs or []}       # tests never reach the network
    return s


# -- pricing -----------------------------------------------------------------------------------------------------
def test_norm_model_canonicalizes_what_a_transcript_names():
    assert S.norm_model("us.anthropic.claude-opus-5-5") == "claude-opus-5-5"
    assert S.norm_model("anthropic.claude-haiku-4-5-20251001-v1:0") == "claude-haiku-4-5-20251001"
    assert S.norm_model("saint/saint-auto") == "saint/saint-auto"        # a machine, not our cloud: stays put
    assert S.norm_model("bedrock/claude-sonnet-5") == "claude-sonnet-5"


def test_rates_for_matches_the_longest_prefix():
    assert S.rates_for("claude-haiku-4-5-20251001") == S.RATES["claude-haiku-4-5"]
    assert S.rates_for("us.anthropic.claude-opus-5-5")["input"] == 4.4
    assert S.rates_for("qwen3") is None and S.rates_for("") is None
    assert S.rates_for("claude-opus-5-5", {"claude-opus-5-5": {"input": 1.0}}) == {"input": 1.0}


def test_price_is_tokens_per_million_times_the_rate():
    rates = S.rates_for("claude-opus-5-5")
    assert S.price(_u(i=1_000_000, cr=2_000_000), rates) == pytest.approx(4.4 + 0.44)
    assert S.price(_u(), rates) == 0.0
    with pytest.raises(KeyError):                                        # tokens of a type with no rate: unpriced
        S.price(_u(i=10), {"output": 1.0})


def test_session_spend_counts_tokens_whether_or_not_they_priced_out():
    t = lambda i, m, **kw: _turn(i, model=m, usage=_u(i=1_000_000, o=1_000_000), **kw)
    d = S.session_spend([t(0, "claude-opus-5-5"), t(1, "claude-opus-5-5")])
    assert d["usd"] == pytest.approx(2 * 4.4 + 2 * 22.0) and d["tokens"] == 4_000_000   # 2M in, 2M out
    assert d["by_type"] == _u(i=2_000_000, o=2_000_000) and d["unpriced"] is False

    pi = S.session_spend([_turn(0, model="saint/saint-auto", usage=_u(i=500_000), cost=0.5)])
    assert pi["usd"] == pytest.approx(0.5) and pi["tokens"] == 500_000    # pi priced itself; its tokens still count

    free = S.session_spend([t(0, "saint/saint-auto")])
    assert free["usd"] is None and free["unpriced"] and free["tokens"] == 2_000_000
    assert S.session_spend([])["usd"] is None and S.session_spend([])["tokens"] == 0


def test_hub_prices_override_the_table_for_a_model():
    prices = {"my-model": {"input": 1.0, "output": 2.0, "cache_read": 0.0, "cache_creation": 0.0}}
    d = S.session_spend([_turn(0, model="my-model", usage=_u(i=1_000_000, o=1_000_000))], prices=prices)
    assert d["usd"] == pytest.approx(3.0) and d["unpriced"] is False
    assert S.session_spend([_turn(0, model="claude-opus-5-5", usage=_u(i=1_000_000))], prices=prices)["usd"] == pytest.approx(4.4)


# -- SAINT's usage endpoint --------------------------------------------------------------------------------------
def test_saint_usage_fetches_dates_and_caches_them():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"totals": {"energy_cost_usd": 1.0, "tokens": {"input": 10}}})

    s = S.SaintUsage("http://saint.local:4000/usage/v1", transport=httpx.MockTransport(handler))
    assert s.window("2026-01-02T23:30:00Z", "2026-01-03T00:30:00Z")["totals"]["energy_cost_usd"] == 1.0
    assert s.window("2026-01-02T23:30:00Z", "2026-01-03T00:30:00Z") is not None      # from the cache
    assert seen == [{"from": "2026-01-02", "to": "2026-01-03"}]                       # dates, UTC, one fetch
    assert s.session("abc")["totals"]["tokens"] == {"input": 10} and seen[-1]["session"] == "abc"
    assert S.SaintUsage("").window(DAY, NEXT) is None                                 # no endpoint configured


def test_saint_usage_reports_a_failure_as_none_with_a_reason():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    s = S.SaintUsage("http://saint.local:4000/usage/v1", transport=httpx.MockTransport(handler))
    assert s.window(DAY, NEXT) is None and s.last_error.startswith("ConnectError:")


def test_usage_url_defaults_to_the_preferred_seat_box():
    cfg = Config()
    cfg.seats = [SeatConfig(url="http://other.local:4000/v1", priority=1), SeatConfig(url="http://saint.local:8000/v1/", priority=5)]
    assert cfg.hub.usage_endpoint(cfg.seats) == "http://other.local:4000/usage/v1"
    cfg.hub.usage_url = "http://box:1/usage/v1"
    assert cfg.hub.usage_endpoint(cfg.seats) == "http://box:1/usage/v1"
    cfg.hub.usage_url = False                                          # `usage_url = false` in the file: no local
    assert cfg.hub.usage_endpoint(cfg.seats) == ""
    cfg.hub.usage_url, cfg.seats = "", [SeatConfig(url="http://127.0.0.1:4000/v1")]
    cfg.seats[0].priority = 0
    assert cfg.hub.usage_endpoint(cfg.seats) == "http://127.0.0.1:4000/usage/v1"


# -- the store's side --------------------------------------------------------------------------------------------
def test_pool_job_columns_round_trip_and_link(tmp_path):
    store = Store(tmp_path / "hub.db")
    _sess(store, P)
    _sess(store, CHILD, parent_key=P, job="sa-7-j1", job_status="ok", job_brief="brief.md", started="2026-01-02T11:00:00Z")
    _sess(store, "laptop/claude/j2", parent_key=P, job="sa-7-j2", started="2026-01-02T12:00:00Z")
    for k in (P, CHILD, "laptop/claude/j2"):
        store.upsert_turns(k, [_turn(0)], None)
    assert store.children(P) == ["laptop/claude/j2", CHILD]             # newest started_at first
    rows = {r["key"]: r for r in store.sessions()}
    assert (rows[CHILD]["parent_key"], rows[CHILD]["job"], rows[CHILD]["job_status"]) == (P, "sa-7-j1", "ok")
    assert (rows[P]["children"], rows[CHILD]["children"]) == (2, 0)
    one = store.session(P)
    assert one["children_keys"] == ["laptop/claude/j2", CHILD] and one["children"] == 2
    store.upsert_turns(CHILD, [_turn(0, usage=_u(i=1_000_000, o=1_000_000))], None)
    assert store.session(CHILD)["spend"]["by_type"] == _u(i=1_000_000, o=1_000_000)
    assert store.session(CHILD)["spend"]["model"] == "claude-opus-5-5"


def test_an_older_database_gains_the_columns(tmp_path):
    import sqlite3
    db = tmp_path / "hub.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE sessions (key TEXT PRIMARY KEY, host_id TEXT NOT NULL, harness TEXT NOT NULL, "
                      "session_id TEXT NOT NULL, meta TEXT NOT NULL, title TEXT, cwd TEXT, repo_root TEXT, "
                      "repo_remote TEXT, git_branch TEXT, started_at TEXT, last_activity TEXT, "
                      "ended INTEGER NOT NULL DEFAULT 0, live_status TEXT NOT NULL DEFAULT 'unknown', status_since TEXT, "
                      "pid INTEGER, socket TEXT, last_viewed_at TEXT, updated REAL NOT NULL);")
    con.commit(); con.close()
    store = Store(db)
    _sess(store, P, parent_key="laptop/claude/other", job="sa-1")
    store.upsert_turns(P, [_turn(0)], None)
    assert store.children("laptop/claude/other") == [P] and store.sessions()[0]["job"] == "sa-1"


# -- /v1/spend ---------------------------------------------------------------------------------------------------
def _corpus(store):
    _sess(store, P, profile="bedrock")
    store.upsert_turns(P, [_turn(0, usage=_u(i=1_000_000, o=1_000_000)), _turn(1, "2026-01-02T11:00:00Z",
                                                                              model="claude-sonnet-5", usage=_u(i=500_000))], None)
    _sess(store, CHILD, profile="max", parent_key=P, job="sa-9-j1", job_status="ok", job_brief="runtime.md",
          job_tokens=_u(i=500_000, o=250_000, cr=250_000), job_dur_s=72, job_turns=30)
    store.upsert_turns(CHILD, [_turn(0, model="saint/saint-auto", usage=_u(i=200_000))], None)
    _sess(store, PI)
    store.upsert_turns(PI, [_turn(0, model="saint/saint-auto", usage=_u(i=400_000), cost=0.5)], None)
    _sess(store, SH)
    store.upsert_turns(SH, [_turn(0, model="", usage=None)], None)
    _sess(store, OLD, profile="bedrock", when="2026-01-01T10:00:00Z")
    store.upsert_turns(OLD, [_turn(0, "2026-01-01T10:00:00Z", usage=_u(i=9_000_000))], None)


def _window_totals():
    return {"energy_cost_usd": 1.0, "requests": 342, "tokens_by_type": _u(i=5_000_000, o=2_000_000, cr=3_000_000)}


def test_spend_totals_the_window_by_account_and_model(tmp_path):
    store = Store(tmp_path / "hub.db")
    _corpus(store)
    c = _hub(store, saint=_saint(_window_totals()))
    body = c.get("/v1/spend", params={"from": DAY, "to": NEXT}).json()
    assert body["window"] == {"from": DAY, "to": NEXT}
    # opus turn 4.4+22, sonnet turn 1.1, pi's own 0.50; the saint/saint-auto turn and the shell add tokens, no $
    assert body["cloud"]["usd"] == pytest.approx(27.5 + 0.5)
    assert body["cloud"]["tokens"] == _u(i=2_100_000, o=1_000_000)
    assert [a["account"] for a in body["cloud"]["by_account"]] == ["bedrock", "pi", "max"]     # by $, then tokens
    assert body["cloud"]["by_account"][0]["usd"] == pytest.approx(27.5) and body["cloud"]["by_account"][0]["sessions"] == 1
    assert {m["model"]: m["priced"] for m in body["cloud"]["by_model"]}["saint/saint-auto"] is False
    assert body["unpriced_models"] == ["saint/saint-auto"]
    assert body["rates"] == "default"
    assert body["total_usd"] == pytest.approx(28.0 + 1.0)               # cloud + SAINT's energy
    assert [s["key"] for s in body["sessions"]][0] == P and body["sessions"][0]["usd"] == pytest.approx(27.5)
    assert body["sessions"][-1]["tokens"] == 0                          # the shell: nothing recorded


def test_spend_local_energy_and_the_pool_jobs(tmp_path):
    store = Store(tmp_path / "hub.db")
    _corpus(store)
    c = _hub(store, saint=_saint(_window_totals()))
    body = c.get("/v1/spend", params={"from": DAY, "to": NEXT}).json()
    local = body["local"]
    assert (local["energy_usd"], local["tokens"], local["requests"], local["source"]) == (1.0, 10_000_000, 342, "saint")
    assert local["price_as"] == "claude-opus-5-5" and local["error"] is None
    assert local["if_cloud_usd"] == pytest.approx(5 * 4.4 + 2 * 22 + 3 * 0.22)
    (job,) = body["jobs"]
    assert (job["job"], job["status"], job["brief"], job["parent_key"], job["turns"], job["dur_s"]) == \
           ("sa-9-j1", "ok", "runtime.md", P, 30, 72)
    assert job["parent_title"] == "p" and job["tokens"] == 1_000_000
    assert job["local_usd"] == pytest.approx(1.0 * 1_000_000 / 10_000_000)          # the window's $/token
    assert job["if_cloud_usd"] == pytest.approx(0.5 * 4.4 + 0.25 * 22 + 0.25 * 0.22)


def test_spend_filters_by_window_and_host_and_validates_the_timestamps(tmp_path):
    store = Store(tmp_path / "hub.db")
    _corpus(store)
    _sess(store, "desk/claude/d1", when=IN_DAY)
    store.upsert_turns("desk/claude/d1", [_turn(0, usage=_u(i=1_000_000))], None)
    c = _hub(store)
    url = lambda **p: c.get("/v1/spend", params=p)   # "from" is a keyword: pass it in a dict
    assert c.get("/v1/spend").status_code == 200                                  # defaults: the last day
    assert url(**{"from": "not-a-timestamp"}).status_code == 400 and url(to="nope").status_code == 400
    assert url(**{"from": DAY, "to": DAY}).status_code == 400                                # from must be before to
    assert TestClient(Hub(store, token="t").app()).get("/v1/spend").status_code == 401   # auth required
    day = url(**{"from": DAY, "to": NEXT}).json()
    assert OLD not in [s["key"] for s in day["sessions"]]                          # last year's turns are out
    assert day["cloud"]["tokens"] == _u(i=3_100_000, o=1_000_000)             # the corpus's 2.1M + desk's 1M
    half = url(**{"from": "2026-01-02T10:30:00Z", "to": NEXT}).json()              # only turns that started after it
    assert half["cloud"]["tokens"] == _u(i=500_000) and half["cloud"]["usd"] == pytest.approx(1.1)
    desk = url(**{"from": DAY, "to": NEXT}, host="desk").json()
    assert [s["key"] for s in desk["sessions"]] == ["desk/claude/d1"] and desk["cloud"]["usd"] == pytest.approx(4.4)
    assert url(**{"from": DAY, "to": NEXT}, host="nobody").json()["cloud"]["usd"] == 0.0


def test_spend_reports_the_cloud_when_SAINT_is_unreachable(tmp_path):
    store = Store(tmp_path / "hub.db")
    _corpus(store)
    saint = S.SaintUsage("http://saint.local:4000/usage/v1")
    saint.last_error = "ConnectError: connection refused"
    saint.window = lambda f, t: None
    body = _hub(store, saint=saint).get("/v1/spend", params={"from": DAY, "to": NEXT}).json()
    assert body["local"]["energy_usd"] is None and body["local"]["error"] == "ConnectError: connection refused"
    assert body["local"]["source"] is None and body["local"]["if_cloud_usd"] is None
    assert body["total_usd"] == pytest.approx(body["cloud"]["usd"])               # cloud only
    assert body["jobs"][0]["local_usd"] is None and body["jobs"][0]["if_cloud_usd"] is not None


def test_spend_without_a_usage_endpoint_has_no_local_at_all(tmp_path):
    store = Store(tmp_path / "hub.db")
    _corpus(store)
    body = _hub(store, saint=S.SaintUsage("")).get("/v1/spend", params={"from": DAY, "to": NEXT}).json()
    assert body["local"] == {"energy_usd": None, "if_cloud_usd": None, "tokens": 0, "requests": 0,
                             "price_as": "claude-opus-5-5", "source": None, "error": None}


def test_spend_lists_a_job_saint_knows_that_the_ledger_never_linked(tmp_path):
    store = Store(tmp_path / "hub.db")
    _sess(store, P)
    store.upsert_turns(P, [_turn(0)], None)
    c = _hub(store, saint=_saint(_window_totals(), [{"job": "sa-4-j1", "status": "error:timeout", "turns": 3,
                                                     "tokens_by_type": _u(i=1_000_000), "started_at": IN_DAY}]))
    (job,) = c.get("/v1/spend", params={"from": DAY, "to": NEXT}).json()["jobs"]
    assert (job["job"], job["status"], job["tokens"], job["key"]) == ("sa-4-j1", "error:timeout", 1_000_000, "")
    assert job["local_usd"] == pytest.approx(0.1)


# -- the session endpoints ---------------------------------------------------------------------------------------
def test_session_rows_carry_the_link_and_their_spend(tmp_path):
    store = Store(tmp_path / "hub.db")
    _sess(store, P)
    _sess(store, CHILD, parent_key=P, job="sa-2-j1", job_status="running", profile="max")
    store.set_liveness("laptop", [{"key": k, "status": "idle", "status_since": IN_DAY, "pid": 1, "socket": "/x"} for k in (P, CHILD)])
    store.upsert_turns(CHILD, [_turn(0, model="claude-haiku-4-5-20251001-v1:0", usage=_u(i=1_000_000, o=1_000_000))], None)
    rows = {r["key"]: r for r in _hub(store).get("/v1/sessions", headers=H).json()["sessions"]}
    assert (rows[CHILD]["parent_key"], rows[CHILD]["job"], rows[CHILD]["job_status"], rows[CHILD]["job_brief"]) == \
           (P, "sa-2-j1", "running", "")
    assert rows[P]["children"] == 1 and rows[CHILD]["children"] == 0
    assert rows[CHILD]["spend"]["usd"] == pytest.approx(1.1 + 5.5) and rows[CHILD]["spend"]["tokens"] == 2_000_000
    assert rows[CHILD]["spend"]["account"] == "max" and rows[CHILD]["spend"]["unpriced"] is False
    assert rows[P]["spend"] == {"usd": None, "tokens": 0, "by_type": _u(), "model": "", "account": "default",
                                "unpriced": False}
    one = _hub(store).get(f"/v1/sessions/{P}", headers=H).json()
    assert one["children_keys"] == [CHILD] and one["children"] == 1 and one["spend"]["local"] is None


def test_a_detail_row_asks_SAINT_about_its_session(tmp_path):
    store = Store(tmp_path / "hub.db")
    _sess(store, PI)
    store.upsert_turns(PI, [_turn(0, model="saint/saint-auto", usage=_u(i=10))], None)
    _sess(store, SH)
    store.upsert_turns(SH, [_turn(0, model="", usage=None)], None)
    saint = S.SaintUsage("http://saint.local:4000/usage/v1")
    saint.session = lambda sid: {"totals": {"energy_cost_usd": 0.48, "tokens": {"input": 17_300_000}, "requests": 342}}
    body = _hub(store, saint=saint).get(f"/v1/sessions/{PI}", headers=H).json()
    assert body["spend"]["local"] == {"energy_usd": 0.48, "tokens": 17_300_000, "requests": 342}
    shell = _hub(store, saint=saint).get(f"/v1/sessions/{SH}", headers=H).json()
    assert shell["spend"]["local"] is None                                # a bash never ran on SAINT


def test_a_config_may_price_a_model_the_hub_does_not_know(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[hub]\nurl = "http://h:7433"\nusage_url = "http://box:1/usage/v1"\n'
                        '[hub.prices."my-model"]\ninput = 1.0\noutput = 2.0\ncache_read = 0.1\ncache_creation = 0.0\n')
    cfg = load(cfg_file)
    assert cfg.hub.prices == {"my-model": {"input": 1.0, "output": 2.0, "cache_read": 0.1, "cache_creation": 0.0}}
    assert cfg.hub.usage_url == "http://box:1/usage/v1" and cfg.hub.local_price_as == "claude-opus-5-5"
    store = Store(tmp_path / "hub.db")
    _sess(store, P)
    store.upsert_turns(P, [_turn(0, model="my-model", usage=_u(i=1_000_000, o=1_000_000))], None)
    body = _hub(store, prices=cfg.hub.prices).get("/v1/spend", params={"from": DAY, "to": NEXT}).json()
    assert body["rates"] == "config" and body["cloud"]["usd"] == pytest.approx(3.0) and body["unpriced_models"] == []
    assert _hub(store, prices=cfg.hub.prices).get(f"/v1/sessions/{P}", headers=H).json()["spend"]["usd"] == pytest.approx(3.0)
