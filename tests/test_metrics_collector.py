"""The collector's CPU/GPU sample: what the board's metrics panel shows for this host. Every reader takes its root,
so each test hands it a fake /proc and /sys tree — the numbers below are the real ones off the GPU box."""
import asyncio
import struct
import subprocess

import pytest

from ctxswitch.collector import main as C
from ctxswitch.collector import metrics as M
from ctxswitch.common.config import Config

# nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw -noheader -nounits
NV_OUT = ("0, NVIDIA GeForce RTX 4080, 0, 14591, 16376, 29, 3.54\n"
          "1, NVIDIA GeForce RTX 5080, 0, 15624, 16303, 32, 4.14\n")
# /sys/class/drm/card{0,4}/device on the AMD box: product_name is empty there, so the panel says "AMD GPU"
AMD = dict(util=3, vram_used=30529724416, vram_total=34208743424, temp=31000, power=21000000)


def w(root, name, text):
    """Write a file in the fake tree, making its directories."""
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def stat(total, cores, iow=0):
    """A /proc/stat cpu block: one (user, idle) pair per line, the aggregate first, then the cores in order."""
    lines = [f"cpu  {u} 0 0 {i} {iow} 0 0 0 0 0" for u, i in [total]]
    lines += [f"cpu{n}  {u} 0 0 {i} {iow} 0 0 0 0 0" for n, (u, i) in enumerate(cores)]
    return "\n".join(lines) + "\n"


def cpu_tree(tmp_path, cpuinfo="model name : AMD Ryzen 9 7950X 16-Core Processor\n", cpufreq=None, hwmon=None,
             loadavg="1.25 0.75 0.50 2/345 6789", idle=True):
    """A host with two logical CPUs, both idle in its one /proc/stat snapshot; ``cpufreq`` = kHz per core,
    ``hwmon`` = {name: {file: value}}."""
    proc, sys = tmp_path / "proc", tmp_path / "sys"
    w(proc, "stat", stat((1000, 8000), [(500, 500), (500, 500)]) if idle else stat((1000, 0), [(500, 0), (500, 0)]))
    w(proc, "cpuinfo", cpuinfo)
    w(proc, "loadavg", loadavg)
    for i, khz in enumerate(cpufreq or []):
        w(sys, f"devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq", str(khz))
    for i, (name, files) in enumerate((hwmon or {}).items()):
        w(sys, f"class/hwmon/hwmon{i}/name", name + "\n")
        for f, v in files.items():
            w(sys, f"class/hwmon/hwmon{i}/{f}", str(v))
    return proc, sys


def _two_snapshots(proc, sys, s1, s2):
    """A CpuSampler that sees s1 then s2: the file is rewritten between the two sample() calls."""
    s = M.CpuSampler(proc, sys)
    (proc / "stat").write_text(s1)
    first = s.sample()
    (proc / "stat").write_text(s2)
    return first, s.sample()


# -- the CPU -------------------------------------------------------------------------------------------------------

def test_the_first_sample_has_no_delta_yet_so_says_zero(tmp_path):
    proc, sys = cpu_tree(tmp_path, idle=False)               # counters that show the CPU pegged since boot
    d = M.CpuSampler(proc, sys).sample()
    assert d["pct"] == 0.0 and d["cores"] == [0.0, 0.0]      # ... and still nothing to say about this interval
    assert d["model"] == "AMD Ryzen 9 7950X 16-Core Processor"


def test_a_second_sample_is_the_busy_percent_of_the_time_between_the_two(tmp_path):
    proc, sys = cpu_tree(tmp_path)
    s1, s2 = stat((1000, 8000), [(500, 500), (500, 500)]), stat((1100, 8100), [(600, 500), (500, 600)])
    first, second = _two_snapshots(proc, sys, s1, s2)        # half the host busy; core0 all of it, core1 none
    assert first["pct"] == 0.0 and first["cores"] == [0.0, 0.0]
    assert second["pct"] == 50.0 and second["cores"] == [100.0, 0.0]


def test_iowait_is_not_busy_and_a_core_that_never_moved_is_zero(tmp_path):
    proc, sys = cpu_tree(tmp_path)
    s1, s2 = stat((1000, 8000), [(100, 100), (0, 0)]), stat((1000, 8000), [(100, 100), (0, 0)], iow=200)
    _, second = _two_snapshots(proc, sys, s1, s2)            # 200 ticks waiting on disk: nothing was computed
    assert second["pct"] == 0.0 and second["cores"] == [0.0, 0.0]


def test_the_clock_comes_from_cpufreq_and_else_from_cpuinfo(tmp_path):
    proc, sys = cpu_tree(tmp_path, cpufreq=[4200000, 3800000])
    assert M.CpuSampler(proc, sys).sample()["mhz"] == 4000.0
    proc, sys = cpu_tree(tmp_path / "no-cpufreq", cpuinfo="model name : Intel Core i9\ncpu MHz : 3000.000\ncpu MHz : 1000.000\n")
    assert M.CpuSampler(proc, sys).sample()["mhz"] == 2000.0
    assert M.CpuSampler(*cpu_tree(tmp_path / "bare", cpuinfo="processor : 0\n")).sample()["mhz"] is None


def test_the_temperature_is_k10temp_s_tctl_or_coretemp_s_package(tmp_path):
    proc, sys = cpu_tree(tmp_path, hwmon={"k10temp": {"temp1_input": 45750}})
    assert M.CpuSampler(proc, sys).sample()["temp_c"] == 45.75
    proc, sys = cpu_tree(tmp_path, hwmon={"coretemp": {"temp1_label": "Package id 0", "temp1_input": 52000,
                                                       "temp2_label": "Core 0", "temp2_input": 49000}})
    assert M.CpuSampler(proc, sys).sample()["temp_c"] == 52.0
    proc, sys = cpu_tree(tmp_path, hwmon={"it8728": {"temp1_input": 30000}})   # a board sensor, not the CPU's
    assert M.CpuSampler(proc, sys).sample()["temp_c"] is None
    assert M.CpuSampler(*cpu_tree(tmp_path)).sample()["temp_c"] is None


