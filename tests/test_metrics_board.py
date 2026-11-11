"""The board's CPU/GPU panel: what one host's collector last said about its silicon, in two lines plus one a card.

The collector and the hub's side of the contract live on another branch, so every test here hands the board a fake
client with the contract's ``metrics()``: nothing touches a real hub. A hub without the route must cost the panel
its words and nothing else."""
from __future__ import annotations

import json
import re

from rich.text import Text
from textual.widgets import Static

from ctxswitch.client.tui import app as tui
from ctxswitch.client.tui.metrics_panel import (METRICS_HUB_MSG, METRICS_NONE_MSG, HostPicker, fits_height,
                                                hosts_text, panel_text, pick_host, plan_heights)
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
def test_a_sample_at_the_boards_usual_width_says_it_in_five_lines():
    t = panel_text("specul8", _sample(), 60)
    lines = t.plain.splitlines()
    assert len(lines) == 5 and all(len(ln) <= 60 for ln in lines)
    assert lines[0].startswith("specul8 · ") and "1.8 GHz" in lines[0] and "46°C" in lines[0]
    assert "Threadripper PRO 3945WX" in lines[0] and "AMD " not in lines[0] and "12-Core" not in lines[0]
    assert "GPU0 RTX 4080" in lines[3] and "GPU1 AMD GPU" in lines[4]
    assert "13.6/15.3G" in lines[3] and "28.4/31.9G" in lines[4]
    assert "29°C" in lines[3] and "31°C" in lines[4] and "21.0W" in lines[4] and "3.5W" in lines[3]
    assert lines[1].startswith("CPU") and len(re.findall(r"[▁▂▃▄▅▆▇█]", lines[1])) == 24      # one char a core
    assert lines[2].startswith("MEM") and lines[2].rstrip().endswith("38/58G")              # memory on its own row
    assert lines[3].split(" ")[-3].startswith("13.6/") and lines[3].rstrip().endswith("3.5W")   # 14.6e9 B = 13.6 GiB


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
    assert spark(at100[3]) > spark(at60[3]) == 8, (at60[3], at100[3])


def test_a_host_with_no_gpus_says_only_its_title_cpu_and_memory():
    lines = panel_text("laptop", _sample(gpus=[]), 60).plain.splitlines()
    assert len(lines) == 3 and lines[1].startswith("CPU") and lines[2].startswith("MEM") and "38/58G" in lines[2]


def test_a_value_the_collector_cant_read_prints_a_dash_not_a_guess():
    blind = _sample(gpus=[{"index": 0, "vendor": "nvidia", "name": "NVIDIA GeForce RTX 4090", "util": None,
                           "mem_used": None, "mem_total": None, "temp_c": None, "power_w": None}], temp=None, mhz=None)
    lines = panel_text("specul8", blind, 100).plain.splitlines()
    assert "—" in lines[3] and "GHz" not in lines[0] and "46°C" not in lines[0]
    assert "—" in panel_text("specul8", {}, 60).plain                             # nothing at all is not an error


def test_ten_cards_are_ten_lines_and_the_last_one_is_gpu9():
    entry = _sample(gpus=_ten())
    entry["history"]["gpus"] = {str(i): [10.0] * 60 for i in range(10)}
    for w in (44, 60, 100):
        lines = panel_text("specul8", entry, w).plain.splitlines()
        assert len(lines) == 13, (w, lines)
        assert all(len(ln) <= w for ln in lines), (w, lines)
        assert "GPU9" in lines[-1] and lines[3].startswith("GPU0 ")


def test_a_panel_half_the_board_high_shows_what_fits_and_says_the_rest():
    entry = _sample(gpus=_ten())
    entry["history"]["gpus"] = {str(i): [10.0] * 60 for i in range(10)}
    shown, more = fits_height(entry, 12)                     # a 24-row board: half of it
    assert shown + more == 10 and shown < 10
    lines = panel_text("specul8", entry, 60, more).plain.splitlines()
    assert len(lines) == 3 + shown + 1 and lines[-1].startswith(f"+{more} more GPUs")
    assert all(len(ln) <= 60 for ln in lines)
    assert fits_height(entry, 200) == (10, 0) and fits_height(_sample(gpus=[]), 12) == (0, 0)


