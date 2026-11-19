"""CPU and GPU sampling for the board's metrics panel: pure readers, each taking its root, so a test hands it a fake
/proc and /sys tree instead of this machine's.

Read the way btop reads it: ``/proc/stat``'s **delta** between two calls (its counters are cumulative, so one read
says nothing), the k10temp / coretemp hwmon for temperature, the cpufreq knobs for clock, and either ``nvidia-smi``
or the amdgpu sysfs for each card. An Intel card on the xe driver has no busy percent at all, so it is read the same
way the CPU is: the GT's idle residency and the hwmon's energy counter, both as the delta between two calls, so a
card's first sample says nothing for util and power. Its VRAM comes from no sysfs file but from the xe device-query
ioctl on the card's render node, which an unprivileged user in the ``render`` group can ask; it is null when the card
is runtime-suspended or the query fails. NVIDIA first, then AMD by DRM card number, then Intel by the same number,
re-indexed 0..n-1, so #0..#7 on the board read like btop's.

A value that can't be read is null, never a guess: a panel showing 0 °C or 0 % for a sensor it couldn't open is
worse than one showing nothing. A host with no GPUs sends ``"gpus": []``.
"""
from __future__ import annotations

import ctypes
import fcntl
import re
import os
import shutil
import struct
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

NVIDIA_QUERY = ("index", "name", "utilization.gpu", "memory.used", "memory.total", "temperature.gpu", "power.draw")
NVIDIA_TIMEOUT_S = 3                         # a hung nvidia-smi must not hold the sample loop
NA = ("[N/A]", "[Not Supported]", "[Unknown Error]", "N/A")
_CARD = re.compile(r"^card(\d+)$")
# DRM_IOWR(DRM_COMMAND_BASE + DRM_XE_DEVICE_QUERY, struct drm_xe_device_query) — the one ioctl that asks an xe card
# what memory it has and how much of it is in use. No sysfs file says this.
XE_DEVICE_QUERY = (3 << 30) | (40 << 16) | (ord("d") << 8) | 0x40
XE_QUERY_MEM_REGIONS = 1                     # the query whose reply is the card's memory regions
XE_MEM_CLASS_VRAM = 1                        # 0 is sysmem, which isn't the card's own memory
_XE_REGION_SIZE = 88                         # sizeof(struct drm_xe_mem_region)


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
    (0x8086, 0xE212): "Arc B50",               # Battlemage G21, lspci: Arc Pro B50 (gpu-host)
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


def xe_mem_regions(node: str) -> bytes:
    """The reply of ``DRM_XE_DEVICE_QUERY_MEM_REGIONS`` on a render node, as raw bytes: the query struct's own ``size``
    field is read back first (asking with 0 makes the kernel fill it in), then the reply is fetched into a buffer of
    that size. Needs the ``render`` group, not root. Raises OSError — no such node, no permission, an old driver
    without the query — and the caller says nothing for that card."""
    fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
    try:
        q = bytearray(struct.pack("QIIQQQ", 0, XE_QUERY_MEM_REGIONS, 0, 0, 0, 0))
        fcntl.ioctl(fd, XE_DEVICE_QUERY, q)
        size = struct.unpack_from("QIIQ", q)[2]
        buf = ctypes.create_string_buffer(size)
        q = bytearray(struct.pack("QIIQQQ", 0, XE_QUERY_MEM_REGIONS, size, ctypes.addressof(buf), 0, 0))
        fcntl.ioctl(fd, XE_DEVICE_QUERY, q)
        return buf.raw
    finally:
        os.close(fd)


def parse_mem_regions(raw: bytes) -> tuple[int | None, int | None]:
    """(used, total) bytes over every VRAM region of a ``xe_mem_regions`` reply — summed, because a multi-tile card
    reports vram0 and vram1 separately. (None, None) when it names no VRAM at all (an iGPU, sysmem only) or is shorter
    than its own count says. A VRAM region that reports 0 used says None rather than 0: some kernels only fill ``used``
    for a CAP_PERFMON caller and leave it 0 for everyone else, so 0 is "unknown", not "empty"."""
    try:
        n, = struct.unpack_from("I", raw, 0)
        regions = [struct.unpack_from("HHIQQQQ", raw, 8 + i * _XE_REGION_SIZE) for i in range(n)]
    except (struct.error, IndexError):
        return None, None
    vram = [r for r in regions if r[0] == XE_MEM_CLASS_VRAM]     # r = (class, instance, page size, total, used, …)
    total = sum(r[3] for r in vram)
    if not vram or total <= 0:
        return None, None
    used = sum(r[4] for r in vram)
    return (used if used > 0 else None), total


def xe_vram(dev: Path, query: Callable[[str], bytes] = xe_mem_regions) -> tuple[int | None, int | None]:
    """A card's own VRAM, (used, total) bytes, from the device query on its render node — ``/dev/dri/renderD*`` is
    linked off the card's ``device`` dir. None, None when the card is runtime-suspended: every xe ioctl takes a
    runtime-PM reference, so polling a suspended card would wake it on every sample. Also None when the card has no
    render node yet or the query fails."""
    if _text(dev / "power" / "runtime_status").strip() == "suspended":
        return None, None
    nodes = sorted(d.name for d in (dev / "drm").glob("renderD*")) if (dev / "drm").is_dir() else []
    if not nodes:
        return None, None
    try:
        return parse_mem_regions(query("/dev/dri/" + nodes[0]))
    except OSError:
        return None, None


