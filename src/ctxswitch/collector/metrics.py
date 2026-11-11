"""CPU and GPU sampling for the board's metrics panel: pure readers, each taking its root, so a test hands it a fake
/proc and /sys tree instead of this machine's.

Read the way btop reads it: ``/proc/stat``'s **delta** between two calls (its counters are cumulative, so one read
says nothing), the k10temp / coretemp hwmon for temperature, the cpufreq knobs for clock, and either ``nvidia-smi``
or the amdgpu sysfs for each card. NVIDIA first, then AMD by DRM card number, re-indexed 0..n-1, so GPU0..GPU7 on
the board read like btop's.

A value that can't be read is null, never a guess: a panel showing 0 °C or 0 % for a sensor it couldn't open is
worse than one showing nothing. A host with no GPUs sends ``"gpus": []``.
"""
from __future__ import annotations

import re
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

NVIDIA_QUERY = ("index", "name", "utilization.gpu", "memory.used", "memory.total", "temperature.gpu", "power.draw")
NVIDIA_TIMEOUT_S = 3                         # a hung nvidia-smi must not hold the sample loop
NA = ("[N/A]", "[Not Supported]", "[Unknown Error]", "N/A")
_CARD = re.compile(r"^card(\d+)$")


def _text(p: Path) -> str:
    try:
        return p.read_text(errors="replace")
    except OSError:                          # unreadable, vanished, a directory (a connector's device/): nothing
        return ""


def _num(p: Path) -> float | None:
    """One sysfs number, or None when the file isn't there or says something else."""
    s = _text(p).strip()
    try:
        return float(s)
    except ValueError:
        return None


def _int(p: Path) -> int | None:
    v = _num(p)
    return int(v) if v is not None else None


def _stat_lines(proc: Path) -> list[tuple[str, list[int]]]:
    """``/proc/stat``'s cpu lines: (label, the counters after it), "cpu" first, then cpuN in file order."""
    out: list[tuple[str, list[int]]] = []
    for line in _text(proc / "stat").splitlines():
        parts = line.split()
        if not parts or not (parts[0] == "cpu" or re.fullmatch(r"cpu\d+", parts[0])):
            continue
        try:
            out.append((parts[0], [int(x) for x in parts[1:]]))
        except ValueError:                   # a truncated line (read mid-write): keep what came before
            continue
    return out


def _pct(prev: list[int], cur: list[int]) -> float:
    """Busy percent over the delta of two cumulative counter lines: everything that isn't idle or iowait."""
    n = min(len(prev), len(cur))
    d = [cur[i] - prev[i] for i in range(n)]
    total = sum(x for x in d if x > 0)
    if total <= 0:
        return 0.0
    idle = sum(max(0, d[i]) for i in (3, 4) if i < len(d))   # user nice system idle iowait: only the last two wait
    return round(max(0.0, min(100.0, (total - idle) / total * 100)), 1)