def test_the_load_average_is_the_three_familiar_numbers(tmp_path):
    proc, sys = cpu_tree(tmp_path)
    assert M.CpuSampler(proc, sys).sample()["load"] == [1.25, 0.75, 0.50]
    assert M.CpuSampler(*cpu_tree(tmp_path, loadavg="")).sample()["load"] == []


def test_memory_is_total_minus_available_in_bytes(tmp_path):
    proc = tmp_path / "proc"
    w(proc, "meminfo", "MemTotal:       32768000 kB\nMemAvailable:   16384000 kB\nBuffers: 1 kB\n")
    assert M.mem(proc) == {"used": 16384000 * 1024, "total": 32768000 * 1024}
    assert M.mem(tmp_path / "nowhere") == {"used": None, "total": None}


# -- the GPUs ------------------------------------------------------------------------------------------------------

def card(tmp_path, n, util=AMD["util"], name="", connector=False, **over):
    v = {**AMD, **over}
    d = tmp_path / "sys" / "class" / "drm" / f"card{n}"
    if connector:
        d = tmp_path / "sys" / "class" / "drm" / f"card{n}-DP-1"      # a display output, not a card
    if util is not False:
        w(d, "device/gpu_busy_percent", str(util))
    for f, val in (("device/mem_info_vram_used", v["vram_used"]), ("device/mem_info_vram_total", v["vram_total"]),
                   ("device/hwmon/hwmon0/temp1_input", v["temp"]), ("device/hwmon/hwmon0/power1_average", v["power"])):
        if val is not None:
            w(d, f, str(val))
    if name:
        w(d, "device/product_name", name)
    return d


def test_amd_cards_are_read_by_drm_number_and_connectors_are_ignored(tmp_path):
    card(tmp_path, 0)
    card(tmp_path, 4, connector=True)                                    # card4-DP-1: a monitor, not a GPU
    card(tmp_path, 10, util=50)                                          # a second card, out of lexical order
    g = M.amd_gpus(tmp_path / "sys")
    assert [x["index"] for x in g] == [0, 1] and [x["name"] for x in g] == ["AMD GPU", "AMD GPU"]
    assert g[0]["vendor"] == "amd" and g[0]["util"] == 3
    assert g[0]["mem_used"] == 30529724416 and g[0]["mem_total"] == 34208743424
    assert g[0]["temp_c"] == 31.0 and g[0]["power_w"] == 21.0


def test_a_card_without_busy_percent_is_not_a_gpu_and_a_missing_file_is_null(tmp_path):
    card(tmp_path, 0, util=False, name="Radeon W7900")                   # nothing says this is a GPU we can read
    card(tmp_path, 2, util=7, name="Radeon W7900", vram_used=None, temp=None, power=None)
    g = M.amd_gpus(tmp_path / "sys")
    assert [x["name"] for x in g] == ["Radeon W7900"] and g[0]["index"] == 0
    assert g[0]["mem_used"] is None and g[0]["temp_c"] is None and g[0]["power_w"] is None
    assert g[0]["util"] == 7 and g[0]["mem_total"] == 34208743424
    assert M.amd_gpus(tmp_path / "nothing") == []


def test_a_card_without_power_average_falls_back_to_its_input(tmp_path):
    card(tmp_path, 0)
    w(tmp_path, "sys/class/drm/card0/device/hwmon/hwmon0/power1_average", "not a number")
    w(tmp_path, "sys/class/drm/card0/device/hwmon/hwmon0/power1_input", "15500000")
    assert M.amd_gpus(tmp_path / "sys")[0]["power_w"] == 15.5


class _P:
    def __init__(self, out, rc=0):
        self.stdout, self.returncode = out, rc


def _run(out=NV_OUT, rc=0, boom=None):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        if boom:
            raise boom
        return _P(out, rc)
    run.calls = calls
    return run


def test_nvidia_smis_two_rows_become_two_gpus_with_the_right_units():
    run = _run()
    g = M.nvidia_gpus(run)
    assert [x["index"] for x in g] == [0, 1] and [x["vendor"] for x in g] == ["nvidia", "nvidia"]
    assert [x["name"] for x in g] == ["NVIDIA GeForce RTX 4080", "NVIDIA GeForce RTX 5080"]
    assert g[0]["util"] == 0.0 and g[0]["temp_c"] == 29.0 and g[0]["power_w"] == 3.54
    assert g[0]["mem_used"] == 14591 * 1024 * 1024 and g[0]["mem_total"] == 16376 * 1024 * 1024
    assert g[1]["mem_used"] == 15624 * 1024 * 1024 and g[1]["power_w"] == 4.14
    argv, kw = run.calls[0]
    assert argv[0].endswith("nvidia-smi") and "utilization.gpu" in argv[1] and kw["timeout"] == 3


def test_a_value_nvidia_smis_cannot_answer_is_null_not_zero():
    g = M.nvidia_gpus(_run("0, NVIDIA GeForce RTX 4090, 55, [N/A], 24576, [N/A], [Not Supported]\n"))
    assert g[0]["mem_used"] is None and g[0]["temp_c"] is None and g[0]["power_w"] is None
    assert g[0]["util"] == 55.0 and g[0]["mem_total"] == 24576 * 1024 * 1024


def test_no_nvidia_smi_is_not_an_error_just_no_nvidia(tmp_path):
    assert M.nvidia_gpus(_run(boom=FileNotFoundError())) == []
    assert M.nvidia_gpus(_run(boom=subprocess.TimeoutExpired("nvidia-smi", 3))) == []
    assert M.nvidia_gpus(_run(rc=1)) == []
    assert M.nvidia_gpus(_run("")) == []


