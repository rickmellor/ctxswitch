"""The hub's half of the metrics panel: it keeps each host's latest sample and a short history, in memory only —
nothing here belongs in SQLite, so a test checks that the database is untouched."""
import sqlite3

from starlette.testclient import TestClient

from ctxswitch.hub.api import Hub
from ctxswitch.hub.metrics import Metrics
from ctxswitch.hub.store import Store

H = {"Authorization": "Bearer t"}


def sample(host="gpu-box", pct=10.0, utils=(1.0, 2.0), at="2026-11-03T12:00:00Z"):
    return {"host_id": host, "at": at,
            "cpu": {"model": "AMD Ryzen 9 7950X", "pct": pct, "cores": [pct, pct], "temp_c": 45.75, "mhz": 4000.0,
                    "load": [1.0, 0.5, 0.25]},
            "mem": {"used": 8 * 1024 ** 3, "total": 32 * 1024 ** 3},
            "gpus": [{"index": i, "vendor": "nvidia", "name": f"GPU {i}", "util": u, "mem_used": 1, "mem_total": 2,
                      "temp_c": 30.0, "power_w": 4.0} for i, u in enumerate(utils)]}


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
