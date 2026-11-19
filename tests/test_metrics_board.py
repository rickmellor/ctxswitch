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
                                                gpu_count, hosts_text, panel_text, pick_host, plan_heights)
from ctxswitch.common.config import Config

GB = 1_000_000_000
CORES = [3.0] * 24


def _sample(host="gpu-host", *, util=0.0, gpus=None, cores=None, age=3.0, model="AMD Ryzen Threadripper PRO 3945WX 12-Cores",
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


def _seat(name, *, up=True, tok_s=219.3, running=2, waiting=0, mn=140.2, avg=175.0, mx=231.9, duty=63.4):
    """One seat as the "llm" part of a sample lists it: its rate now, its window's min/avg/max, its duty."""
    return {"name": name, "up": up, "tok_s": tok_s, "running": running, "waiting": waiting,
            "min": mn, "avg": avg, "max": mx, "duty": duty}


def _total(*, tok_s=306.4, mn=150.0, avg=301.2, mx=420.5, duty=31.7):
    return {"tok_s": tok_s, "min": mn, "avg": avg, "max": mx, "duty": duty}


def _with_llm(entry, *, seats=None, total=None, hist=True):
    """A host's entry as a collector that lists ``[collector] llm_seats`` sends it: the same sample, plus its "llm"
    key and the hub's per-seat history beside the cards'. A host without them sends no "llm" key at all."""
    seats = [_seat("worker1"), _seat("worker2", tok_s=87.1, mn=60.0, avg=74.0, mx=91.2, duty=41.0)] \
        if seats is None else seats
    total = _total() if total is None else total
    entry["latest"]["llm"] = {"window_s": 600, "seats": seats, "total": total}
    if hist:
        entry["history"]["llm"] = {s["name"]: [s["tok_s"]] * 60 for s in seats}
        entry["history"]["llm_total"] = [total["tok_s"]] * 60
    return entry


def _ten():
    """The host going to ten cards: two NVIDIA first, then eight AMD, 32 GB each."""
    return ([_gpu(i, vendor="nvidia", name=f"NVIDIA GeForce RTX 4080 {i}") for i in (0, 1)]
            + [_gpu(i) for i in range(2, 10)])


class FakeClient:
    """The hub as the contract describes it. Pass an Exception as ``body`` and every metrics call fails, as it does
    on a hub that has no /v1/metrics."""

    def __init__(self, rows, body=None):
        self.rows = rows
        self.body = {"hosts": {"gpu-host": _sample()}} if body is None else body
        self.metrics_calls: list = []

    def sessions(self, host=None, live_only=False):
        return {"sessions": [dict(x) for x in self.rows], "change": 1}

    def session(self, key, turns=50):
        return {**dict(next((x for x in self.rows if x["key"] == key), {"key": key})), "turns": []}

    def attention(self, host=None):
        return {"items": []}

    def hosts(self):
        return [{"host_id": "laptop"}, {"host_id": "gpu-host"}]

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
    t = panel_text("gpu-host", _sample(), 60)
    lines = t.plain.splitlines()
    assert len(lines) == 5 and all(len(ln) <= 60 for ln in lines)
    assert lines[0].startswith("gpu-host · ") and "1.8 GHz" in lines[0] and "46°C" in lines[0]
    assert "Threadripper PRO 3945WX" in lines[0] and "AMD " not in lines[0] and "12-Core" not in lines[0]
    assert "#0 RTX 4080" in lines[3] and "#1 AMD GPU" in lines[4]
    assert "13.6/15.3G" in lines[3] and "28.4/31.9G" in lines[4]
    assert "29°C" in lines[3] and "31°C" in lines[4] and "21.0W" in lines[4] and "3.5W" in lines[3]
    assert lines[1].startswith("CPU") and len(re.findall(r"[▁▂▃▄▅▆▇█]", lines[1])) == 24      # one char a core
    assert lines[2].startswith("MEM") and lines[2].rstrip().endswith("38/58G")              # memory on its own row
    assert lines[3].split()[-3].startswith("13.6/") and lines[3].rstrip().endswith("3.5W")   # 14.6e9 B = 13.6 GiB


def test_every_width_the_board_comes_in_stays_inside_it():
    for w in (44, 60, 100):
        for entry in (_sample(), _sample(gpus=_ten()), _sample(gpus=[]), _sample(age=42.0)):
            lines = panel_text("gpu-host", entry, w).plain.splitlines()
            assert all(len(ln) <= w for ln in lines), (w, lines)


def test_a_narrow_pane_gives_up_the_core_strip_and_a_wide_one_more_history():
    at41 = panel_text("gpu-host", _sample(), 41).plain.splitlines()   # "#0" labels: 44 fits the strip exactly
    assert all(len(ln) <= 41 for ln in at41)
    assert len(re.findall(r"[▁▂▃▄▅▆▇█]", at41[1])) < 24 and "CPU" in at41[1]      # no room for 24 of them
    at60 = panel_text("gpu-host", _sample(), 60).plain.splitlines()
    at100 = panel_text("gpu-host", _sample(), 100).plain.splitlines()
    spark = lambda ln: len(re.search(r"% ([▁▂▃▄▅▆▇█]*)", ln).group(1))              # the run after the percent
    assert spark(at100[3]) > spark(at60[3]) == 8, (at60[3], at100[3])


def test_a_host_with_no_gpus_says_only_its_title_cpu_and_memory():
    lines = panel_text("laptop", _sample(gpus=[]), 60).plain.splitlines()
    assert len(lines) == 3 and lines[1].startswith("CPU") and lines[2].startswith("MEM") and "38/58G" in lines[2]


def test_a_value_the_collector_cant_read_prints_a_dash_not_a_guess():
    blind = _sample(gpus=[{"index": 0, "vendor": "nvidia", "name": "NVIDIA GeForce RTX 4090", "util": None,
                           "mem_used": None, "mem_total": None, "temp_c": None, "power_w": None}], temp=None, mhz=None)
    lines = panel_text("gpu-host", blind, 100).plain.splitlines()
    assert "—" in lines[3] and "GHz" not in lines[0] and "46°C" not in lines[0]
    assert "—" in panel_text("gpu-host", {}, 60).plain                             # nothing at all is not an error


def test_ten_cards_are_ten_lines_and_the_last_one_is_gpu9():
    entry = _sample(gpus=_ten())
    entry["history"]["gpus"] = {str(i): [10.0] * 60 for i in range(10)}
    for w in (44, 60, 100):
        lines = panel_text("gpu-host", entry, w).plain.splitlines()
        assert len(lines) == 13, (w, lines)
        assert all(len(ln) <= w for ln in lines), (w, lines)
        assert "#9" in lines[-1] and lines[3].startswith("#0 ")


def test_a_panel_half_the_board_high_shows_what_fits_and_says_the_rest():
    entry = _sample(gpus=_ten())
    entry["history"]["gpus"] = {str(i): [10.0] * 60 for i in range(10)}
    shown, more = fits_height(entry, 12)                     # a 24-row board: half of it
    assert shown + more == 10 and shown < 10
    lines = panel_text("gpu-host", entry, 60, more).plain.splitlines()
    assert len(lines) == 3 + shown + 1 and lines[-1].startswith(f"+{more} more GPUs")
    assert all(len(ln) <= 60 for ln in lines)
    assert fits_height(entry, 200) == (10, 0) and fits_height(_sample(gpus=[]), 12) == (0, 0)


# -- colours ------------------------------------------------------------------------------------------------------
def test_a_busy_card_is_red_a_warm_one_yellow_and_a_quiet_one_green():
    for util, want in ((90.0, "red"), (60.0, "yellow"), (10.0, "green")):
        t = panel_text("gpu-host", _sample(gpus=[_gpu(0, util=util, name="AMD GPU")]), 100)
        line = t.plain.splitlines()[3]
        assert f"{util:.0f}%" in line and "█" in line.split("#0 ")[1], line
        assert _style_of(t, 3, f"{util:.0f}%") == want, (util, line)         # its percent, and the bar beside it
        filled = round(util / 10)
        assert _style_of(t, 3, "█" * filled + "░" * (10 - filled)) == want, (util, line)


def test_vram_nearly_full_is_red_and_a_host_that_stopped_reporting_is_stale():
    full = _sample(gpus=[_gpu(0, used=int(round(32 * 2**30 * 0.98)), total=32 * 2**30)])   # a 32 GiB card, 98% full
    assert _style_of(panel_text("gpu-host", full, 100), 3, "31.4/32.0G") == "red"
    stale = panel_text("gpu-host", _sample(age=30.0), 100)
    assert "stale 30s" in stale.plain.splitlines()[0]
    assert _style_of(stale, 0, "stale 30s") == "yellow"
    assert "stale" not in panel_text("gpu-host", _sample(age=3.0), 100).plain


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
        assert "#0 RTX 4080" in "\n".join(_panel(app)) and "gpu-host" in _panel(app)[0]
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
        assert app.query_one("#metrics").display and "#0 RTX 4080" in "\n".join(_panel(app))


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
    app = _board(monkeypatch, tmp_path, body={"hosts": {"gpu-host": entry}})
    async with app.run_test(size=(70, 24)) as pilot:                # ten cards would need 12 lines; half is 12
        await pilot.press("g"); await pilot.pause(2.5)
        w = app.query_one("#metrics")
        lines = _panel(app)
        assert w.region.height <= 24 // 2, w.region
        assert lines[-1].startswith("+") and "more GPUs" in lines[-1] and len(lines) <= 24 // 2
        assert lines[3].startswith("#0 ") and any(ln.startswith("#6 ") for ln in lines)   # 3 fixed + 7 cards + "+3 more"


# -- several hosts at once ----------------------------------------------------------------------------------------
def _three(*, big=10, mid=1, small=0):
    """Three hosts as the hub's body holds them: one with ``big`` cards, one with ``mid``, one with none."""
    return {"alpha": _sample("alpha", gpus=[_gpu(i) for i in range(big)]),
            "beta": _sample("beta", gpus=[_gpu(0) for _ in range(mid)]),
            "devbox": _sample("devbox", gpus=[_gpu(0) for _ in range(small)])}


def test_the_height_plan_keeps_every_hosts_cpu_lines_and_counts_the_rest():
    counts = [10, 1, 0]                                       # alpha, beta, devbox, in sorted order
    # every shown host keeps 3 lines (title, CPU, MEM); room = half the board less its border
    assert plan_heights(counts, 46) == ([10, 1, 0], 0)        # room 22 for all 20 lines: every card of every host
    assert plan_heights(counts, 40) == ([8, 0, 0], 0)         # room 19: cards go out in sorted host order, and the
    assert plan_heights(counts, 30) == ([3, 0, 0], 0)         # "+N more GPUs" lines cost a line each
    assert plan_heights(counts, 24) == ([0, 0, 0], 0)         # room 11: three hosts' fixed lines and two "+N more"
    assert plan_heights(counts, 20) == ([0, 0, None], 1)      # room 9: devbox has no room at all
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
    assert t[0].startswith("alpha · ") and t[1].startswith("CPU") and t[2].startswith("MEM") and "#0" in t[3]
    assert t[4].startswith("beta · ") and t[5].startswith("CPU") and t[6].startswith("MEM") and "#0" in t[7]
    assert sum(1 for ln in t if ln.startswith("alpha ·")) == 1 and len(t) == 8
    assert hosts_text(hosts, ["beta"], 60, 40).plain.splitlines()[0].startswith("beta ·")   # one host on its own


def test_a_chosen_host_that_stopped_reporting_gets_one_dim_line():
    hosts = _three(big=1, mid=1)                              # "gone" is a host the hub's body has no entry for
    t = hosts_text(hosts, ["beta", "gone"], 60, 40)
    lines = t.plain.splitlines()
    assert lines[-1].rstrip() == "gone · no samples (is metrics_every set on it?)"
    assert _style_of(t, len(lines) - 1, "no samples") == "dim"
    assert lines[0].startswith("beta · ")                     # the host that does report keeps its block


def test_hosts_text_left_to_itself_shows_the_one_host_it_would_have_picked():
    hosts = _three(big=3, mid=1)                              # an empty choice = the old automatic pick
    lines = hosts_text(hosts, [], 60, 40).plain.splitlines()
    assert len([ln for ln in lines if " · " in ln]) == 1 and lines[0].startswith("alpha · ")


# -- the G picker -----------------------------------------------------------------------------------------------------------------
async def test_G_lists_the_hosts_that_report_with_their_cards_and_toggles(monkeypatch, tmp_path):
    hosts = {"alpha": _sample("alpha", gpus=[_gpu(0)]), "beta": _sample("beta"), "devbox": _sample("devbox", gpus=[])}
    app = _board(monkeypatch, tmp_path, body={"hosts": hosts})
    app.metrics_on = True
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause(2.5)
        await pilot.press("G"); await pilot.pause(0.5)
        assert isinstance(app.screen, HostPicker)
        body = app.screen.query_one(Static).render().plain
        assert "1 [ ] alpha  1 GPU" in body and "2 [ ] beta  2 GPUs" in body
        assert "3 [ ] devbox  no GPUs" in body
        await pilot.press("2"); await pilot.pause(0.3)        # a digit toggles, and says so in its box
        assert "2 [x] beta" in app.screen.query_one(Static).render().plain
        await pilot.press("3"); await pilot.press("1"); await pilot.pause(0.3)
        assert "3 [x] devbox" in app.screen.query_one(Static).render().plain
        assert "1 [x] alpha" in app.screen.query_one(Static).render().plain
        await pilot.press("enter"); await pilot.pause(2.5)
        assert not isinstance(app.screen, HostPicker)
        assert app.metrics_picked == ["alpha", "beta", "devbox"]          # what it dismisses is sorted
        assert sorted(json.loads((tmp_path / "metrics-panel").read_text())["hosts"]) == ["alpha", "beta", "devbox"]
        await pilot.press("G"); await pilot.pause(0.5)
        assert "1 [x] alpha" in app.screen.query_one(Static).render().plain   # the ticks came back
        await pilot.press("g"); await pilot.pause(0.5)              # G closes it too, keeping the set
        assert not isinstance(app.screen, HostPicker) and app.metrics_picked == ["alpha", "beta", "devbox"]
        assert app.query_one("#metrics").display               # the picker's g is not the board's: nothing toggled
        await pilot.press("G"); await pilot.pause(0.5)
        await pilot.press("2"); await pilot.pause(0.3)
        await pilot.press("escape"); await pilot.pause(2.5)              # closing keeps the ticks made in it
        assert app.metrics_picked == ["alpha", "devbox"]
        assert json.loads((tmp_path / "metrics-panel").read_text())["hosts"] == ["alpha", "devbox"]


async def test_G_picks_several_hosts_and_the_board_shows_them_all(monkeypatch, tmp_path):
    hosts = {"alpha": _sample("alpha", gpus=[_gpu(0)]), "beta": _sample("beta", gpus=[_gpu(1)]),
             "devbox": _sample("devbox", gpus=[])}
    app = _board(monkeypatch, tmp_path, body={"hosts": hosts})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.press("g"); await pilot.pause(2.5)
        assert _panel(app)[0].startswith(("alpha", "beta", "devbox"))     # nothing picked yet: one automatic host
        await pilot.press("G"); await pilot.pause(0.5)
        await pilot.press("1"); await pilot.press("2"); await pilot.pause(0.3)
        await pilot.press("enter"); await pilot.pause(2.5)
        lines = _panel(app)
        assert any(ln.startswith("alpha · ") for ln in lines) and any(ln.startswith("beta · ") for ln in lines)
        assert not any(ln.startswith("devbox · ") for ln in lines)        # not ticked: not shown
        assert json.loads((tmp_path / "metrics-panel").read_text()) == {"shown": True, "hosts": ["alpha", "beta"]}


async def test_a_board_started_from_the_file_shows_the_hosts_it_left_behind(monkeypatch, tmp_path):
    hosts = {"alpha": _sample("alpha", gpus=[_gpu(0)]), "beta": _sample("beta", gpus=[_gpu(1)]),
             "devbox": _sample("devbox", gpus=[])}
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
        assert app.query_one("#metrics").display and "gpu-host" in _panel(app)[0]   # the automatic host
        assert (tmp_path / "metrics-panel").read_text().strip() == "on"       # reading an old file changes nothing


async def test_a_chosen_host_the_hub_never_heard_of_shows_its_no_samples_line(monkeypatch, tmp_path):
    app = _board(monkeypatch, tmp_path, body={"hosts": {"alpha": _sample("alpha", gpus=[_gpu(0)])}},
                 shown='{"shown": true, "hosts": ["alpha", "gone"]}')
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(2.5)
        lines = _panel(app)
        assert any(ln.startswith("alpha · ") for ln in lines)
        assert lines[-1].rstrip() == "gone · no samples (is metrics_every set on it?)"


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
    """RTX 4080 is a character longer than AMD GPU, and #10 one longer than #9: the bars still line up."""
    gpus = [_gpu(i, name=n, vendor=v) for i, (n, v) in enumerate(
        [("NVIDIA GeForce RTX 4080", "nvidia")] + [("AMD GPU", "amd")] * 10)]
    lines = panel_text("gpu-host", _sample(gpus=gpus), 69).plain.splitlines()[3:]
    start = [next(i for i, c in enumerate(ln) if c in "█░") for ln in lines]      # where each bar begins
    assert len(lines) == 11 and len(set(start)) == 1, start


def test_the_cpu_bar_and_percent_sit_in_the_gpus_columns_on_every_host():
    """The CPU line's label is padded to the GPU labels' width, so its bar and percent line up with theirs — across
    every host stacked in the panel, not just within one."""
    laptop = {"latest": _sample(gpus=[_gpu(0, name="NVIDIA RTX 2000 Ada Generation Laptop GPU", vendor="nvidia")])["latest"]}
    box = {"latest": _sample(gpus=[_gpu(0, name="NVIDIA GeForce RTX 4080", vendor="nvidia")] +
                     [_gpu(i, name="AMD GPU") for i in range(1, 11)])["latest"]}
    lines = hosts_text({"laptop": laptop, "gpu-host": box}, ["laptop", "gpu-host"], 69, 80).plain.splitlines()
    bars = [ln for ln in lines if ln.startswith(("CPU", "#"))]
    bars = [ln for ln in lines if ln.startswith(("CPU", "MEM", "#"))]
    assert len(bars) == 2 * 2 + 1 + 11                                     # CPU + MEM per host, then the cards
    start = {next(i for i, c in enumerate(ln) if c in "█░") for ln in bars}   # where each bar begins
    assert len(start) == 1 and len({ln.index("%") for ln in bars}) == 1, bars
    card = next(ln for ln in lines if "RTX 2000" in ln)                        # hosts are sorted: find it by name
    assert "RTX 2000 Ada " in card and "Generation" not in card


def test_a_card_name_keeps_its_letters_and_the_sparkline_gives_way():
    """At the board's 66-67 usable columns, "RTX 2000 Ada" lost its last letter to a full-length sparkline."""
    laptop = _sample(gpus=[_gpu(0, name="NVIDIA RTX 2000 Ada Generation Laptop GPU", vendor="nvidia")])
    for w in (66, 67, 100):                         # at 60 the row is full even with the shortest sparkline
        lines = panel_text("laptop", laptop, w).plain.splitlines()
        assert "RTX 2000 Ada " in lines[3] and all(len(ln) <= w for ln in lines), (w, lines)


async def test_the_panel_is_pinned_above_the_keys_and_the_detail_still_scrolls(monkeypatch, tmp_path):
    """g's panel sits at the bottom, right on top of the key bar; the detail pane above it keeps its own scrolling
    region, ending where the panel starts."""
    app = _board(monkeypatch, tmp_path, body={"hosts": {"gpu-host": _sample(gpus=_ten())}})
    async with app.run_test(size=(70, 40)) as pilot:
        await pilot.press("g"); await pilot.pause(2.5)
        panel, keys = app.query_one("#metrics"), app.query_one("#keys")
        detail, table = app.query_one("#detail"), app.query_one("#table")
        assert panel.region.height > 3
        assert panel.region.y + panel.region.height == keys.region.y            # flush on top of the key bar
        assert table.region.y < detail.region.y < panel.region.y                # list, then detail, then the panel
        assert detail.region.y + detail.region.height <= panel.region.y         # no overlap: the detail ends above it
        assert detail.region.height > 0
        app.query_one("#detail_text").update("\n".join(f"line {i}" for i in range(200)))   # long enough to scroll
        await pilot.pause(0.3)
        assert detail.max_scroll_y > 0                                          # still a scrolling region
        detail.scroll_end(animate=False); await pilot.pause(0.2)
        assert detail.scroll_y == detail.max_scroll_y and panel.region.y + panel.region.height == keys.region.y


# -- the thread strip, segmented by physical core ---------------------------------------------------------------------

from ctxswitch.client.tui.metrics_panel import _core_strip, core_colour  # noqa: E402

TWELVE_BY_TWO = list(range(12)) * 2                       # the 3945WX: cpu0..11 are cores 0..11, cpu12..23 again


def test_a_cores_threads_sit_together_and_share_its_hue():
    threads = [90.0] + [5.0] * 11 + [85.0] + [5.0] * 11   # core 0 is busy on both its threads (cpu0 and cpu12)
    t, label = _core_strip(threads, TWELVE_BY_TWO)
    assert t.plain.startswith("▇▇") and len(t.plain) == 24 and label == "12c"
    styles = [str(s.style) for s in t.spans]
    assert styles[0] == core_colour(0, 12, 90.0) and styles[1] == core_colour(0, 12, 85.0)
    assert styles[2] == styles[3] == core_colour(1, 12, 5.0)            # core 1's two idle threads: one colour


def _rgb(style):
    return tuple(int(x) for x in re.fullmatch(r"rgb\((\d+),(\d+),(\d+)\)", style).groups())


def test_hue_runs_blue_to_red_across_the_cores_and_load_raises_saturation():
    r, g, b = _rgb(core_colour(0, 12, 100.0))
    assert b > r and b > g                                                # the first core is blue
    r, g, b = _rgb(core_colour(11, 12, 100.0))
    assert r > g and r > b                                                # the last core is red
    spread = lambda c: max(c) - min(c)                                    # a stand-in for saturation
    assert spread(_rgb(core_colour(3, 12, 100.0))) > spread(_rgb(core_colour(3, 12, 50.0))) > spread(_rgb(core_colour(3, 12, 0.0))) > 0
    assert core_colour(0, 1, 50.0) == core_colour(0, 1, 50.0)              # one core: no division by zero


def test_without_topology_the_strip_is_the_old_one_with_no_label():
    t, label = _core_strip([20.0, 50.0], None)
    assert t.plain == "▂▅" and label == "" and not any(str(s.style).startswith("rgb(") for s in t.spans)
    assert _core_strip([20.0, 50.0], [0])[1] == ""                         # a core_of of the wrong length is ignored
    assert _core_strip([], TWELVE_BY_TWO) is None


def test_the_cpu_row_shows_the_segments_and_the_core_count_when_it_fits():
    entry = _sample()
    entry["latest"]["cpu"]["core_of"] = TWELVE_BY_TWO
    wide = panel_text("gpu-host", entry, 100).plain.splitlines()[1]
    assert wide.endswith(" 12c") and len(re.findall(r"[▁▂▃▄▅▆▇█]", wide.split("%", 1)[1])) == 24
    for width in (44, 60, 100):                                             # never wider than the pane
        assert all(len(ln) <= width for ln in panel_text("gpu-host", entry, width).plain.splitlines())


# -- each host's title line is a band across the pane -------------------------------------------------------------

from ctxswitch.client.tui.metrics_panel import HEAD_BG  # noqa: E402


def _bg_cols(line: Text) -> set:
    return {i for s in line.spans if HEAD_BG in str(s.style) for i in range(s.start, s.end)}


def test_a_hosts_title_line_is_a_full_width_band_and_its_other_lines_are_not():
    for width in (44, 60, 100):
        lines = panel_text("gpu-host", _sample(), width).split("\n")
        assert len(lines[0].plain) == width and _bg_cols(lines[0]) == set(range(width))
        assert all(not _bg_cols(ln) for ln in lines[1:])
    head = panel_text("gpu-host", _sample(), 100).split("\n")[0]
    assert any("bold" in str(s.style) for s in head.spans)                   # the name keeps its own style


def test_a_host_with_no_samples_gets_the_band_too():
    from ctxswitch.client.tui.metrics_panel import header_band
    band = header_band(Text("devbox · no samples", "dim"), 40)
    assert len(band.plain) == 40 and _bg_cols(band) == set(range(40))
    assert len(header_band(Text("x" * 60), 40).plain) == 40                   # never wider than the pane


# -- the LLM rows: what the seats generated, and how busy they were --------------------------------------------------

from ctxswitch.client.tui.metrics_panel import line_count, llm_lines  # noqa: E402


def _llm_rows(entry, width=62) -> list[str]:
    return [ln for ln in panel_text("gpu-host", entry, width).plain.splitlines()
            if ln.startswith(("LLM", "worker"))]


def test_a_host_with_no_llm_seats_says_exactly_what_it_said_before():
    """The old shape is untouched: no "llm" key at all, or one with nothing in it, and the panel is byte for byte
    what it was before seats existed — including a seat list that came in empty."""
    plain = panel_text("gpu-host", _sample(), 62).plain
    for llm in ({}, {"window_s": 600, "seats": [], "total": None}, None):
        entry = _sample()
        entry["latest"]["llm"] = llm
        assert panel_text("gpu-host", entry, 62).plain == plain
    assert panel_text("gpu-host", _with_llm(_sample(), seats=[]), 62).plain == plain


def test_two_up_seats_get_an_llm_row_and_one_a_seat_between_the_memory_and_the_cards():
    lines = panel_text("gpu-host", _with_llm(_sample()), 62).plain.splitlines()
    assert lines[3].startswith("LLM") and lines[4].startswith("worker1") and lines[5].startswith("worker2")
    assert lines[6].startswith("#0 ")                       # the LLM rows sit above the cards
    rows = _llm_rows(_with_llm(_sample()))
    assert len(rows) == 3 and len({len(r) for r in rows}) == 1, rows
    assert all("tok/s" in r and "duty" in r for r in rows), rows
    assert len({r.index("tok/s") for r in rows}) == 1, rows   # one column for all three
    assert "219 tok/s" in rows[1] and "306 tok/s" in rows[0] and "140/ 175/ 232" in rows[1]
    assert rows[0].rstrip().endswith("duty  32%") and rows[1].rstrip().endswith("duty  63%")


def test_a_rate_the_collector_has_no_answer_for_prints_a_dash_and_a_down_seat_says_only_down():
    entry = _with_llm(_sample(), seats=[_seat("worker1", tok_s=None, mn=None, avg=None, mx=None, duty=None),
                                        _seat("worker2", up=False, tok_s=None, mn=None, avg=None, mx=None, duty=None)],
                      total=_total(tok_s=None, mn=None, avg=None, mx=None, duty=None))
    lines = panel_text("gpu-host", entry, 62).plain.splitlines()
    assert lines[3].startswith("LLM") and "tok/s" in lines[3] and "0 tok/s" not in lines[3]
    assert lines[3].count("—") == 5 and "duty   —%" in lines[3], lines[3]   # tok/s, the three rates, the duty — and
    assert lines[4].count("—") == 5 and lines[4].rstrip().endswith("—%")     # the seat's, its history still the block
    assert lines[5].rstrip() == "worker2  down" and "tok/s" not in lines[5]
    assert _style_of(panel_text("gpu-host", entry, 62), 5, "down") == "yellow"


def test_an_idle_seat_says_0_tok_s_and_duty_0_percent_not_a_dash():
    """0 is an answer: a seat that ran nothing in the window has a duty of 0 %, and only the rates it never had are
    dashes. A dash would say the collector couldn't tell."""
    entry = _with_llm(_sample(), seats=[_seat("worker1", tok_s=0.0, mn=None, avg=None, mx=None, duty=0.0)],
                      total=_total(tok_s=0.0, mn=None, avg=None, mx=None, duty=0.0))
    for row in _llm_rows(entry):
        assert row.rstrip().endswith("duty   0%") and " 0 tok/s" in row and row.count("—") == 3, row


def test_a_seats_history_is_its_own_whatever_its_neighbours_are_called():
    """A seat name longer than the CPU label is cut to it, so "worker12" and "worker13" can print the same word —
    the hub's history is still keyed by the whole name, so each row keeps its own run, scaled to its own fastest
    interval, and neither comes out blank."""
    entry = _with_llm(_sample(), seats=[_seat("worker12", tok_s=10.0, mx=200.0), _seat("worker13", tok_s=10.0, mx=20.0)])
    rows = [ln for ln in panel_text("gpu-host", entry, 44).plain.splitlines() if ln.startswith("worker")]
    assert len(rows) == 2 and all(ln.startswith("worker ") for ln in rows), rows     # cut to the label's width
    assert len({len(re.findall(r"[▁▂▃▄▅▆▇█]", r)) for r in rows}) == 1               # both found theirs, neither empty
    assert "▁" in rows[0] and "▅" in rows[1], rows           # 10 tok/s is a floor against 200, a shoulder against 20


def test_the_llm_rows_fit_every_pane_and_a_narrow_one_keeps_tok_s_and_duty():
    entry = _with_llm(_sample())
    for w in (41, 44, 62, 69, 100):
        lines = panel_text("gpu-host", entry, w).plain.splitlines()
        assert all(len(ln) <= w for ln in lines), (w, lines)
        assert len(_llm_rows(entry, w)) == 3, w
    rates = lambda r: re.search(r"\d+/\s*\d+/\s*\d+", r.split("tok/s")[1])       # the window's three rates
    for w in (41, 44):                                   # too narrow for the window: tok/s and duty stay
        rows = _llm_rows(entry, w)
        assert all("tok/s" in r and "duty" in r and not rates(r) for r in rows), (w, rows)
    for w in (62, 69, 100):                              # and the min/avg/max is back as soon as it fits
        assert all(rates(r) for r in _llm_rows(entry, w)), w
        assert "140/ 175/ 232" in _llm_rows(entry, w)[1], w


def test_a_short_panel_cuts_the_cards_first_and_the_seat_rows_only_after_them():
    """Ten cards and two seats in a panel too short for the lot: the LLM rows are worth too much to lose, so the
    cards go, and the line says GPUs; only when even the seats have to go does it start saying lines."""
    entry = _with_llm(_sample(gpus=_ten()))
    entry["history"]["gpus"] = {str(i): [10.0] * 60 for i in range(10)}
    roomy = hosts_text({"gpu-host": entry}, ["gpu-host"], 62, 30).plain.splitlines()
    assert roomy[-1].startswith("+3 more GPUs") and any(ln.startswith("worker2 ") for ln in roomy)
    assert sum(1 for ln in roomy if ln.startswith("#")) == 7 and len(roomy) <= 30 // 2 - 1
    tight = hosts_text({"gpu-host": entry}, ["gpu-host"], 62, 14).plain.splitlines()
    assert tight[-1].startswith("+11 more lines") and any(ln.startswith("LLM") for ln in tight)
    assert not any(ln.startswith("worker2 ") for ln in tight) and any(ln.startswith("worker1 ") for ln in tight)
    assert len(tight) <= 14 // 2 - 1, tight


def test_the_lines_a_host_costs_are_its_cards_plus_the_llm_row_and_its_seats():
    two, none = _with_llm(_sample()), _sample()
    assert gpu_count(two) == gpu_count(none) == 2           # the G picker still counts cards, only cards
    assert llm_lines(two) == 3 and llm_lines(none) == 0
    assert line_count(two) == gpu_count(two) + 3 and line_count(none) == 2
    assert line_count(_with_llm(_sample(gpus=_ten()))) == 13


def test_the_help_says_what_the_llm_rows_mean():
    tip = dict(tui.HELP_TIPS)["CPU and GPU"]
    assert "llm_seats" in tip and "duty" in tip and "tok/s" in tip
    assert "min/avg/max" in tip.replace(" ", "") or "min/avg/max" in tip


# -- the agent rows: what the accounts on the rig generated, from their own transcripts ----------------------------


def _agent(name, *, tok_s=23.5, mn=40.1, avg=81.0, mx=114.4, duty=34.2, calls=52, hist=None):
    """One agent row exactly as the hub's ``agents`` block puts it: an account's rate now, its window's min/avg/max
    per reply, the share of the window a call was in flight, how many calls, and 60 buckets of 10 s."""
    return {"name": name, "tok_s": tok_s, "min": mn, "avg": avg, "max": mx, "duty": duty, "calls": calls,
            "history": [tok_s] * 60 if hist is None else hist}


def _agents(*rows, window=600):
    """The GET /v1/metrics body's top-level "agents" block, beside "hosts" — absent, as the hub leaves it out when
    the window holds no calls."""
    return {"window_s": window, "rows": list(rows)}


def _show(hosts, width=62, height=200) -> list[str]:
    """hosts_text over a body's hosts, with its "agents" block when it has one (the "_agents" a test parked)."""
    hosts = dict(hosts)
    agents = hosts.pop("_agents", None)
    return hosts_text(hosts, sorted(hosts), width, height, agents=agents).plain.splitlines()


def test_no_agent_rows_change_not_a_character():
    """A hub with no "agents" key, an empty one, or one whose rows are empty: the board is byte for byte what it
    said before agents existed, with seats and without them."""
    for hosts in ({"gpu-host": _sample()}, {"gpu-host": _with_llm(_sample(gpus=_ten()))}):
        before = _show(hosts)
        for agents in (None, {}, {"window_s": 600}, _agents()):
            assert _show({**hosts, "_agents": agents}) == before, agents


def test_the_agent_rows_are_the_seat_rows_columns_under_the_seats():
    """One host, two accounts: LLM, its seats, then the accounts, then the cards — and an account row is a seat row
    in every column, only its own name and history."""
    entry = _with_llm(_sample())
    lines = panel_text("gpu-host", entry, 62, agents=[_agent("bedrock"), _agent("max", tok_s=8.4, duty=0.0)]
                       ).plain.splitlines()
    assert [ln.split()[0] for ln in lines[3:8]] == ["LLM", "worker1", "worker2", "bedrock", "max"]
    assert lines[8].startswith("#0 ")
    rows = [ln for ln in lines if ln.split()[0] in ("worker1", "worker2", "bedrock", "max")]
    assert len({len(r) for r in rows}) == 1, rows                    # as long as the seat rows
    assert len({r.index("tok/s") for r in rows}) == 1, rows          # and tok/s in the same column
    bedrock = lines[6]
    assert "tok/s" in bedrock and "40/  81/ 114" in bedrock and bedrock.rstrip().endswith("duty  34%"), bedrock


def test_the_agent_rows_appear_once_where_the_seats_are():
    """Rows are rig-wide, so they print once: in the first rendered host whose latest sample has LLM seats."""
    hosts = {"alpha": _sample("alpha"), "beta": _with_llm(_sample("beta"))}
    lines = _show({**hosts, "_agents": _agents(_agent("bedrock"), _agent("agy"))})
    assert sum(1 for ln in lines if ln.startswith("bedrock")) == 1 and sum(1 for ln in lines if ln.startswith("agy ")) == 1
    at = [i for i, ln in enumerate(lines) if ln.startswith(("alpha", "beta"))]        # the two title bands
    block_b = lines[at[1]:]
    assert block_b[0].startswith("beta") and block_b[3].startswith("LLM")
    assert block_b[6].startswith("bedrock") and block_b[7].startswith("agy") and not any(ln.startswith("agy") for ln in lines[:at[1]])


def test_without_llm_seats_anywhere_the_agent_rows_sit_under_the_memory_of_the_first_host():
    hosts = {"alpha": _sample("alpha"), "beta": _sample("beta")}
    lines = _show({**hosts, "_agents": _agents(_agent("max"))})
    at = [i for i, ln in enumerate(lines) if ln.startswith(("alpha", "beta"))]
    assert lines[at[0] + 3].startswith("max ") and sum(1 for ln in lines if ln.startswith("max ")) == 1
    assert lines[at[1] + 3].startswith("#0 "), lines[at[1]:at[1] + 4]      # the second host says nothing about agents


def test_an_account_that_answered_nothing_prints_dashes_and_an_idle_one_its_zero():
    """A window with no 200+ token reply has no min/avg/max to show; a duty of 0 is an answer, not a dash — and a
    row has no "up", so it is never the one that says "down"."""
    entry = _with_llm(_sample())
    rows = [ln for ln in panel_text("gpu-host", entry, 62, agents=[
        _agent("bedrock", tok_s=None, mn=None, avg=None, mx=None, duty=None),
        _agent("agy", tok_s=0.0, mn=None, avg=None, mx=None, duty=0.0)]).plain.splitlines()
        if ln.startswith(("bedrock", "agy"))]
    assert rows[0].count("—") == 5 and "duty   —%" in rows[0] and "down" not in rows[0], rows[0]
    assert rows[1].rstrip().endswith("duty   0%") and " 0 tok/s" in rows[1] and rows[1].count("—") == 3, rows[1]


def test_a_short_panel_cuts_the_agent_rows_after_the_cards_and_before_the_seats():
    """Ten cards, two seats and two accounts in a panel too short for the lot: the cards go first (and the line says
    GPUs), and only when the accounts have to go too does it start saying lines — the seats are worth more than
    they are."""
    rows = [_agent("bedrock"), _agent("max")]
    entry = _with_llm(_sample(gpus=_ten()))
    entry["history"]["gpus"] = {str(i): [10.0] * 60 for i in range(10)}
    roomy = hosts_text({"gpu-host": entry}, ["gpu-host"], 62, 34, agents=_agents(*rows)).plain.splitlines()
    assert roomy[-1].startswith("+3 more GPUs") and any(ln.startswith("max ") for ln in roomy)
    assert sum(1 for ln in roomy if ln.startswith("#")) == 7 and len(roomy) <= 34 // 2 - 1
    tight = hosts_text({"gpu-host": entry}, ["gpu-host"], 62, 14, agents=_agents(*rows)).plain.splitlines()
    assert tight[-1].startswith("+13 more lines") and any(ln.startswith("worker1 ") for ln in tight)
    assert any(ln.startswith("LLM") for ln in tight)
    assert not any(ln.startswith(("worker2 ", "bedrock", "max ")) for ln in tight), tight


def test_agent_rows_stay_inside_every_pane():
    entry = _with_llm(_sample(gpus=_ten()))
    agents = _agents(_agent("bedrock"), _agent("max", tok_s=None, mn=None, avg=None, mx=None, duty=None), _agent("agy"))
    for w in (41, 62, 100):
        lines = _show({"gpu-host": entry, "_agents": agents}, w)
        assert all(len(ln) <= w for ln in lines), (w, [ln for ln in lines if len(ln) > w])
        assert sum(1 for ln in lines if "tok/s" in ln) == 6, w        # LLM, two seats, three accounts


def test_the_agent_rows_cost_lines_only_where_they_print():
    """The host that carries them counts them; the one that doesn't isn't shortened by them, and a hub that sends no
    "agents" costs the height plan nothing at all."""
    with_seats, plain = _with_llm(_sample()), _sample()
    assert line_count(with_seats) == 5 and line_count(plain) == 2        # line_count itself knows nothing of agents
    rows = [_agent("bedrock"), _agent("max")]
    hosts = {"alpha": plain, "beta": with_seats}                          # alpha sorted first, beta carries the rows
    roomy = _show({**hosts, "_agents": _agents(*rows)}, 62, 40)
    assert sum(1 for ln in roomy if ln.startswith("#")) == 4, roomy       # every line of both hosts fits
    assert sum(1 for ln in roomy if ln.startswith(("bedrock", "max "))) == 2
    assert plan_heights([line_count(plain), line_count(with_seats) + 2], 40)[0] == [2, 7]
    tight = _show({**hosts, "_agents": _agents(*rows)}, 62, 24)           # the two rows cost beta its own tail
    assert sum(1 for ln in tight if ln.startswith("#")) == 2, tight       # alpha still shows both of its cards
    assert not any(ln.startswith(("bedrock", "max ")) for ln in tight) and any(ln.startswith("worker1 ") for ln in tight)


def test_the_help_says_what_the_agent_rows_mean():
    tip = dict(tui.HELP_TIPS)["CPU and GPU"]
    for word in ("bedrock", "agy", "duty", "tok/s", "transcript"):
        assert word in tip, word


def test_vram_temperature_and_power_sit_in_columns_whatever_their_digits():
    """A 3.4 GiB card under a 29.6 GiB one, 9 W under 271 W, 8 °C under 86 °C, a card with a second of history under
    ones with a minute, an unreadable VRAM: every line is as long as the next, so each field starts in one column."""
    gpus = [_gpu(0, used=int(29.6 * GB), total=int(31.9 * GB), temp=86.0, power=271.0),
            _gpu(1, used=int(3.4 * GB), total=int(15.9 * GB), temp=8.0, power=9.0),
            _gpu(2, used=None, total=None, temp=None, power=None),
            _gpu(10, vendor="intel", name="Arc B50", used=int(3.4 * GB), total=int(15.9 * GB), power=22.1)]
    entry = _sample(gpus=gpus)
    entry["history"]["gpus"]["10"] = [0.0]                                 # just came up: one sample of history
    for width in (62, 69, 100):
        lines = [ln for ln in panel_text("gpu-host", entry, width).plain.splitlines() if ln.startswith("#")]
        assert len(lines) == 4 and len({len(ln) for ln in lines}) == 1, (width, lines)
        for mark in ("G", "°C", "W"):
            assert len({ln.rindex(mark) for ln in lines}) == 1, (width, mark, lines)
