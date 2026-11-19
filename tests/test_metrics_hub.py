"""The hub's half of the metrics panel: it keeps each host's latest sample and a short history, in memory only —
nothing here belongs in SQLite, so a test checks that the database is untouched."""
import sqlite3

from starlette.testclient import TestClient

from ctxswitch.hub.api import Hub
from ctxswitch.hub.metrics import Metrics
from ctxswitch.hub.store import Store

H = {"Authorization": "Bearer t"}


def sample(host="gpu-box", pct=10.0, utils=(1.0, 2.0), at="2026-11-03T12:00:00Z", seats=None, total=None):
    d = {"host_id": host, "at": at,
         "cpu": {"model": "AMD Ryzen 9 7950X", "pct": pct, "cores": [pct, pct], "temp_c": 45.75, "mhz": 4000.0,
                 "load": [1.0, 0.5, 0.25]},
         "mem": {"used": 8 * 1024 ** 3, "total": 32 * 1024 ** 3},
         "gpus": [{"index": i, "vendor": "nvidia", "name": f"GPU {i}", "util": u, "mem_used": 1, "mem_total": 2,
                   "temp_c": 30.0, "power_w": 4.0} for i, u in enumerate(utils)]}
    if seats is not None:                  # an LLM host adds its vLLM seats; any other host sends no such key
        d["llm"] = {"window_s": 600.0,
                    "seats": [{"name": n, "up": v is not None, "tok_s": v, "running": 1 if v else 0, "waiting": 0,
                               "min": v, "avg": v, "max": v, "duty": 100.0 if v else 0.0} for n, v in seats.items()],
                    "total": {"tok_s": total, "min": total, "avg": total, "max": total, "duty": 50.0}}
    return d


def _hub(tmp_path):
    store = Store(tmp_path / "hub.db")
    return store, TestClient(Hub(store, token="t").app())


def _post(c, d, headers=H):
    return c.post("/v1/metrics", json=d, headers=headers)


def _rows(db):
    con = sqlite3.connect(db)
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}   # noqa: S608 - names from sqlite_master
    finally:
        con.close()


# -- the hub's store of it -----------------------------------------------------------------------------------------

def test_a_sample_is_kept_as_the_host_s_latest_with_its_history_in_order():
    m = Metrics(keep=60)
    m.put(sample(pct=10.0, utils=(1.0, 2.0)))
    m.put(sample(pct=20.0, utils=(5.0, 6.0)))
    h = m.get()["hosts"]["gpu-box"]
    assert h["latest"]["cpu"]["pct"] == 20.0 and h["age_s"] >= 0
    assert h["history"]["cpu"] == [10.0, 20.0] and h["history"]["gpus"] == {"0": [1.0, 5.0], "1": [2.0, 6.0]}


def test_the_latest_one_wins_and_each_host_keeps_its_own_curve():
    m = Metrics()
    m.put(sample(host="gpu-box", pct=10.0))
    m.put(sample(host="laptop", pct=90.0, utils=()))
    assert m.get()["hosts"]["gpu-box"]["latest"]["cpu"]["pct"] == 10.0
    assert m.get()["hosts"]["laptop"]["history"] == {"cpu": [90.0], "gpus": {}}
    m.put(sample(host="gpu-box", pct=11.0))
    assert m.get("gpu-box")["hosts"]["gpu-box"]["latest"]["cpu"]["pct"] == 11.0


def test_the_history_holds_the_last_keep_samples_oldest_first():
    m = Metrics(keep=3)
    for i in range(5):
        m.put(sample(pct=float(i)))
    assert m.get()["hosts"]["gpu-box"]["history"]["cpu"] == [2.0, 3.0, 4.0]


def test_a_gpu_that_goes_quiet_keeps_its_curve_and_one_that_appears_gets_one():
    m = Metrics(keep=5)
    m.put(sample(utils=(10.0, 20.0)))
    m.put(sample(utils=(11.0,)))                              # GPU1 stopped reporting: its line stays, unpadded
    h = m.get()["hosts"]["gpu-box"]["history"]["gpus"]
    assert h == {"0": [10.0, 11.0], "1": [20.0]}


def test_a_sample_that_names_no_host_is_dropped_and_a_null_util_is_null():
    m = Metrics()
    m.put({"at": "2026-11-03T12:00:00Z", "cpu": {"pct": 1.0}, "gpus": []})
    m.put(sample(pct=None, utils=(None,)))
    assert sorted(m.get()["hosts"]) == ["gpu-box"]           # the one with no host_id never got in
    assert m.get("gpu-box")["hosts"]["gpu-box"]["history"] == {"cpu": [None], "gpus": {"0": [None]}}
    assert m.get("never-reported-host") == {"hosts": {}}


# -- an LLM host's seats -------------------------------------------------------------------------------------------