# -- colours ------------------------------------------------------------------------------------------------------
def test_a_busy_card_is_red_a_warm_one_yellow_and_a_quiet_one_green():
    for util, want in ((90.0, "red"), (60.0, "yellow"), (10.0, "green")):
        t = panel_text("specul8", _sample(gpus=[_gpu(0, util=util, name="AMD GPU")]), 100)
        line = t.plain.splitlines()[3]
        assert f"{util:.0f}%" in line and "█" in line.split("GPU0 ")[1], line
        assert _style_of(t, 3, f"{util:.0f}%") == want, (util, line)         # its percent, and the bar beside it
        filled = round(util / 10)
        assert _style_of(t, 3, "█" * filled + "░" * (10 - filled)) == want, (util, line)


def test_vram_nearly_full_is_red_and_a_host_that_stopped_reporting_is_stale():
    full = _sample(gpus=[_gpu(0, used=int(round(32 * 2**30 * 0.98)), total=32 * 2**30)])   # a 32 GiB card, 98% full
    assert _style_of(panel_text("specul8", full, 100), 3, "31.4/32.0G") == "red"
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
        assert json.loads((tmp_path / "metrics-panel").read_text()) == {"shown": True, "hosts": []}
        assert app.client.metrics_calls                                          # shown: now it asks
        n = len(app.client.metrics_calls)
        await pilot.pause(4.5)
        assert len(app.client.metrics_calls) > n                                 and n > 0        # every 2 s
        await pilot.press("g"); await pilot.pause(0.5)
        assert not app.query_one("#metrics").display
        assert json.loads((tmp_path / "metrics-panel").read_text()) == {"shown": False, "hosts": []}
        n = len(app.client.metrics_calls)
        await pilot.pause(4.5)
        assert len(app.client.metrics_calls) == n                                # hidden again: no more asking


async def test_a_board_that_left_the_panel_on_shows_it_as_soon_as_it_mounts(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, shown="on")          # a board that starts shown fetches as soon as it mounts
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(2.5)
        assert app.query_one("#metrics").display and "GPU0 RTX 4080" in "\n".join(_panel(app))


async def test_G_opens_the_picker_over_the_board(monkeypatch, tmp_path):
    hosts = {"alpha": _sample("alpha", gpus=[_gpu(0)]), "beta": _sample("beta", gpus=[_gpu(0)])}
    app = _board(monkeypatch, tmp_path, body={"hosts": hosts})
    app.metrics_on = True
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(2.5)
        assert _panel(app)[0].startswith(("alpha", "beta"))                  # nothing picked: the automatic one
        first = _panel(app)[0].split(" ·")[0]
        await pilot.press("G"); await pilot.pause(0.5)
        assert isinstance(app.screen, HostPicker)
        await pilot.press("escape"); await pilot.pause(2.5)                  # closing changes nothing
        assert not isinstance(app.screen, HostPicker)
        assert _panel(app)[0].split(" ·")[0] == first                        # still the automatic host
        assert app.client.metrics_calls[-1] is None                          # it asks for the lot, then picks


async def test_a_hub_with_no_metrics_route_says_so_and_keeps_trying(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, body=ValueError("HTTP 404"))
    app.metrics_on = True                                    # what a board that starts shown does at mount
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(2.5)
        text = "\n".join(_panel(app))
        assert text == METRICS_HUB_MSG                                        # it predates them, and says so
        assert app.query_one("#table").row_count == 1                        # the rest of the board is untouched
        n = len(app.client.metrics_calls)
        await pilot.pause(4.5)
        assert len(app.client.metrics_calls) > n                             # it keeps asking


async def test_a_hub_nobody_has_reported_to_yet_says_that(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, body={"hosts": {}})
    app.metrics_on = True
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(2.5)
        assert "\n".join(_panel(app)) == METRICS_NONE_MSG                    # opt-in: nothing is sampling


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
        assert lines[3].startswith("GPU0 ") and any(ln.startswith("GPU6 ") for ln in lines)   # 3 fixed + 7 cards + "+3 more"


# -- several hosts at once ----------------------------------------------------------------------------------------
def _three(*, big=10, mid=1, small=0):
    """Three hosts as the hub's body holds them: one with ``big`` cards, one with ``mid``, one with none."""
    return {"alpha": _sample("alpha", gpus=[_gpu(i) for i in range(big)]),
            "beta": _sample("beta", gpus=[_gpu(0) for _ in range(mid)]),
            "scrub": _sample("scrub", gpus=[_gpu(0) for _ in range(small)])}


