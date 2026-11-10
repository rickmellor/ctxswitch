"""The board's CPU/GPU panel: what one host's collector last said about its silicon, in two lines plus one a card.

The collector and the hub's side of the contract live on another branch, so every test here hands the board a fake
client with the contract's ``metrics()``: nothing touches a real hub. A hub without the route must cost the panel
its words and nothing else."""
from __future__ import annotations

import re

from rich.text import Text

from ctxswitch.client.tui import app as tui
from ctxswitch.client.tui.metrics_panel import fits_height, panel_text, pick_host
from ctxswitch.common.config import Config

GB = 1_000_000_000
CORES = [3.0] * 24


def _sample(host="specul8", *, util=0.0, gpus=None, cores=None, age=3.0, model="AMD Ryzen Threadripper PRO 3945WX 12-Cores",
            mhz=1800.0, temp=46.0, mem=(41 * GB, 62 * GB)):
    """One host's entry exactly as GET /v1/metrics puts it under "hosts": latest, age_s, history."""
    gpus = [{"index": 0, "vendor": "nvidia", "name": "NVIDIA GeForce RTX 4080", "util": util, "mem_used": 14_600_000_000,
             "mem_total": 16_400_000_000, "temp_c": 29.0, "power_w": 3.5},
            {"index": 1, "vendor": "amd", "name": "AMD GPU", "util": util, "mem_used": 30_500_000_000,
             "mem_total": 34_200_000_000, "temp_c": 31.0, "power_w": 21.0}] if gpus is None else gpus
    return {"age_s": age,
            "latest": {"host_id": host, "at": "2026-11-10T09:00:00Z",
                       "cpu": {"model": model, "pct": 3.0, "cores": CORES if cores is None else cores,
                               "temp_c": temp, "mhz": mhz, "load": [0.66, 0.81, 0.84]},
                       "mem": {"used": mem[0], "total": mem[1]}, "gpus": gpus},
            "history": {"cpu": [3.0] * 60, "gpus": {str(g["index"]): [util] * 60 for g in gpus}}}


def _gpu(i, *, vendor="amd", name="AMD GPU", util=10.0, used=int(28.8 * GB), total=32 * GB, temp=31.0, power=21.0):
    return {"index": i, "vendor": vendor, "name": name, "util": util, "mem_used": used, "mem_total": total,
            "temp_c": temp, "power_w": power}


def _ten():
    """The host going to ten cards: two NVIDIA first, then eight AMD, 32 GB each."""
    return ([_gpu(i, vendor="nvidia", name=f"NVIDIA GeForce RTX 4080 {i}") for i in (0, 1)]
            + [_gpu(i) for i in range(2, 10)])


class FakeClient:
    """The hub as the contract describes it. Pass an Exception as ``body`` and every metrics call fails, as it does
    on a hub that has no /v1/metrics."""

    def __init__(self, rows, body=None):
        self.rows = rows
        self.body = {"hosts": {"specul8": _sample()}} if body is None else body
        self.metrics_calls: list = []

    def sessions(self, host=None, live_only=False):
        return {"sessions": [dict(x) for x in self.rows], "change": 1}

    def session(self, key, turns=50):
        return {**dict(next((x for x in self.rows if x["key"] == key), {"key": key})), "turns": []}

    def attention(self, host=None):
        return {"items": []}

    def hosts(self):
        return [{"host_id": "laptop"}, {"host_id": "specul8"}]

    def spend(self, frm, to, host=None):
        return None

    def metrics(self, host=None):
        self.metrics_calls.append(host)
        if isinstance(self.body, Exception):
            raise self.body
        return self.body

    def viewed(self, key, at):
        pass

    def ws_url(self):
        return "ws://x/v1"


def _board(monkeypatch, tmp_path, body=None, rows=(), shown=None) -> tui.CtxApp:
    cfg = Config(); cfg.hub.token = "t"; cfg.collector.host_id = "laptop"
    monkeypatch.setattr(tui, "METRICS_FILE", tmp_path / "metrics-panel")
    if shown is not None:
        (tmp_path / "metrics-panel").write_text(shown + "\n")
    monkeypatch.setattr(tui.tmux, "in_tmux", lambda: False)
    monkeypatch.setattr(tui.CtxApp, "listen_ws", lambda self: None)
    monkeypatch.setattr(tui.CtxApp, "join_tmux", lambda self: None)
    app = tui.CtxApp(cfg, None)
    app.client = FakeClient(list(rows) or [_sess()], body)
    return app