class CpuSampler:
    """One host's CPU: the "cpu" object of a metrics sample. ``sample()`` is the delta since the last call, so the
    first call of a process says 0 — it has no interval to measure yet, and a made-up number would read as a real one."""

    def __init__(self, proc: str | Path = "/proc", sys: str | Path = "/sys"):
        self.proc, self.sys = Path(proc), Path(sys)
        self._prev: dict[str, list[int]] = {}

    def sample(self) -> dict[str, Any]:
        lines = _stat_lines(self.proc)
        cur = {k: v for k, v in lines}
        pct, cores = 0.0, [0.0] * len([k for k in cur if k != "cpu"])
        if self._prev:
            total = cur.get("cpu") or []
            pct = _pct(self._prev.get("cpu") or [], total) if total else 0.0
            cores = [_pct(self._prev.get(k) or [], v) for k, v in cur.items() if k != "cpu"]
        self._prev = cur
        load = [float(x) for x in _text(self.proc / "loadavg").split()[:3]]
        out = {"model": self.model(), "pct": pct, "cores": cores, "temp_c": self.temp_c(),
               "mhz": self.mhz(), "load": load}
        groups = self.core_groups([k for k in cur if k != "cpu"])
        if groups is not None:
            out["core_of"] = groups
        return out

    def core_groups(self, labels: list[str]) -> list[int] | None:
        """For each logical CPU in ``labels`` (cpuN, /proc/stat order), the index of the physical core it runs on:
        0, 1, … in first-seen order of (package, core_id). The ids are not contiguous (a 3945WX says 0,1,2,4,5,6,…)
        and SMT siblings are not adjacent (cpu0 and cpu12 share a core there), so they are keyed, not counted.
        None when any CPU's topology can't be read — the panel then shows the threads ungrouped."""
        seen: dict[tuple[int, int], int] = {}
        out: list[int] = []
        for label in labels:
            topo = self.sys / "devices" / "system" / "cpu" / label / "topology"
            try:
                key = (int(_text(topo / "physical_package_id").strip()), int(_text(topo / "core_id").strip()))
            except ValueError:
                return None
            out.append(seen.setdefault(key, len(seen)))
        return out

    def model(self) -> str:
        """The first ``model name`` in /proc/cpuinfo ("" when there is none, as on ARM)."""
        for line in _text(self.proc / "cpuinfo").splitlines():
            k, _, v = line.partition(":")
            if k.strip() == "model name":
                return v.strip()
        return ""

    def mhz(self) -> float | None:
        """Current clock: the mean of the cpufreq knobs (kHz), else the mean of /proc/cpuinfo's "cpu MHz". A core
        without a scaling driver drops out of the mean rather than pulling it toward 0."""
        knobs = sorted((self.sys / "devices" / "system" / "cpu").glob("cpu[0-9]*/cpufreq/scaling_cur_freq")) \
            if (self.sys / "devices" / "system" / "cpu").is_dir() else []
        vals = [v / 1000 for v in (_num(p) for p in knobs) if v is not None]
        if not vals:
            vals = [float(v.strip()) for line in _text(self.proc / "cpuinfo").splitlines()
                    for k, _, v in [line.partition(":")] if k.strip() == "cpu MHz" and _is_num(v)]
        return round(sum(vals) / len(vals), 1) if vals else None

    def temp_c(self) -> float | None:
        """Package temperature from whichever hwmon this board has: k10temp's Tctl, or coretemp's Package id 0.
        Anything else (a board with neither, a fan-only sensor) says nothing."""
        hwmons = sorted((self.sys / "class" / "hwmon").glob("hwmon[0-9]*")) if (self.sys / "class" / "hwmon").is_dir() else []
        by_name = {_text(h / "name").strip(): h for h in hwmons}
        k10 = by_name.get("k10temp")
        if k10 is not None:                  # Tctl, the package's own reading (Tdie is the die's, lower)
            v = _num(k10 / "temp1_input")
            return round(v / 1000, 2) if v is not None else None
        core = by_name.get("coretemp")
        if core is not None:
            for label in sorted(core.glob("temp*_label")):
                if _text(label).strip().lower().startswith("package"):
                    v = _num(core / label.name.replace("_label", "_input"))
                    return round(v / 1000, 2) if v is not None else None
        return None


def _is_num(s: str) -> bool:
    try:
        float(s.strip())
        return True
    except ValueError:
        return False


def mem(proc: str | Path = "/proc") -> dict[str, Any]:
    """What's in use: MemTotal minus MemAvailable (what a normal process could get without swapping), in bytes."""
    total = avail = None
    for line in _text(Path(proc) / "meminfo").splitlines():
        k, _, v = line.partition(":")
        cells = v.split()
        if k.strip() in ("MemTotal", "MemAvailable") and cells and _is_num(cells[0]):
            kb = int(float(cells[0]))
            if k.strip() == "MemTotal":
                total = kb * 1024
            else:
                avail = kb * 1024
    used = max(0, total - avail) if total is not None and avail is not None else None
    return {"used": used, "total": total}


# Names for cards whose driver leaves sysfs ``product_name`` empty (amdgpu does on the R9700 and 9070 XT boxes), by
# PCI id. Only a fallback: a non-empty product_name always wins. A key is (vendor, device) or, where one device id
# covers several models, (vendor, device, revision): 1002:7550 is the RX 9070, 9070 XT and 9070 GRE alike, and only
# the revision tells them apart, so it is named per revision and an unlisted revision stays generic.
PCI_NAMES: dict[tuple[int, ...], str] = {
    (0x1002, 0x7551): "AI R9700",              # Navi 48, lspci: Radeon AI PRO R9700 (gpu-host)
    (0x1002, 0x7550, 0xC0): "RX 9070 XT",      # Navi 48 rev c0 (devbox)
}


def _pci_name(dev: Path) -> str:
    """The PCI_NAMES entry for this card: vendor:device:revision first, then vendor:device; "" when neither is listed
    or the ids can't be read. A card whose revision can't be read is matched on vendor:device only."""
    try:
        vendor, device = int(_text(dev / "vendor").strip(), 16), int(_text(dev / "device").strip(), 16)
    except ValueError:
        return ""
    try:
        rev = int(_text(dev / "revision").strip(), 16)
    except ValueError:
        rev = None
    return (PCI_NAMES.get((vendor, device, rev), "") if rev is not None else "") or PCI_NAMES.get((vendor, device), "")