def test_a_sample_lists_nvidia_first_then_amd_and_numbers_them_like_btop(tmp_path):
    proc, sys = cpu_tree(tmp_path, hwmon={"k10temp": {"temp1_input": 45750}})
    card(tmp_path, 0)
    s = M.Sampler(proc, sys, run=_run()).sample("gpu-box")   # one sample: pct 0 by the rule above
    assert s["host_id"] == "gpu-box" and set(s) == {"host_id", "at", "cpu", "mem", "gpus"}
    assert [g["index"] for g in s["gpus"]] == [0, 1, 2]
    assert [g["vendor"] for g in s["gpus"]] == ["nvidia", "nvidia", "amd"]
    assert s["cpu"]["temp_c"] == 45.75 and s["cpu"]["pct"] == 0.0
    assert M.Sampler(proc, tmp_path / "empty-sys", run=_run(out="")).sample("h")["gpus"] == []


# -- the Intel xe cards --------------------------------------------------------------------------------

def xe_card(tmp_path, n, idle=(93_587, 102_710), energy=121_800_692_199, energy_label="card", temp=55_000,
             temp_label="pkg", gts=2):
    """A card the ``xe`` driver drives, laid out like card2 on gpu-host: one idle counter per GT, an energy counter
    labelled ``energy_label`` and a temperature labelled ``temp_label``. The ids are the B50's: 8086:e212 rev 00."""
    d = tmp_path / "sys" / "class" / "drm" / f"card{n}" / "device"
    w(d, "vendor", "0x8086\n"); w(d, "device", "0xe212\n"); w(d, "revision", "0x00\n")
    w(d, "hwmon/hwmon3/name", "xe\n")
    for i in range(gts):
        w(d, f"tile0/gt{i}/gtidle/idle_residency_ms", f"{idle[i]}\n")
    if energy is not None:
        w(d, "hwmon/hwmon3/energy1_input", f"{energy}\n")
        w(d, "hwmon/hwmon3/energy1_label", energy_label + "\n")
    if temp is not None:
        w(d, "hwmon/hwmon3/temp2_input", f"{temp}\n")
        w(d, "hwmon/hwmon3/temp2_label", temp_label + "\n")
    return d


class _Clock:
    """A clock the test advances by hand, so a sample's interval is the test's rather than the suite's."""

    def __init__(self, t=0.0):
        self.t = float(t)

    def __call__(self):
        return self.t


def test_the_first_sample_of_an_xe_card_names_it_and_says_nothing_about_load_or_watts(tmp_path):
    xe_card(tmp_path, 2)
    (g,) = M.IntelSampler(tmp_path / "sys", _Clock(), vram=lambda dev: (None, None)).sample()
    assert g["index"] == 0 and g["vendor"] == "intel" and g["name"] == "Arc B50"
    assert g["util"] is None and g["power_w"] is None          # both are deltas: no interval, nothing to say
    assert g["temp_c"] == 55.0 and g["mem_used"] is None and g["mem_total"] is None


def test_util_is_the_busiest_gt_and_power_the_joules_over_the_interval(tmp_path):
    d, clk = xe_card(tmp_path, 2), _Clock()
    s = M.IntelSampler(tmp_path / "sys", clk, vram=lambda dev: (None, None))
    s.sample()
    clk.t = 2.0
    w(d, "tile0/gt0/gtidle/idle_residency_ms", str(93_587 + 1000) + "\n")   # idled 1000 of 2000 ms: 50 % busy
    w(d, "tile0/gt1/gtidle/idle_residency_ms", str(102_710 + 2000) + "\n")   # idled all of it: 0 % busy
    w(d, "hwmon/hwmon3/energy1_input", str(121_800_692_199 + 43_600_000) + "\n")
    g = s.sample()[0]                                # one sample of the interval: both counters moved once
    assert g["util"] == 50.0 and g["power_w"] == 21.8 and g["temp_c"] == 55.0


def test_an_idle_counter_that_went_backwards_contributes_nothing(tmp_path):
    d, clk = xe_card(tmp_path, 2, idle=(93_587, 102_710)), _Clock(100.0)
    s = M.IntelSampler(tmp_path / "sys", clk, vram=lambda dev: (None, None))
    s.sample()
    clk.t = 102.0
    w(d, "tile0/gt0/gtidle/idle_residency_ms", "80000\n")        # the driver reloaded: that counter went down
    w(d, "tile0/gt1/gtidle/idle_residency_ms", str(102_710 + 1_500) + "\n")   # counts for nothing; 1500 of 2000: 25 %
    assert s.sample()[0]["util"] == 25.0


def test_an_energy_counter_that_went_backwards_says_nothing(tmp_path):
    d, clk = xe_card(tmp_path, 2), _Clock()
    s = M.IntelSampler(tmp_path / "sys", clk, vram=lambda dev: (None, None))
    s.sample()
    clk.t = 2.0
    w(d, "hwmon/hwmon3/energy1_input", str(121_800_692_199 - 43_600_000) + "\n")
    assert s.sample()[0]["power_w"] is None


def test_an_energy_counter_without_a_card_label_is_still_the_card_s(tmp_path):
    d, clk = xe_card(tmp_path, 2, energy_label="pkg"), _Clock()   # no "card" label anywhere: take energy1
    s = M.IntelSampler(tmp_path / "sys", clk, vram=lambda dev: (None, None))
    s.sample()
    clk.t = 2.0
    w(d, "hwmon/hwmon3/energy1_input", str(121_800_692_199 + 20_000_000) + "\n")
    assert s.sample()[0]["power_w"] == 10.0