def test_the_height_plan_keeps_every_hosts_cpu_lines_and_counts_the_rest():
    counts = [10, 1, 0]                                       # alpha, beta, scrub, in sorted order
    # every shown host keeps 3 lines (title, CPU, MEM); room = half the board less its border
    assert plan_heights(counts, 46) == ([10, 1, 0], 0)        # room 22 for all 20 lines: every card of every host
    assert plan_heights(counts, 40) == ([8, 0, 0], 0)         # room 19: cards go out in sorted host order, and the
    assert plan_heights(counts, 30) == ([3, 0, 0], 0)         # "+N more GPUs" lines cost a line each
    assert plan_heights(counts, 24) == ([0, 0, 0], 0)         # room 11: three hosts' fixed lines and two "+N more"
    assert plan_heights(counts, 20) == ([0, 0, None], 1)      # room 9: scrub has no room at all
    assert plan_heights(counts, 16) == ([2, None, None], 2)   # room 7: alpha alone, and "+2 more hosts"
    assert plan_heights(counts, 8) == ([None, None, None], 3)  # room 3: nothing but "+3 more hosts" fits
    for height in range(0, 60):                              # and at no height does it promise more than half
        per, more = plan_heights(counts, height)
        lines = sum(0 if p is None else 2 + p + (1 if p < c else 0) for p, c in zip(per, counts)) + bool(more)
        assert lines <= max(0, height // 2 - 1) or height < 4, (height, per, more, lines)
        if None in per:                                      # what gets dropped is the last hosts, never one between
            i = per.index(None)
            assert all(p is None for p in per[i:]) and more == len(counts) - i


def test_the_height_plan_counts_the_more_lines_it_owes():
    """A "+N more GPUs" line is a line: the plan that hands out every card can still be one over the half."""
    for counts in ([3, 3, 3], [0, 0, 0], [8, 8], [1] * 5, [0, 10, 0]):
        for height in range(0, 60):
            per, more = plan_heights(counts, height)
            lines = sum(0 if p is None else 3 + p + (1 if p < c else 0) for p, c in zip(per, counts)) + bool(more)
            assert lines <= max(0, height // 2 - 1) or height < 4, (counts, height, per, more, lines)
    assert plan_heights([3, 3, 3], 24) == ([3, 0, None], 1)   # room 11: alpha whole, beta's 3 lines + its "+3 more"
    assert plan_heights([0, 0, 0], 12) == ([0, None, None], 2)  # room 5: one host's 3 lines, then "+2 more hosts"


def test_hosts_text_stacks_the_chosen_hosts_sorted():
    hosts = _three(big=1, mid=1)
    t = hosts_text(hosts, ["beta", "alpha"], 60, 40).plain.splitlines()
    assert t[0].startswith("alpha · ") and t[1].startswith("CPU") and t[2].startswith("MEM") and "GPU0" in t[3]
    assert t[4].startswith("beta · ") and t[5].startswith("CPU") and t[6].startswith("MEM") and "GPU0" in t[7]
    assert sum(1 for ln in t if ln.startswith("alpha ·")) == 1 and len(t) == 8
    assert hosts_text(hosts, ["beta"], 60, 40).plain.splitlines()[0].startswith("beta ·")   # one host on its own


def test_a_chosen_host_that_stopped_reporting_gets_one_dim_line():
    hosts = _three(big=1, mid=1)                              # "gone" is a host the hub's body has no entry for
    t = hosts_text(hosts, ["beta", "gone"], 60, 40)
    lines = t.plain.splitlines()
    assert lines[-1] == "gone · no samples (is metrics_every set on it?)"
    assert _style_of(t, len(lines) - 1, "no samples") == "dim"
    assert lines[0].startswith("beta · ")                     # the host that does report keeps its block


def test_hosts_text_left_to_itself_shows_the_one_host_it_would_have_picked():
    hosts = _three(big=3, mid=1)                              # an empty choice = the old automatic pick
    lines = hosts_text(hosts, [], 60, 40).plain.splitlines()
    assert len([ln for ln in lines if " · " in ln]) == 1 and lines[0].startswith("alpha · ")


# -- the G picker -----------------------------------------------------------------------------------------------------------------
async def test_G_lists_the_hosts_that_report_with_their_cards_and_toggles(monkeypatch, tmp_path):
    hosts = {"alpha": _sample("alpha", gpus=[_gpu(0)]), "beta": _sample("beta"), "scrub": _sample("scrub", gpus=[])}
    app = _board(monkeypatch, tmp_path, body={"hosts": hosts})
    app.metrics_on = True
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause(2.5)
        await pilot.press("G"); await pilot.pause(0.5)
        assert isinstance(app.screen, HostPicker)
        body = app.screen.query_one(Static).render().plain
        assert "1 [ ] alpha  1 GPU" in body and "2 [ ] beta  2 GPUs" in body
        assert "3 [ ] scrub  no GPUs" in body
        await pilot.press("2"); await pilot.pause(0.3)        # a digit toggles, and says so in its box
        assert "2 [x] beta" in app.screen.query_one(Static).render().plain
        await pilot.press("3"); await pilot.press("1"); await pilot.pause(0.3)
        assert "3 [x] scrub" in app.screen.query_one(Static).render().plain
        assert "1 [x] alpha" in app.screen.query_one(Static).render().plain
        await pilot.press("enter"); await pilot.pause(2.5)
        assert not isinstance(app.screen, HostPicker)
        assert app.metrics_picked == ["alpha", "beta", "scrub"]          # what it dismisses is sorted
        assert sorted(json.loads((tmp_path / "metrics-panel").read_text())["hosts"]) == ["alpha", "beta", "scrub"]
        await pilot.press("G"); await pilot.pause(0.5)
        assert "1 [x] alpha" in app.screen.query_one(Static).render().plain   # the ticks came back
        await pilot.press("g"); await pilot.pause(0.5)              # G closes it too, keeping the set
        assert not isinstance(app.screen, HostPicker) and app.metrics_picked == ["alpha", "beta", "scrub"]
        assert app.query_one("#metrics").display               # the picker's g is not the board's: nothing toggled
        await pilot.press("G"); await pilot.pause(0.5)
        await pilot.press("2"); await pilot.pause(0.3)
        await pilot.press("escape"); await pilot.pause(2.5)              # closing keeps the ticks made in it
        assert app.metrics_picked == ["alpha", "scrub"]
        assert json.loads((tmp_path / "metrics-panel").read_text())["hosts"] == ["alpha", "scrub"]


async def test_G_picks_several_hosts_and_the_board_shows_them_all(monkeypatch, tmp_path):
    hosts = {"alpha": _sample("alpha", gpus=[_gpu(0)]), "beta": _sample("beta", gpus=[_gpu(1)]),
             "scrub": _sample("scrub", gpus=[])}
    app = _board(monkeypatch, tmp_path, body={"hosts": hosts})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.press("g"); await pilot.pause(2.5)
        assert _panel(app)[0].startswith(("alpha", "beta", "scrub"))     # nothing picked yet: one automatic host
        await pilot.press("G"); await pilot.pause(0.5)
        await pilot.press("1"); await pilot.press("2"); await pilot.pause(0.3)
        await pilot.press("enter"); await pilot.pause(2.5)
        lines = _panel(app)
        assert any(ln.startswith("alpha · ") for ln in lines) and any(ln.startswith("beta · ") for ln in lines)
        assert not any(ln.startswith("scrub · ") for ln in lines)        # not ticked: not shown
        assert json.loads((tmp_path / "metrics-panel").read_text()) == {"shown": True, "hosts": ["alpha", "beta"]}


async def test_a_board_started_from_the_file_shows_the_hosts_it_left_behind(monkeypatch, tmp_path):
    hosts = {"alpha": _sample("alpha", gpus=[_gpu(0)]), "beta": _sample("beta", gpus=[_gpu(1)]),
             "scrub": _sample("scrub", gpus=[])}
    app = _board(monkeypatch, tmp_path, body={"hosts": hosts},
                 shown='{"shown": true, "hosts": ["beta", "alpha"]}')
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(2.5)
        assert app.query_one("#metrics").display and app.metrics_picked == ["alpha", "beta"]
        lines = _panel(app)
        assert any(ln.startswith("alpha · ") for ln in lines) and any(ln.startswith("beta · ") for ln in lines)
        await pilot.press("g"); await pilot.pause(0.5)                   # off, and the hosts are kept
        assert json.loads((tmp_path / "metrics-panel").read_text()) == {"shown": False, "hosts": ["alpha", "beta"]}


async def test_the_old_one_word_file_still_opens_the_panel_on_the_automatic_host(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, shown="on")                      # a file an older board wrote
    assert tui._metrics_state() == (True, [])
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(2.5)
        assert app.query_one("#metrics").display and "specul8" in _panel(app)[0]   # the automatic host
        assert (tmp_path / "metrics-panel").read_text().strip() == "on"       # reading an old file changes nothing


async def test_a_chosen_host_the_hub_never_heard_of_shows_its_no_samples_line(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, body={"hosts": {"alpha": _sample("alpha", gpus=[_gpu(0)])}},
                 shown='{"shown": true, "hosts": ["alpha", "gone"]}')
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(2.5)
        lines = _panel(app)
        assert any(ln.startswith("alpha · ") for ln in lines)
        assert lines[-1] == "gone · no samples (is metrics_every set on it?)"


def test_the_two_ways_there_is_nothing_to_show_say_which_one_it_is():
    assert METRICS_NONE_MSG == "no host sends CPU/GPU samples: set [collector] metrics_every = 2 on the hosts to watch"
    assert METRICS_HUB_MSG == "this hub has no CPU/GPU samples (it predates them)"
    assert tui.METRICS_NONE_MSG == METRICS_NONE_MSG and tui.METRICS_HUB_MSG == METRICS_HUB_MSG
    assert "metrics_every" in dict(tui.HELP_TIPS)["CPU and GPU"] and "`G`" in dict(tui.HELP_TIPS)["CPU and GPU"]


async def test_G_with_the_panel_hidden_still_lists_the_hosts(monkeypatch, tmp_path):
    """A board nobody has shown yet has never asked the hub: G asks first and opens on the answer."""
    hosts = {"alpha": _sample("alpha", gpus=[_gpu(0)]), "beta": _sample("beta")}
    app = _board(monkeypatch, tmp_path, body={"hosts": hosts})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1)
        assert not app.query_one("#metrics").display and app.client.metrics_calls == []
        await pilot.press("G"); await pilot.pause(2.5)
        assert isinstance(app.screen, HostPicker)
        assert "1 [ ] alpha  1 GPU" in app.screen.query_one(Static).render().plain
        assert "2 [ ] beta  2 GPUs" in app.screen.query_one(Static).render().plain
        assert not app.query_one("#metrics").display                      # picking changes the set, not the showing
        await pilot.press("1"); await pilot.press("enter"); await pilot.pause(0.5)
        assert app.metrics_picked == ["alpha"] and not app.query_one("#metrics").display
        n = len(app.client.metrics_calls)                                 # the one ask G needed to list with
        await pilot.pause(4.5)
        assert len(app.client.metrics_calls) == n                         # hidden is hidden: no asking on its own
        await pilot.press("g"); await pilot.pause(2.5)                    # and now it shows just that one
        assert [ln.split(" ·")[0] for ln in _panel(app) if " · " in ln] == ["alpha"]


def test_mixed_card_names_keep_the_bars_in_one_column():
    """RTX 4080 is a character longer than AMD GPU, and GPU10 one longer than GPU9: the bars still line up."""
    gpus = [_gpu(i, name=n, vendor=v) for i, (n, v) in enumerate(
        [("NVIDIA GeForce RTX 4080", "nvidia")] + [("AMD GPU", "amd")] * 10)]
    lines = panel_text("specul8", _sample(gpus=gpus), 69).plain.splitlines()[3:]
    start = [next(i for i, c in enumerate(ln) if c in "█░") for ln in lines]      # where each bar begins
    assert len(lines) == 11 and len(set(start)) == 1, start


def test_the_cpu_bar_and_percent_sit_in_the_gpus_columns_on_every_host():
    """The CPU line's label is padded to the GPU labels' width, so its bar and percent line up with theirs — across
    every host stacked in the panel, not just within one."""
    laptop = {"latest": _sample(gpus=[_gpu(0, name="NVIDIA RTX 2000 Ada Generation Laptop GPU", vendor="nvidia")])["latest"]}
    box = {"latest": _sample(gpus=[_gpu(0, name="NVIDIA GeForce RTX 4080", vendor="nvidia")] +
                     [_gpu(i, name="AMD GPU") for i in range(1, 11)])["latest"]}
    lines = hosts_text({"laptop": laptop, "specul8": box}, ["laptop", "specul8"], 69, 80).plain.splitlines()
    bars = [ln for ln in lines if ln.startswith(("CPU", "GPU"))]
    bars = [ln for ln in lines if ln.startswith(("CPU", "MEM", "GPU"))]
    assert len(bars) == 2 * 2 + 1 + 11                                     # CPU + MEM per host, then the cards
    start = {next(i for i, c in enumerate(ln) if c in "█░") for ln in bars}   # where each bar begins
    assert len(start) == 1 and len({ln.index("%") for ln in bars}) == 1, bars
    assert "RTX 2000 Ada " in lines[3] and "Generation" not in lines[3]


def test_a_card_name_keeps_its_letters_and_the_sparkline_gives_way():
    """At the board's 66-67 usable columns, "RTX 2000 Ada" lost its last letter to a full-length sparkline."""
    laptop = _sample(gpus=[_gpu(0, name="NVIDIA RTX 2000 Ada Generation Laptop GPU", vendor="nvidia")])
    for w in (66, 67, 100):                         # at 60 the row is full even with the shortest sparkline
        lines = panel_text("laptop", laptop, w).plain.splitlines()
        assert "RTX 2000 Ada " in lines[3] and all(len(ln) <= w for ln in lines), (w, lines)