def test_a_host_that_reports_seats_keeps_a_line_per_seat_and_one_for_the_total():
    m = Metrics(keep=60)
    m.put(sample(seats={"worker1": 219.3, "worker2": None}, total=219.3))
    m.put(sample(seats={"worker1": 180.0, "worker2": 40.0}, total=220.0))
    h = m.get()["hosts"]["gpu-box"]["history"]
    assert h["llm"] == {"worker1": [219.3, 180.0], "worker2": [None, 40.0]}    # a down seat is a gap, not a 0
    assert h["llm_total"] == [219.3, 220.0] and h["cpu"] == [10.0, 10.0]


def test_a_host_that_never_reported_seats_has_no_seat_keys_to_look_empty():
    m = Metrics()
    m.put(sample(host="gpu-host", seats={"worker1": 100.0}, total=100.0))
    m.put(sample(host="laptop", utils=()))
    hosts = m.get()["hosts"]
    assert hosts["gpu-host"]["history"]["llm"] == {"worker1": [100.0]}
    assert "llm" not in hosts["laptop"]["history"] and "llm_total" not in hosts["laptop"]["history"]


def test_a_seat_that_stops_reporting_keeps_its_line_unpadded():
    m = Metrics(keep=5)
    m.put(sample(seats={"worker1": 100.0, "worker2": 50.0}, total=150.0))
    m.put(sample(seats={"worker1": 110.0}, total=110.0))       # worker2's collector no longer names it
    h = m.get()["hosts"]["gpu-box"]["history"]
    assert h["llm"] == {"worker1": [100.0, 110.0], "worker2": [50.0]} and h["llm_total"] == [150.0, 110.0]


def test_a_sample_without_a_total_leaves_a_gap_in_it_rather_than_a_zero():
    m = Metrics()
    m.put(sample(seats={"worker1": None}))                     # every seat down: the collector says nothing in total
    assert m.get()["hosts"]["gpu-box"]["history"]["llm_total"] == [None]


def test_a_posted_sample_carries_its_seats_through_the_get(tmp_path):
    store, c = _hub(tmp_path)
    assert _post(c, sample(seats={"worker1": 219.3}, total=219.3)).json() == {"ok": True}
    h = c.get("/v1/metrics", headers=H).json()["hosts"]["gpu-box"]
    assert h["latest"]["llm"]["seats"][0]["name"] == "worker1" and h["latest"]["llm"]["total"]["tok_s"] == 219.3
    assert h["history"]["llm"] == {"worker1": [219.3]} and h["history"]["llm_total"] == [219.3]


# -- the routes ----------------------------------------------------------------------------------------------------

def test_a_posted_sample_comes_back_out_of_the_get(tmp_path):
    store, c = _hub(tmp_path)
    assert _post(c, sample()).json() == {"ok": True}
    h = c.get("/v1/metrics", headers=H).json()["hosts"]["gpu-box"]
    assert h["latest"]["cpu"]["model"] == "AMD Ryzen 9 7950X" and h["latest"]["gpus"][0]["vendor"] == "nvidia"
    assert h["age_s"] >= 0 and h["history"]["cpu"] == [10.0]


def test_the_history_only_grows_while_the_host_keeps_posting(tmp_path):
    store, c = _hub(tmp_path)
    for i in range(61):
        _post(c, sample(pct=float(i)))
    h = c.get("/v1/metrics", headers=H).json()["hosts"]["gpu-box"]
    assert len(h["history"]["cpu"]) == 60 and h["history"]["cpu"][0] == 1.0 and h["history"]["cpu"][-1] == 60.0
    assert len(h["history"]["gpus"]["0"]) == 60


def test_the_host_filter_shows_one_host_and_a_stranger_shows_none(tmp_path):
    store, c = _hub(tmp_path)
    _post(c, sample(host="gpu-box")), _post(c, sample(host="laptop"))
    assert sorted(c.get("/v1/metrics", headers=H).json()["hosts"]) == ["gpu-box", "laptop"]
    assert sorted(c.get("/v1/metrics?host=laptop", headers=H).json()["hosts"]) == ["laptop"]
    assert c.get("/v1/metrics?host=nowhere", headers=H).json() == {"hosts": {}}