def test_with_no_card_label_the_lowest_numbered_counter_wins_not_the_smallest_reading(tmp_path):
    d, clk = xe_card(tmp_path, 2, energy_label="pkg"), _Clock()
    w(d, "hwmon/hwmon3/energy2_label", "other\n")
    w(d, "hwmon/hwmon3/energy2_input", "100\n")                  # smaller than energy1, but numbered after it
    s = M.IntelSampler(tmp_path / "sys", clk, vram=lambda dev: (None, None))
    s.sample()
    clk.t = 2.0
    w(d, "hwmon/hwmon3/energy1_input", str(121_800_692_199 + 20_000_000) + "\n")
    w(d, "hwmon/hwmon3/energy2_input", "100\n")
    assert s.sample()[0]["power_w"] == 10.0


def test_a_temperature_labelled_vram_is_not_the_gpu_temperature(tmp_path):
    xe_card(tmp_path, 2, temp_label="vram")                     # no "pkg" label: nothing to say about temperature
    s = M.IntelSampler(tmp_path / "sys", _Clock(), vram=lambda dev: (None, None))
    assert s.sample()[0]["temp_c"] is None


def test_a_card_with_no_gtidle_is_an_intel_card_xe_does_not_drive(tmp_path):
    xe_card(tmp_path, 2, gts=0)                                      # an Intel card on another driver: no GT counters
    assert M.IntelSampler(tmp_path / "sys", _Clock()).sample() == []


def test_the_intel_reader_takes_only_intel_cards_and_the_amd_reader_only_amd(tmp_path):
    card(tmp_path, 0)                                               # the AMD box's card: gpu_busy_percent, no gtidle
    xe_card(tmp_path, 2)
    assert [x["name"] for x in M.amd_gpus(tmp_path / "sys")] == ["AMD GPU"]
    intel = M.IntelSampler(tmp_path / "sys", _Clock(), vram=lambda dev: (None, None))
    assert [x["name"] for x in intel.sample()] == ["Arc B50"]


def test_a_sample_lists_nvidia_then_amd_then_intel_and_numbers_them_like_btop(tmp_path, monkeypatch):
    def no_ioctl(node):                                        # the real one is an ioctl on /dev/dri: not here
        raise OSError("no xe driver")
    monkeypatch.setattr(M, "xe_mem_regions", no_ioctl)
    proc, sys = cpu_tree(tmp_path, hwmon={"k10temp": {"temp1_input": 45750}})
    card(tmp_path, 0)
    xe_card(tmp_path, 2)
    s = M.Sampler(proc, sys, run=_run()).sample("gpu-box")
    assert [g["vendor"] for g in s["gpus"]] == ["nvidia", "nvidia", "amd", "intel"]
    assert [g["index"] for g in s["gpus"]] == [0, 1, 2, 3]
    assert s["gpus"][3]["name"] == "Arc B50" and s["gpus"][3]["util"] is None
    assert s["gpus"][3]["mem_used"] is None and s["gpus"][3]["mem_total"] is None


# -- an xe card's VRAM, from the device-query ioctl ----------------------------------------------------------------

VRAM_TOTAL = 17_093_969_838                                  # 15.92 GiB, the B50's
VRAM_USED = 3_656_090_910                                    # 3.405 GiB of it, as the query on gpu-host answered


def xe_reply(*regions):
    """A ``DRM_XE_DEVICE_QUERY_MEM_REGIONS`` reply: a count, then one 88-byte struct drm_xe_mem_region per region,
    each (mem_class, instance, min_page_size, total_size, used, cpu_visible_size, cpu_visible_used, reserved[6])."""
    out = struct.pack("II", len(regions), 0)
    for cls, total, used in regions:
        out += struct.pack("HHIQQQQ6Q", cls, 0, 4096, total, used, total, used, *([0] * 6))
    return out


def test_the_vram_regions_of_a_query_reply_are_summed(tmp_path):
    raw = xe_reply((0, 16_106_127_360, 8_000_000_000), (1, VRAM_TOTAL, VRAM_USED))   # sysmem beside the card's own
    assert M.parse_mem_regions(raw) == (VRAM_USED, VRAM_TOTAL)
    half = VRAM_TOTAL // 2
    two_tiles = xe_reply((1, half, 1_000), (1, half, 2_000))                         # vram0 + vram1, both counted
    assert M.parse_mem_regions(two_tiles) == (3_000, VRAM_TOTAL)


def test_a_query_that_names_no_vram_or_reports_no_used_says_nothing(tmp_path):
    assert M.parse_mem_regions(xe_reply((0, 16_106_127_360, 8_000_000_000))) == (None, None)   # an iGPU: sysmem only
    # an older kernel fills ``used`` only for a CAP_PERFMON caller: 0 there means unknown, not an empty card
    assert M.parse_mem_regions(xe_reply((1, VRAM_TOTAL, 0))) == (None, VRAM_TOTAL)
    assert M.parse_mem_regions(xe_reply((1, VRAM_TOTAL, VRAM_USED))[:40]) == (None, None)       # truncated reply
    assert M.parse_mem_regions(b"") == (None, None)


def test_vram_is_read_from_the_render_node_and_not_at_all_when_suspended(tmp_path):
    calls = []

    def query(node):
        calls.append(node)
        return xe_reply((1, VRAM_TOTAL, VRAM_USED))

    d = xe_card(tmp_path, 2)
    w(d, "drm/renderD128", "")                                   # sysfs links the render node off the card's dir
    w(d, "drm/conn1", "")                                        # a drm entry that is not a render node
    assert M.xe_vram(d, query) == (VRAM_USED, VRAM_TOTAL)
    assert calls == ["/dev/dri/renderD128"]                      # exactly that node, once

    w(d, "power/runtime_status", "suspended\n")                  # the card is asleep between samples
    calls.clear()
    assert M.xe_vram(d, query) == (None, None) and calls == []   # asking would wake it, so nothing is asked

    calls.clear()
    assert M.xe_vram(tmp_path / "sys/class/drm/card9/device", query) == (None, None) and calls == []