def _sess(key="laptop/claude/p1"):
    return {"key": key, "host_id": "laptop", "harness": "claude", "session_id": "p1", "title": "webapp@main work",
            "live_status": "idle", "status_since": "2026-11-10T09:00:00Z", "last_activity": "2026-11-10T09:00:00Z",
            "started_at": "2026-11-10T09:00:00Z", "repo_root": "/r/webapp", "git_branch": "main", "turn_count": 3,
            "meta": {}, "open_asks": [], "parent_key": "", "job": "", "job_status": "", "job_brief": ""}


def _panel(app) -> list[str]:
    return app.query_one("#metrics").render().plain.splitlines()


def _style_of(text: Text, line: int, needle: str) -> str:
    """The style the panel put on ``needle`` in ``line``: the span that covers it and holds no space."""
    lines = text.plain.splitlines()
    at = sum(len(x) + 1 for x in lines[:line]) + lines[line].index(needle)
    inside = [s for s in text.spans if s.start <= at < s.end]
    return min(inside, key=lambda s: s.end - s.start).style if inside else ""   # the tightest span over it


# -- panel_text, pure rendering -----------------------------------------------------------------------------------
def test_a_sample_at_the_boards_usual_width_says_it_in_four_lines():
    t = panel_text("specul8", _sample(), 60)
    lines = t.plain.splitlines()
    assert len(lines) == 4 and all(len(ln) <= 60 for ln in lines)
    assert lines[0].startswith("specul8 · ") and "1.8 GHz" in lines[0] and "46°C" in lines[0]
    assert "Threadripper PRO 3945WX" in lines[0] and "AMD " not in lines[0] and "12-Core" not in lines[0]
    assert "GPU0 RTX 4080" in lines[2] and "GPU1 AMD GPU" in lines[3]
    assert "14.6/16.4G" in lines[2] and "30.5/34.2G" in lines[3]
    assert "29°C" in lines[2] and "31°C" in lines[3] and "21.0W" in lines[3] and "3.5W" in lines[2]
    assert lines[1].startswith("CPU") and len(re.findall(r"[▁▂▃▄▅▆▇█]", lines[1])) == 24      # one char a core
    assert lines[2].split(" ")[-3].startswith("14.6/") and lines[2].rstrip().endswith("3.5W")


def test_every_width_the_board_comes_in_stays_inside_it():
    for w in (44, 60, 100):
        for entry in (_sample(), _sample(gpus=_ten()), _sample(gpus=[]), _sample(age=42.0)):
            lines = panel_text("specul8", entry, w).plain.splitlines()
            assert all(len(ln) <= w for ln in lines), (w, lines)


def test_a_narrow_pane_gives_up_the_core_strip_and_a_wide_one_more_history():
    at44 = panel_text("specul8", _sample(), 44).plain.splitlines()
    assert all(len(ln) <= 44 for ln in at44)
    assert len(re.findall(r"[▁▂▃▄▅▆▇█]", at44[1])) < 24 and "CPU" in at44[1]      # no room for 24 of them
    at60 = panel_text("specul8", _sample(), 60).plain.splitlines()
    at100 = panel_text("specul8", _sample(), 100).plain.splitlines()
    spark = lambda ln: len(re.search(r"% ([▁▂▃▄▅▆▇█]*)", ln).group(1))              # the run after the percent
    assert spark(at100[2]) > spark(at60[2]) == 8, (at60[2], at100[2])


def test_a_host_with_no_gpus_says_only_the_two_cpu_lines():
    lines = panel_text("laptop", _sample(gpus=[]), 60).plain.splitlines()
    assert len(lines) == 2 and lines[1].startswith("CPU") and "RAM 41/62G" in lines[1]