class IntelSampler:
    """Every Intel card the ``xe`` driver drives, as the ``gpus`` entries. Like CpuSampler it has to be called twice
    before it can say anything about load or watts: xe publishes no busy percent and no power reading, only two
    counters that add up since the card came up, each GT's ``gtidle/idle_residency_ms`` and a hwmon ``energyN_input``.
    So util is the share of the interval a GT was *not* idling, and power the joules it spent over it. The first
    sample of a card says null for both; a card that vanishes between two samples simply drops out of the list. Its
    VRAM is not a delta but a state, read from the xe device-query ioctl on the render node (unprivileged, in the
    ``render`` group), and null when the card is runtime-suspended or the query fails."""

    def __init__(self, sys: str | Path = "/sys", clock: Callable[[], float] = time.monotonic,
                 vram: Callable[[Path], tuple[int | None, int | None]] = xe_vram):
        self.sys, self.clock, self.vram = Path(sys), clock, vram
        self._prev: dict[str, dict[str, Any]] = {}     # by DRM card directory name, "card2"

    def sample(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name, dev in self._cards():
            if _text(dev / "vendor").strip().lower() != "0x8086":
                continue
            idles = {str(g.relative_to(dev)): v for g in sorted(dev.glob("tile*/gt*/gtidle/idle_residency_ms"))
                    if (v := _num(g)) is not None}
            if not idles:                    # no GT idle counters: an Intel card xe does not drive
                continue
            now, prev = self.clock(), self._prev.get(name)
            cur = {"now": now, "idles": idles, "uj": self._energy(dev)}
            self._prev[name] = cur
            used, total = self.vram(dev)
            out.append({"index": len(out), "vendor": "intel",
                "name": _text(dev / "product_name").strip() or _pci_name(dev) or "Intel GPU",
                "util": self._util(prev, cur, now), "mem_used": used, "mem_total": total,
                "temp_c": self._temp(dev), "power_w": self._watts(prev, cur, now)})
        return out

    def _cards(self) -> list[tuple[str, Path]]:
        """Every cardN under /sys/class/drm that is a card rather than a connector, as (directory name, its device/ dir),
        by DRM card number: card2 before card10, which a lexical sort gets wrong."""
        drm = self.sys / "class" / "drm"
        cards = [(int(m.group(1)), d.name, d / "device")
                 for d in (sorted(drm.iterdir()) if drm.is_dir() else []) if (m := _CARD.match(d.name))]
        return [d[1:] for d in sorted(cards, key=lambda x: x[0])]

    @staticmethod
    def _util(prev: dict[str, Any] | None, cur: dict[str, Any], now: float) -> float | None:
        """The busiest GT's share of the interval, from each GT that has a reading on both sides. A GT whose idle
        counter went backwards (a driver reload) drops out, and with nothing left to compare, or no interval at all, the
        card says nothing."""
        d_wall = (now - prev["now"]) * 1000 if prev is not None else 0.0
        if d_wall <= 0:
            return None
        d_idle = [v - prev["idles"][k] for k, v in cur["idles"].items()
                  if k in prev["idles"] and v - prev["idles"][k] >= 0]
        if not d_idle:
            return None
        busy = 100.0 * (1 - min(d_idle) / d_wall)        # the least-idling GT is the busiest
        return round(max(0.0, min(100.0, busy)), 1)

    @staticmethod
    def _energy(dev: Path) -> float | None:
        """The card's own cumulative joules, in microjoules: the ``energyN_input`` whose label says "card", in a hwmon
        named "xe"; failing that, the lowest-numbered counter there that reads as a number."""
        best: tuple[int, float] | None = None
        for h in sorted((dev / "hwmon").glob("hwmon[0-9]*")):
            if _text(h / "name").strip() != "xe":
                continue
            for label in sorted(h.glob("energy*_label")):
                v = _num(h / label.name.replace("_label", "_input"))
                if v is None:
                    continue
                if _text(label).strip().lower() == "card":
                    return v
                m = re.fullmatch(r"energy(\d+)_label", label.name)
                n = int(m.group(1)) if m else 1 << 30
                best = min(best, (n, v)) if best is not None else (n, v)
        return best[1] if best is not None else None

    @staticmethod
    def _watts(prev: dict[str, Any] | None, cur: dict[str, Any], now: float) -> float | None:
        """Watts: the joules of the interval over its seconds. Nothing on a card's first sample, or when the counter
        went backwards (a driver reload resets it)."""
        if prev is None or cur["uj"] is None or prev.get("uj") is None:
            return None
        d_s, d_uj = now - prev["now"], cur["uj"] - prev["uj"]
        return round(d_uj / 1e6 / d_s, 1) if d_s > 0 and d_uj >= 0 else None

    @staticmethod
    def _temp(dev: Path) -> float | None:
        """The package's own sensor, by its label. A vram or board sensor is not this panel's GPU temperature, so with no
        "pkg" label the card says nothing."""
        for h in sorted((dev / "hwmon").glob("hwmon[0-9]*")):
            for label in sorted(h.glob("temp*_label")):
                if _text(label).strip().lower() == "pkg":
                    v = _num(h / label.name.replace("_label", "_input"))
                    return round(v / 1000, 2) if v is not None else None
        return None


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
        self.intel = IntelSampler(sys)

    def sample(self, host_id: str) -> dict[str, Any]:
        gpus = nvidia_gpus(self.run) + amd_gpus(self.sys) + self.intel.sample()
        for i, g in enumerate(gpus):         # one numbering across the vendors, btop's order
            g["index"] = i
        return {"host_id": host_id, "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cpu": self.cpu.sample(), "mem": mem(self.cpu.proc), "gpus": gpus}