def test_a_card_whose_query_fails_says_nothing_about_its_memory(tmp_path):
    def boom(node):
        raise OSError("render group missing")
    d = xe_card(tmp_path, 2)
    w(d, "drm/renderD128", "")
    assert M.xe_vram(d, boom) == (None, None)
    (g,) = M.IntelSampler(tmp_path / "sys", _Clock(), vram=lambda dev: M.xe_vram(dev, boom)).sample()
    assert g["mem_used"] is None and g["mem_total"] is None


def test_the_intel_card_s_reports_its_vram_as_bytes(tmp_path):
    xe_card(tmp_path, 2)
    (g,) = M.IntelSampler(tmp_path / "sys", _Clock(), vram=lambda dev: (3, 16)).sample()
    assert g["mem_used"] == 3 and g["mem_total"] == 16


# -- the loop that posts it ----------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch):
    monkeypatch.setattr(C, "_metrics_warned", set())


def _collector(tmp_path, every=2.0):
    cfg = Config()
    cfg.collector.host_id, cfg.collector.metrics_every = "gpu-box", every
    cfg.collector.queue_path = str(tmp_path / "q.db")
    return C.Collector(cfg)


async def test_the_metrics_loop_posts_at_once_and_again_after_the_interval(tmp_path, monkeypatch):
    col = _collector(tmp_path)
    slept, posted = [], []

    async def nap(s):
        slept.append(s)
        if len(slept) > 1:
            raise asyncio.CancelledError

    async def post(client, d):
        posted.append(d)

    def read(host_id):                                     # the real sampler blocks; to_thread runs it as it is
        return {"host_id": host_id, "cpu": {"pct": 1.0}}
    monkeypatch.setattr(asyncio, "sleep", nap)
    with pytest.raises(asyncio.CancelledError):
        await col.metrics_loop(sampler=read, post=post)
    assert posted == [{"host_id": "gpu-box", "cpu": {"pct": 1.0}}] * 2 and slept == [2.0, 2.0]
    assert col.urls["metrics"].endswith("/v1/metrics")


async def test_a_post_that_fails_is_said_once_and_the_loop_keeps_going(tmp_path, monkeypatch, caplog):
    col = _collector(tmp_path)
    slept, posted = [], []

    async def nap(s):
        slept.append(s)
        if len(slept) > 2:
            raise asyncio.CancelledError

    async def post(client, d):
        posted.append(d)
        raise OSError("hub down")
    monkeypatch.setattr(asyncio, "sleep", nap)
    with pytest.raises(asyncio.CancelledError):
        await col.metrics_loop(sampler=lambda h: {"host_id": h}, post=post)
    assert len(posted) == 3                                      # one failure is not the end of the loop
    assert [r.getMessage() for r in caplog.records] == ["metrics: OSError: hub down"]


async def test_metrics_every_zero_runs_no_loop_at_all(tmp_path):
    col = _collector(tmp_path, every=0)
    await col.metrics_loop(sampler=lambda h: pytest.fail("sampled"), post=lambda c, d: pytest.fail("posted"))


def test_the_config_names_the_panel_host_and_the_interval(tmp_path):
    cfg = C.load(_cfg(tmp_path, 'metrics_host = "gpu-box"\n[collector]\nmetrics_every = 5.0\n'))
    assert cfg.collector.metrics_every == 5.0 and cfg.metrics_host == "gpu-box"
    assert C.Config().collector.metrics_every == 0.0 and C.Config().metrics_host == ""   # opt-in: off unless configured