def test_a_value_the_collector_cant_read_prints_a_dash_not_a_guess():
    blind = _sample(gpus=[{"index": 0, "vendor": "nvidia", "name": "NVIDIA GeForce RTX 4090", "util": None,
                           "mem_used": None, "mem_total": None, "temp_c": None, "power_w": None}], temp=None, mhz=None)
    lines = panel_text("specul8", blind, 100).plain.splitlines()
    assert "—" in lines[2] and "GHz" not in lines[0] and "46°C" not in lines[0]
    assert "—" in panel_text("specul8", {}, 60).plain                             # nothing at all is not an error


def test_ten_cards_are_ten_lines_and_the_last_one_is_gpu9():
    entry = _sample(gpus=_ten())
    entry["history"]["gpus"] = {str(i): [10.0] * 60 for i in range(10)}
    for w in (44, 60, 100):
        lines = panel_text("specul8", entry, w).plain.splitlines()
        assert len(lines) == 12, (w, lines)
        assert all(len(ln) <= w for ln in lines), (w, lines)
        assert "GPU9" in lines[-1] and lines[2].startswith("GPU0 ")


def test_a_panel_half_the_board_high_shows_what_fits_and_says_the_rest():
    entry = _sample(gpus=_ten())
    entry["history"]["gpus"] = {str(i): [10.0] * 60 for i in range(10)}
    shown, more = fits_height(entry, 12)                     # a 24-row board: half of it
    assert shown + more == 10 and shown < 10
    lines = panel_text("specul8", entry, 60, more).plain.splitlines()
    assert len(lines) == 2 + shown + 1 and lines[-1].startswith(f"+{more} more GPUs")
    assert all(len(ln) <= 60 for ln in lines)
    assert fits_height(entry, 200) == (10, 0) and fits_height(_sample(gpus=[]), 12) == (0, 0)


# -- colours ------------------------------------------------------------------------------------------------------
def test_a_busy_card_is_red_a_warm_one_yellow_and_a_quiet_one_green():
    for util, want in ((90.0, "red"), (60.0, "yellow"), (10.0, "green")):
        t = panel_text("specul8", _sample(gpus=[_gpu(0, util=util, name="AMD GPU")]), 100)
        line = t.plain.splitlines()[2]
        assert f"{util:.0f}%" in line and "█" in line.split("GPU0 ")[1], line
        assert _style_of(t, 2, f"{util:.0f}%") == want, (util, line)         # its percent, and the bar beside it
        filled = round(util / 10)
        assert _style_of(t, 2, "█" * filled + "░" * (10 - filled)) == want, (util, line)


def test_vram_nearly_full_is_red_and_a_host_that_stopped_reporting_is_stale():
    full = _sample(gpus=[_gpu(0, used=int(round(32 * GB * 0.98)), total=32 * GB)])
    assert _style_of(panel_text("specul8", full, 100), 2, "31.4/32.0G") == "red"
    stale = panel_text("specul8", _sample(age=30.0), 100)
    assert "stale 30s" in stale.plain.splitlines()[0]
    assert _style_of(stale, 0, "stale 30s") == "yellow"
    assert "stale" not in panel_text("specul8", _sample(age=3.0), 100).plain


# -- which host the panel shows -----------------------------------------------------------------------------------
def test_the_configured_host_wins_then_the_one_with_the_most_gpus():
    two, one, none = _sample(gpus=[_gpu(0), _gpu(1)]), _sample(gpus=[_gpu(0)]), _sample(gpus=[])
    hosts = {"alpha": two, "beta": one, "here": two}
    assert pick_host(hosts, "beta", "here") == "beta"        # configured, even with fewer cards
    assert pick_host(hosts, "absent", "here") == "here"      # a tie goes to this host
    assert pick_host({"alpha": two, "beta": one}, "", "elsewhere") == "alpha"
    assert pick_host({"alpha": one, "beta": two}, "", "elsewhere") == "beta"
    assert pick_host({"alpha": none, "beta": none}, "", "elsewhere") == "alpha"   # all equal: alphabetical
    assert pick_host({}, "beta", "here") is None