def amd_gpus(sys: str | Path = "/sys") -> list[dict[str, Any]]:
    """Every amdgpu card under /sys/class/drm, by DRM card number. A connector (card0-DP-1) is a display output, not
    a card, and a card with no ``gpu_busy_percent`` isn't amdgpu — both are skipped rather than listed as a GPU."""
    drm = Path(sys) / "class" / "drm"
    cards = [(int(m.group(1)), d) for d in (sorted(drm.iterdir()) if drm.is_dir() else []) if (m := _CARD.match(d.name))]
    out: list[dict[str, Any]] = []
    for _, d in sorted(cards, key=lambda x: x[0]):          # card2 before card10, which a lexical sort gets wrong
        dev = d / "device"
        util = _num(dev / "gpu_busy_percent")
        if util is None:
            continue
        hwmons = sorted((dev / "hwmon").glob("hwmon[0-9]*")) if (dev / "hwmon").is_dir() else []
        temp = next((t for t in (_num(h / "temp1_input") for h in hwmons) if t is not None), None)
        # power lives beside the temperature, in the card's hwmon (power1_average on RDNA, power1_input on some)
        power = next((p for h in hwmons for p in (_num(h / "power1_average"), _num(h / "power1_input")) if p is not None), None)
        name = _text(dev / "product_name").strip() or _pci_name(dev)
        out.append({"index": len(out), "vendor": "amd", "name": name or "AMD GPU", "util": util,
                    "mem_used": _int(dev / "mem_info_vram_used"), "mem_total": _int(dev / "mem_info_vram_total"),
                    "temp_c": round(temp / 1000, 2) if temp is not None else None,
                    "power_w": round(power / 1e6, 1) if power is not None else None})
    return out


def _f(s: str) -> float | None:
    """One nvidia-smi cell: a number, or null for its "[N/A]" / "[Not Supported]"."""
    return None if s.strip() in NA else (float(s) if _is_num(s) else None)


def _mib(s: str) -> int | None:
    """nvidia-smi's memory, which ``nounits`` reports in MiB, in bytes."""
    v = _f(s)
    return None if v is None else int(v * 1024 * 1024)


def nvidia_smi(which=shutil.which, wsl: str = "/usr/lib/wsl/lib/nvidia-smi") -> str:
    """nvidia-smi: on PATH, else WSL's copy, which a systemd --user unit's PATH leaves out (the laptop's RTX)."""
    return which("nvidia-smi") or (wsl if os.path.exists(wsl) else "nvidia-smi")


def nvidia_gpus(run: Callable[..., Any] = subprocess.run) -> list[dict[str, Any]]:
    """Every NVIDIA card, in ``nvidia-smi``'s own order (its index column is dropped; Sampler re-numbers the whole
    list). No nvidia-smi, a non-zero exit or a timeout means no NVIDIA cards here: [] and the AMD ones take GPU0."""
    argv = [nvidia_smi(), "--query-gpu=" + ",".join(NVIDIA_QUERY), "--format=csv,noheader,nounits"]
    try:
        p = run(argv, capture_output=True, text=True, timeout=NVIDIA_TIMEOUT_S)
    except Exception:                        # noqa: BLE001 - not installed, timed out, killed: this host has no NVIDIA
        return []
    if int(getattr(p, "returncode", 0) or 0):
        return []
    out: list[dict[str, Any]] = []
    for line in str(getattr(p, "stdout", "") or "").splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) != len(NVIDIA_QUERY):
            continue
        _, name, util, used, total, temp, power = cells
        out.append({"index": len(out), "vendor": "nvidia", "name": name or "NVIDIA GPU", "util": _f(util),
                    "mem_used": _mib(used), "mem_total": _mib(total), "temp_c": _f(temp), "power_w": _f(power)})
    return out


class Sampler:
    """A whole metrics sample for one host, per the collector→hub contract. Sampling is blocking (sysfs, a subprocess)
    so the collector runs it in a thread; the object itself only remembers its roots."""

    def __init__(self, proc: str | Path = "/proc", sys: str | Path = "/sys", run: Callable[..., Any] = subprocess.run):
        self.cpu, self.sys, self.run = CpuSampler(proc, sys), Path(sys), run

    def sample(self, host_id: str) -> dict[str, Any]:
        gpus = nvidia_gpus(self.run) + amd_gpus(self.sys)
        for i, g in enumerate(gpus):         # one numbering across both vendors, btop's order
            g["index"] = i
        return {"host_id": host_id, "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cpu": self.cpu.sample(), "mem": mem(self.cpu.proc), "gpus": gpus}