def test_both_metrics_routes_want_the_token(tmp_path):
    store, c = _hub(tmp_path)
    assert _post(c, sample(), headers={}).status_code == 401
    assert c.get("/v1/metrics").status_code == 401
    assert c.get("/v1/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_a_sample_that_names_no_host_is_refused(tmp_path):
    store, c = _hub(tmp_path)
    assert _post(c, {k: v for k, v in sample().items() if k != "host_id"}).status_code == 400
    assert _post(c, {**sample(), "host_id": ""}).status_code == 400
    assert c.get("/v1/metrics", headers=H).json() == {"hosts": {}}


def test_metrics_never_touch_the_database(tmp_path):
    store, c = _hub(tmp_path)
    store.host_seen("gpu-box", None)
    before = _rows(store.path)
    for i in range(3):
        _post(c, sample(pct=float(i)))
    c.get("/v1/metrics", headers=H)
    assert _rows(store.path) == before


# -- the rig-wide agent rows ---------------------------------------------------------------------------------------

import time
from datetime import datetime, timezone

from ctxswitch.hub.metrics import agent_rows

NOW = 1_800_000_000.0


def at(offset: float) -> str:
    """An ISO instant ``offset`` seconds before NOW (the hub's clock in these tests)."""
    return datetime.fromtimestamp(NOW + offset, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def calls_body(*records, host="laptop"):
    """A sample carrying call records; a record missing a name is the sample's host."""
    return sample(host=host, utils=()) | {"calls": [dict(r) for r in records]}


def test_one_call_gives_its_rate_its_span_and_a_full_history_bucket():
    rows = agent_rows([("bedrock", NOW - 10, 10.0, 1000)], NOW)
    assert rows["window_s"] == 600
    (r,) = rows["rows"]
    assert r["name"] == "bedrock" and r["calls"] == 1
    assert (r["tok_s"], r["min"], r["avg"], r["max"]) == (16.7, 100.0, 100.0, 100.0)   # 1000 over the last 60 s
    assert r["duty"] == 1.7                                          # 10 of the window's 600 s
    # the reply streamed over [now−20, now−10], so its 1000 tokens fill the second-to-last 10 s bucket and no other
    assert len(r["history"]) == 60 and r["history"][-2] == 100.0 and sum(r["history"]) == 100.0


def test_two_overlapping_calls_cover_the_window_once():
    calls = [("bedrock", NOW - 50, 50.0, 5000), ("bedrock", NOW - 20, 50.0, 5000)]   # [now-100,now-50] + [now-70,now-20]
    (r,) = agent_rows(calls, NOW)["rows"]
    assert r["duty"] == 13.3 and r["calls"] == 2                     # 80 s of union, not 100 s of intervals


def test_a_short_reply_moves_tokens_and_duty_but_not_min_avg_max():
    calls = [("max", NOW - 10, 10.0, 1000), ("max", NOW - 5, 5.0, 150)]   # the 150-token reply is all time-to-first-token
    (r,) = agent_rows(calls, NOW)["rows"]
    assert r["tok_s"] == 19.2 and (r["min"], r["avg"], r["max"]) == (100.0, 100.0, 100.0)
    assert r["duty"] == 2.5 and r["calls"] == 2                      # 15 s of one reply's span


def test_a_call_outside_the_window_leaves_no_row_at_all():
    assert agent_rows([("claude", NOW - 601, 10.0, 1000)], NOW) is None
    assert agent_rows([], NOW) is None                              # nothing to say, so no "agents" key


def test_every_label_is_its_own_row_sorted_by_name():
    calls = [("max", NOW - 10, 10.0, 400), ("agy", NOW - 12, 12.0, 360), ("bedrock", NOW - 8, 8.0, 640)]
    rows = agent_rows(calls, NOW)["rows"]
    assert [r["name"] for r in rows] == ["agy", "bedrock", "max"]
    assert [r["tok_s"] for r in rows] == [6.0, 10.7, 6.7]
    assert [r["calls"] for r in rows] == [1, 1, 1]


def test_the_hub_keeps_one_pile_so_a_label_across_hosts_is_one_row():
    m = Metrics()
    m.put(calls_body({"agent": "bedrock", "end": at(-30), "dur_s": 20.0, "out": 2000}, host="laptop"))
    m.put(calls_body({"agent": "bedrock", "end": at(-20), "dur_s": 20.0, "out": 2000}, host="gpu-host"))
    m.put(calls_body({"agent": "agy", "end": at(-15), "dur_s": 5.0, "out": 500}, host="devbox"))
    rows = m.get(now=NOW)["agents"]["rows"]
    assert [r["name"] for r in rows] == ["agy", "bedrock"]
    assert rows[1]["calls"] == 2 and rows[1]["min"] == 100.0         # both hosts' bedrock calls, together
    assert m.get(host="laptop", now=NOW)["agents"] == m.get(now=NOW)["agents"]   # ?host= filters hosts, never these


def test_a_record_that_is_not_one_is_skipped_and_the_rest_still_count():
    m = Metrics()
    m.put(calls_body({"agent": "bedrock", "dur_s": 10.0, "out": 500},                    # no end
                     {"agent": "bedrock", "end": at(-10), "dur_s": 0, "out": 500},       # no span
                     {"agent": "bedrock", "end": at(-10), "dur_s": 10.0, "out": "x"},    # tokens aren't a number
                     {"agent": "", "end": at(-10), "dur_s": 10.0, "out": 500},           # no label to row by
                     {"agent": "bedrock", "end": at(-10), "dur_s": 10.0, "out": 500}))
    (r,) = m.get(now=NOW)["agents"]["rows"]
    assert r["name"] == "bedrock" and r["calls"] == 1


def test_no_calls_means_no_agents_key_and_an_old_one_ages_out():
    m = Metrics()
    m.put(sample(host="laptop"))
    assert "agents" not in m.get(now=NOW)
    m.put(calls_body({"agent": "agy", "end": at(-30), "dur_s": 10.0, "out": 1000}))
    assert "agents" in m.get(now=NOW)
    assert "agents" not in m.get(now=NOW + 601)                      # the window moved past it