# -- the board ----------------------------------------------------------------------------------------------------
def test_g_is_bound_to_the_panel_and_documented():
    bound = {k.strip(): b.action for b in tui.CtxApp.BINDINGS for k in b.key.split(",")}
    assert bound["g"] == "metrics" and bound["G"] == "metrics_host"
    assert ("g",) in {keys for keys, _, _ in tui.HELP_KEYS} and ("G",) in {keys for keys, _, _ in tui.HELP_KEYS}
    assert any("CPU and GPU" in words for _, shown, words in tui.HELP_KEYS if shown == "g")
    assert any("CPU and GPU" == title for title, _ in tui.HELP_TIPS)


async def test_g_shows_the_panel_and_remembers_it(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path)
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert not app.query_one("#metrics").display and app.client.metrics_calls == []   # hidden: no fetches at all
        await pilot.press("g"); await pilot.pause(2.5)
        assert app.query_one("#metrics").display
        assert "GPU0 RTX 4080" in "\n".join(_panel(app)) and "specul8" in _panel(app)[0]
        assert (tmp_path / "metrics-panel").read_text().strip() == "on"
        assert app.client.metrics_calls                                          # shown: now it asks
        n = len(app.client.metrics_calls)
        await pilot.pause(4.5)
        assert len(app.client.metrics_calls) > n                                 and n > 0        # every 2 s
        await pilot.press("g"); await pilot.pause(0.5)
        assert not app.query_one("#metrics").display
        assert (tmp_path / "metrics-panel").read_text().strip() == "off"
        n = len(app.client.metrics_calls)
        await pilot.pause(4.5)
        assert len(app.client.metrics_calls) == n                                # hidden again: no more asking


async def test_a_board_that_left_the_panel_on_shows_it_as_soon_as_it_mounts(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, shown="on")          # a board that starts shown fetches as soon as it mounts
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(2.5)
        assert app.query_one("#metrics").display and "GPU0 RTX 4080" in "\n".join(_panel(app))


async def test_G_moves_the_panel_to_the_next_host_that_reports(monkeypatch, tmp_path):
    hosts = {"alpha": _sample("alpha", gpus=[_gpu(0)]), "beta": _sample("beta", gpus=[_gpu(0)])}
    app = _board(monkeypatch, tmp_path, body={"hosts": hosts})
    app.metrics_on = True
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(2.5)
        assert _panel(app)[0].startswith(("alpha", "beta"))
        first = _panel(app)[0].split(" ·")[0]
        await pilot.press("G"); await pilot.pause(2.5)
        assert _panel(app)[0].split(" ·")[0] != first                       # the other one, and only that one
        assert app.client.metrics_calls[-1] is None                          # it asks for the lot, then picks
        await pilot.press("G"); await pilot.pause(2.5)
        assert _panel(app)[0].split(" ·")[0] == first                        # wrapping back round


async def test_a_hub_with_no_metrics_route_says_so_and_keeps_trying(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, body=ValueError("HTTP 404"))
    app.metrics_on = True                                    # what a board that starts shown does at mount
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(2.5)
        text = "\n".join(_panel(app))
        assert "no CPU/GPU samples from the hub" in text and "2026-11-10" in text
        assert app.query_one("#table").row_count == 1                        # the rest of the board is untouched
        n = len(app.client.metrics_calls)
        await pilot.pause(4.5)
        assert len(app.client.metrics_calls) > n                             # it keeps asking


async def test_a_hub_nobody_has_reported_to_yet_says_that(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, body={"hosts": {}})
    app.metrics_on = True
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(2.5)
        assert "no host has sent CPU/GPU samples yet" in "\n".join(_panel(app))


async def test_the_panel_never_takes_more_than_half_the_board(monkeypatch, tmp_path):
    entry = _sample(gpus=_ten())
    entry["history"]["gpus"] = {str(i): [10.0] * 60 for i in range(10)}
    app = _board(monkeypatch, tmp_path, body={"hosts": {"specul8": entry}})
    async with app.run_test(size=(70, 24)) as pilot:                # ten cards would need 12 lines; half is 12
        await pilot.press("g"); await pilot.pause(2.5)
        w = app.query_one("#metrics")
        lines = _panel(app)
        assert w.region.height <= 24 // 2, w.region
        assert lines[-1].startswith("+") and "more GPUs" in lines[-1] and len(lines) <= 24 // 2
        assert lines[2].startswith("GPU0 ") and any(ln.startswith("GPU7 ") for ln in lines)
