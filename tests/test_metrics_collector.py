"""The collector's CPU/GPU sample: what the board's metrics panel shows for this host. Every reader takes its root,
so each test hands it a fake /proc and /sys tree — the numbers below are the real ones off the GPU box."""
import asyncio
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