def _cfg(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


def test_amd_power_is_read_from_the_card_s_hwmon_not_its_device_dir(tmp_path):
    """The real layout: power1_average sits beside temp1_input in device/hwmon/hwmonN (a file in device/ is ignored)."""
    d = tmp_path / "sys/class/drm/card0/device"; (d / "hwmon/hwmon6").mkdir(parents=True)
    (d / "gpu_busy_percent").write_text("3\n"); (d / "hwmon/hwmon6/temp1_input").write_text("31000\n")
    (d / "hwmon/hwmon6/power1_average").write_text("21000000\n"); (d / "power1_average").write_text("99000000\n")
    (g,) = M.amd_gpus(tmp_path / "sys")
    assert (g["power_w"], g["temp_c"]) == (21.0, 31.0)


def test_nvidia_smi_is_found_in_wsl_s_lib_when_path_leaves_it_out(tmp_path):
    wsl = tmp_path / "nvidia-smi"; wsl.write_text("#!/bin/sh\n"); wsl.chmod(0o755)
    assert M.nvidia_smi(which=lambda c: None, wsl=str(wsl)) == str(wsl)
    assert M.nvidia_smi(which=lambda c: "/usr/bin/nvidia-smi", wsl=str(wsl)) == "/usr/bin/nvidia-smi"
    assert M.nvidia_smi(which=lambda c: None, wsl=str(tmp_path / "absent")) == "nvidia-smi"


# The R9700 box: amdgpu leaves product_name empty, so the name comes from the PCI id (1002:7551, Navi 48).
def test_an_r9700_with_no_product_name_is_named_by_its_pci_id(tmp_path):
    d = card(tmp_path, 0)
    w(d, "device/vendor", "0x1002\n")
    w(d, "device/device", "0x7551\n")
    assert [x["name"] for x in M.amd_gpus(tmp_path / "sys")] == ["AI R9700"]


def test_a_product_name_beats_the_pci_table_and_an_unknown_id_stays_generic(tmp_path):
    named = card(tmp_path, 0, name="Radeon Pro W7900")
    w(named, "device/vendor", "0x1002\n")
    w(named, "device/device", "0x7551\n")
    other = card(tmp_path, 1)
    w(other, "device/vendor", "0x1002\n")
    w(other, "device/device", "0x744c\n")                              # not in the table
    garbled = card(tmp_path, 2)
    w(garbled, "device/vendor", "not-hex\n")
    assert [x["name"] for x in M.amd_gpus(tmp_path / "sys")] == ["Radeon Pro W7900", "AMD GPU", "AMD GPU"]


# -- the physical-core grouping of the thread strip -----------------------------------------------------------------

def test_threads_are_grouped_by_package_and_core_id_in_first_seen_order(tmp_path):
    # the 3945WX shape: core ids skip numbers, and a core's two threads are cpuN and cpuN+half, not neighbours
    proc, sys = cpu_tree(tmp_path)
    w(proc, "stat", stat((1000, 8000), [(500, 500)] * 6))
    for n, core in enumerate([0, 1, 4, 0, 1, 4]):
        w(sys, f"devices/system/cpu/cpu{n}/topology/physical_package_id", "0\n")
        w(sys, f"devices/system/cpu/cpu{n}/topology/core_id", f"{core}\n")
    assert M.CpuSampler(proc, sys).sample()["core_of"] == [0, 1, 2, 0, 1, 2]


def test_two_packages_with_the_same_core_id_are_two_cores(tmp_path):
    proc, sys = cpu_tree(tmp_path)
    for n, pkg in enumerate([0, 1]):
        w(sys, f"devices/system/cpu/cpu{n}/topology/physical_package_id", f"{pkg}\n")
        w(sys, f"devices/system/cpu/cpu{n}/topology/core_id", "0\n")
    assert M.CpuSampler(proc, sys).sample()["core_of"] == [0, 1]


def test_no_topology_means_no_grouping_rather_than_a_guess(tmp_path):
    proc, sys = cpu_tree(tmp_path)                               # cpu_tree writes no topology files
    assert "core_of" not in M.CpuSampler(proc, sys).sample()


# -- the vLLM seats of an LLM host -----------------------------------------------------------------------------------

def vtext(gen=1000.0, running=0.0, waiting=0.0, extra=()):
    """A seat's ``/metrics``: vLLM's own shape — a HELP/TYPE pair per metric, one labelled line per engine and model —
    plus any extra lines a test wants ignored."""
    lines = [
        "# HELP vllm:generation_tokens_total Number of generation tokens processed.",
        "# TYPE vllm:generation_tokens_total counter",
        f'vllm:generation_tokens_total{{engine="0",model_name="m"}} {gen}',
        "# HELP vllm:num_requests_running Number of requests currently running on GPU.",
        "# TYPE vllm:num_requests_running gauge",
        f'vllm:num_requests_running{{engine="0",model_name="m"}} {running}',
        f'vllm:num_requests_waiting{{engine="0",model_name="m"}} {waiting}',
        "# TYPE http_requests_total counter",
        'http_requests_total{handler="/v1/completions"} 12.0',
    ]
    return "\n".join(lines + list(extra)) + "\n"


def _sampler(seats, clock, texts, window_s=600.0):
    """An LlmSampler whose seats answer from a list of texts (the last one for ever), or raise when an entry is an
    exception — a scrape that fails is the only way a seat can be down."""

    def fetch(url):
        t = texts[url].pop(0) if len(texts[url]) > 1 else texts[url][0]
        if isinstance(t, Exception):
            raise t
        return t
    return M.LlmSampler(seats, window_s, clock, fetch)


def test_the_vllm_metrics_a_panel_wants_are_summed_over_label_sets():
    """One vLLM process can serve several engines and models; the tokens it generated are the sum of them all."""
    d = M.parse_vllm(vtext(gen=1000.0, running=2.0, extra=['vllm:generation_tokens_total{engine="1",model_name="m"} 250.0',
                                             'vllm:num_requests_running{engine="1",model_name="m"} 1.0']))
    assert d == {"generation_tokens_total": 1250.0, "num_requests_running": 3.0, "num_requests_waiting": 0.0}
    # a comment and a metric this panel doesn't read never get in, and a line that isn't a number drops out alone
    assert M.parse_vllm(vtext("not-a-number", 0.0, 0.0, ['vllm:num_requests_running{engine="1"} 2.0'])) \
        == {"num_requests_running": 2.0, "num_requests_waiting": 0.0}
    assert M.parse_vllm("# HELP vllm:generation_tokens_total x\n") == {}
    assert M.parse_vllm("vllm:generation_tokens_total 7\n") == {"generation_tokens_total": 7.0}   # no labels at all


def test_a_seat_s_nothing_until_its_second_scrape():
    clk = _Clock()
    s = _sampler({"worker1": "http://127.0.0.1:8002/metrics"}, clk, {"http://127.0.0.1:8002/metrics": [vtext()]})
    d = s.sample()
    assert set(d) == {"window_s", "seats", "total"} and d["window_s"] == 600.0
    (seat,) = d["seats"]
    assert seat["name"] == "worker1" and seat["up"] is True
    assert seat["tok_s"] is None and seat["running"] == 0 and seat["waiting"] == 0  # the gauges read; no interval yet
    assert all(seat[k] is None for k in ("min", "avg", "max", "duty"))
    assert d["total"]["tok_s"] is None and d["total"]["duty"] is None


def test_a_steady_interval_is_the_tokens_over_its_seconds():
    clk = _Clock()
    url = "http://127.0.0.1:8002/metrics"
    s = _sampler({"worker1": url}, clk, {url: [vtext(gen=1000.0, running=2.0), vtext(gen=1400.0, running=2.0)]})
    s.sample()
    clk.t = 2.0
    (seat,) = s.sample()["seats"]
    assert set(seat) == {"name", "up", "tok_s", "running", "waiting", "min", "avg", "max", "duty"}
    assert seat["tok_s"] == 200.0 and seat["running"] == 2 and seat["waiting"] == 0
    assert (seat["min"], seat["avg"], seat["max"], seat["duty"]) == (200.0, 200.0, 200.0, 100.0)


def test_only_an_interval_with_requests_at_both_ends_and_tokens_counts_toward_min_and_avg():
    """One steady interval, then a ramp-down that was still working, then three idle ones: the idle time is half the
    window's duty but none of its throughput, so the seat reads at the speed it actually generated at."""
    clk = _Clock()
    url = "http://127.0.0.1:8002/metrics"
    steps = [vtext(gen=1000.0, running=2.0), vtext(gen=1400.0, running=2.0), vtext(gen=1400.0, running=0.0),
             vtext(gen=1400.0, running=0.0), vtext(gen=1400.0, running=0.0)]
    s = _sampler({"worker1": url}, clk, {url: steps})
    for t in (0.0, 2.0, 4.0, 6.0):
        clk.t = t
        s.sample()
    clk.t = 8.0
    (seat,) = s.sample()["seats"]                             # nothing generated since t=4: idle, not unknown
    assert seat["tok_s"] == 0.0
    assert (seat["min"], seat["avg"], seat["max"]) == (200.0, 200.0, 200.0)
    assert seat["duty"] == 50.0                               # 4 of the 8 s it has watched


def test_a_ramp_up_and_a_token_less_prefill_interval_are_active_but_not_steady():
    clk, url = _Clock(), "http://127.0.0.1:8002/metrics"
    steps = [vtext(gen=0.0, running=0.0), vtext(gen=100.0, running=1.0), vtext(gen=100.0, running=1.0),
             vtext(gen=100.0, running=0.0)]
    s = _sampler({"worker1": url}, clk, {url: steps})
    for t in (0.0, 2.0, 4.0, 6.0):
        clk.t = t
        s.sample()
    clk.t = 8.0
    (seat,) = s.sample()["seats"]
    assert (seat["min"], seat["avg"], seat["max"]) == (None, None, None)   # neither generated while requests ran
    assert seat["duty"] == 75.0                                          # ... but 6 of its 8 s were busy time


def test_the_average_is_weighted_by_time_and_not_by_how_many_intervals_there_were():
    clk, url = _Clock(), "http://127.0.0.1:8002/metrics"
    steps = [vtext(gen=0.0, running=2.0), vtext(gen=200.0, running=2.0), vtext(gen=2000.0, running=2.0)]
    s = _sampler({"worker1": url}, clk, {url: steps})
    for t in (0.0, 2.0, 8.0):
        clk.t = t
        s.sample()
    (seat,) = s.sample()["seats"]
    assert (seat["min"], seat["avg"], seat["max"]) == (100.0, 250.0, 300.0)   # 2 s at 100, 6 s at 300


def test_a_seat_without_the_running_gauge_is_judged_by_its_tokens_and_never_raises():
    """A seat whose /metrics has no num_requests_running (another server, an older vLLM) must not break the sample —
    an exception here would cost the whole host its CPU and GPU sample too. Tokens alone say it was active."""
    clk = _Clock()
    url = "http://127.0.0.1:8002/metrics"
    bare = lambda gen: f"vllm:generation_tokens_total {gen}\n"           # noqa: E731
    s = _sampler({"worker1": url}, clk, {url: [bare(1000.0), bare(1400.0), bare(1400.0)]})
    s.sample()
    clk.t = 2.0
    (seat,) = s.sample()["seats"]
    assert seat["up"] is True and seat["running"] is None and seat["tok_s"] == 200.0
    assert seat["duty"] == 100.0 and seat["min"] is None          # active, but never "steady" without the gauge
    clk.t = 4.0
    (seat,) = s.sample()["seats"]
    assert seat["tok_s"] == 0.0 and seat["duty"] == 50.0


def test_a_seat_that_restarted_says_nothing_for_that_interval_and_measures_from_the_new_counter():
    clk, url = _Clock(), "http://127.0.0.1:8002/metrics"
    steps = [vtext(gen=5000.0, running=2.0), vtext(gen=300.0, running=2.0), vtext(gen=700.0, running=2.0)]
    s = _sampler({"worker1": url}, clk, {url: steps})
    for t in (0.0, 2.0):
        clk.t = t
        assert s.sample()["seats"][0]["tok_s"] is None        # the counter went backwards: no interval, no rate
    clk.t = 4.0
    (seat,) = s.sample()["seats"]
    assert seat["tok_s"] == 200.0 and (seat["min"], seat["avg"], seat["max"]) == (200.0, 200.0, 200.0)


def test_a_seat_that_stops_answering_is_down_and_keeps_the_window_it_had():
    clk, url = _Clock(), "http://127.0.0.1:8002/metrics"
    steps = [vtext(gen=1000.0, running=2.0), vtext(gen=1400.0, running=2.0), OSError("connection refused")]
    s = _sampler({"worker1": url}, clk, {url: steps})
    for t in (0.0, 2.0):
        clk.t = t
        s.sample()
    clk.t = 4.0
    (seat,) = s.sample()["seats"]
    assert seat["up"] is False and seat["tok_s"] is None
    assert seat["running"] is None and seat["waiting"] is None
    assert (seat["min"], seat["avg"], seat["max"], seat["duty"]) == (200.0, 200.0, 200.0, 100.0)
    assert s.sample()["seats"][0]["tok_s"] is None            # it came back: no interval across the gap


def test_a_window_that_aged_out_leaves_the_stats_and_the_duty_behind():
    clk, url = _Clock(), "http://127.0.0.1:8002/metrics"
    steps = [vtext(gen=1000.0, running=2.0), vtext(gen=1400.0, running=2.0), vtext(gen=1400.0, running=0.0)]
    s = _sampler({"worker1": url}, clk, {url: steps}, window_s=10.0)
    for t in (0.0, 2.0, 4.0, 6.0, 8.0):
        clk.t = t
        s.sample()
    clk.t = 20.0
    (seat,) = s.sample()["seats"]                             # everything before t=10 has aged out
    assert (seat["min"], seat["avg"], seat["max"]) == (None, None, None)   # the steady interval is gone from the window
    assert seat["duty"] == 0.0 and seat["tok_s"] == 0.0       # the seat is still watched, and still idle


def test_the_total_is_the_seats_at_once_not_their_numbers_added_up():
    clk, a, b = _Clock(), "http://127.0.0.1:8002/metrics", "http://127.0.0.1:8003/metrics"
    texts = {a: [vtext(gen=1000.0, running=2.0), vtext(gen=1400.0, running=2.0)],
             b: [vtext(gen=0.0, running=0.0), vtext(gen=200.0, running=0.0)]}   # the same 2 s at 100 tok/s
    s = _sampler({"worker1": a, "worker2": b}, clk, texts)
    for t in (0.0, 2.0):
        clk.t = t
        d = s.sample()
    assert [x["name"] for x in d["seats"]] == ["worker1", "worker2"]      # config order
    assert d["seats"][0]["tok_s"] == 200.0 and d["seats"][1]["tok_s"] == 100.0
    assert d["total"]["tok_s"] == 300.0 and (d["total"]["min"], d["total"]["avg"], d["total"]["max"]) == (300.0,) * 3


def test_the_total_duty_is_seat_time_weighted_over_every_seat_it_watched():
    clk, a, b = _Clock(), "http://127.0.0.1:8002/metrics", "http://127.0.0.1:8003/metrics"
    texts = {a: [vtext(gen=1000.0, running=2.0), vtext(gen=1400.0, running=2.0)],
             b: [vtext(gen=0.0, running=0.0), vtext(gen=0.0, running=0.0)]}    # worker2 never woke up
    s = _sampler({"worker1": a, "worker2": b}, clk, texts)
    for t in (0.0, 2.0):
        clk.t = t
        d = s.sample()
    assert d["seats"][1]["duty"] == 0.0 and d["total"]["duty"] == 50.0    # 2 of the 4 seat-seconds
    assert d["total"]["tok_s"] == 200.0                                    # only one seat was generating


def test_a_host_with_no_seats_sends_no_llm_key_at_all(tmp_path):
    proc, sys = cpu_tree(tmp_path)
    card(tmp_path, 0)
    s = M.Sampler(proc, sys, run=_run())
    assert set(s.sample("gpu-box")) == {"host_id", "at", "cpu", "mem", "gpus"}
    assert M.LlmSampler({}).sample() is None                   # an empty table says nothing, so the key stays out
    clk = _Clock()
    s.llm = _sampler({"worker1": "http://127.0.0.1:8002/metrics"}, clk,
                     {"http://127.0.0.1:8002/metrics": [vtext()]})
    d = s.sample("gpu-box")
    assert set(d) == {"host_id", "at", "cpu", "mem", "gpus", "llm"}
    assert set(d["llm"]) == {"window_s", "seats", "total"}


def test_the_seats_are_configured_as_a_table_of_urls(tmp_path):
    cfg = C.load(_cfg(tmp_path, '[collector]\nmetrics_every = 2\n[collector.llm_seats]\n'
                                'worker1 = "http://127.0.0.1:8002/metrics"\n'))
    assert cfg.collector.llm_seats == {"worker1": "http://127.0.0.1:8002/metrics"}
    assert cfg.collector.llm_window_s == 600.0
    assert C.Config().collector.llm_seats == {}                # opt-in, like the interval it rides on


def test_a_device_id_shared_by_several_models_is_named_by_its_revision(tmp_path):
    # 1002:7550 is the RX 9070, 9070 XT and 9070 GRE: only rev c0 (devbox's 9070 XT) is listed
    xt = card(tmp_path, 0)
    w(xt, "device/vendor", "0x1002\n"); w(xt, "device/device", "0x7550\n"); w(xt, "device/revision", "0xc0\n")
    other = card(tmp_path, 1)
    w(other, "device/vendor", "0x1002\n"); w(other, "device/device", "0x7550\n"); w(other, "device/revision", "0xc3\n")
    norev = card(tmp_path, 2)
    w(norev, "device/vendor", "0x1002\n"); w(norev, "device/device", "0x7550\n")
    r9700 = card(tmp_path, 3)                                     # a two-part key matches whatever the revision
    w(r9700, "device/vendor", "0x1002\n"); w(r9700, "device/device", "0x7551\n"); w(r9700, "device/revision", "0xc0\n")
    assert [x["name"] for x in M.amd_gpus(tmp_path / "sys")] == ["RX 9070 XT", "AMD GPU", "AMD GPU", "AI R9700"]


# -- the agent call records a sample rides with --------------------------------------------------------------------

def test_a_sample_carries_the_calls_only_when_the_indexer_has_some(tmp_path):
    proc, sys = cpu_tree(tmp_path)
    done = [{"agent": "bedrock", "end": "2026-11-18T18:28:37.120Z", "dur_s": 16.0, "out": 1427,
             "model": "claude-opus-5-5"}]
    s = M.Sampler(proc, sys, run=_run(), calls=lambda: list(done))
    assert s.sample("gpu-box")["calls"] == done                        # the same records, unchanged
    assert set(M.Sampler(proc, sys, run=_run(), calls=lambda: []).sample("gpu-box")) == \
        {"host_id", "at", "cpu", "mem", "gpus"}                        # nothing completed: no key, not an empty list
    assert "calls" not in M.Sampler(proc, sys, run=_run()).sample("gpu-box")   # no indexer at all, as before
