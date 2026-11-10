"""The board's CPU/GPU panel: what one host's collector last said about its silicon, in words.

Pure rendering — ``panel_text`` takes one host's entry straight out of the hub's GET /v1/metrics body (``latest``,
``age_s``, ``history``) and says it in two lines plus one per GPU, nothing wider than the pane. The collector and
the hub's side of this are elsewhere; a board on an older hub simply gets an exception and says so.

Unknown values print "—", never a guess. Nothing here assumes a number of GPUs: 10 cards is 10 lines, and the
caller trims what doesn't fit in half the board's height.
"""
from __future__ import annotations

import re

from rich.text import Text
from textual.widgets import Static

SPARK = "▁▂▃▄▅▆▇█"                       # one character per sample, 0..100 scaled across the eight
BAR_W = 15                                 # the widest a bar ever gets
GB = 1_000_000_000
DASH = "—"


def level_style(pct: float | None) -> str:
    """Green under 50, yellow under 80, red from there: bars, their percent, and the sparklines."""
    if pct is None:
        return "dim"
    return "green" if pct < 50 else "yellow" if pct < 80 else "red"


def temp_style(t: float | None) -> str:
    """Temps ride dim until they're worth looking at, yellow at 85, red past that."""
    if t is None:
        return "dim"
    return "dim" if t < 70 else "yellow" if t < 85 else "red"


def vram_style(pct: float | None) -> str:
    if pct is None:
        return ""
    return "red" if pct >= 97 else "yellow" if pct >= 90 else ""


def cpu_model(model: str) -> str:
    """A CPU's marketing name, without the noise: no vendor prefix, no (R)/(TM), no "12-Core Processor" tail."""
    m = re.sub(r"^\s*(?:AMD|Intel)\s*", "", str(model or "").strip())
    m = re.sub(r"\((?:R|TM|C)\)", " ", m)
    m = re.sub(r"[\s-]*\d+[\s-]*(?:Core|Cores|CPU)\s*(?:Processors?)?\s*$", "", m, flags=re.I)
    return re.sub(r"\s+", " ", m).strip()


def gpu_name(g: dict) -> str:
    """Short as it reads on the board: NVIDIA's prefix and GeForce go, AMD's name stays as the collector sent it."""
    n = str(g.get("name") or "").strip()
    if str(g.get("vendor") or "").lower() == "nvidia":
        for p in ("NVIDIA Corporation ", "NVIDIA ", "GeForce "):
            n = n.replace(p, " ").strip()
        n = re.sub(r"\s+", " ", n)
    return n or "GPU"


def _bar(pct: float | None, width: int) -> Text:
    filled = 0 if pct is None else min(width, round(max(0.0, min(100.0, float(pct))) * width / 100))
    return Text("█" * filled + "░" * (width - filled), style=level_style(pct))


def _spark(vals: list, n: int) -> Text:
    """A GPU's own history as one run of characters (no gaps), the last value's level colouring the lot."""
    vals = [v for v in (vals or []) if isinstance(v, (int, float))][-max(0, n):]
    return Text("".join(SPARK[min(7, max(0, int(round(float(v) * 7 / 100))))] for v in vals) or DASH,
                style=level_style(vals[-1] if vals else None))


def _head(parts: list[tuple[str, str]], width: int, warn: tuple[str, str] | None = None) -> Text:
    """The first line, fitted by dropping parts from the right. A stale host keeps saying so: the load, clock and
    temperature go before the warning does, and the warning only goes when there's no room beside the name."""
    t = Text()
    for with_warn, keep in [(True, k) for k in range(len(parts), 0, -1)] + [(False, k) for k in range(len(parts), 0, -1)]:
        shown = [*parts[:keep], warn] if (with_warn and warn) else parts[:keep]
        t = Text()
        for i, (s, style) in enumerate(shown):
            t.append((" · " if i else "") + s, style)
        if len(t.plain) <= width:
            return t
    return t[:width]                                          # a pane narrower than the host's name


def panel_text(host: str, entry: dict, width: int, more: int = 0) -> Text:
    """One host's entry ({"latest", "age_s", "history"}) as text no line of which is wider than ``width``.

    ``more`` is for a panel too short for every card: the caller says how many it left out (what fits_height
    answers), those lines are left off, and a dim "+N more GPUs" line counts them."""
    entry = entry or {}
    latest = entry.get("latest") or {}
    hist = entry.get("history") or {}
    age = entry.get("age_s")
    cpus = latest.get("cpu") or {}
    mem = latest.get("mem") or {}
    gpus = latest.get("gpus") or []
    pct = cpus.get("pct")
    pct = float(pct) if isinstance(pct, (int, float)) else None

    parts: list[tuple[str, str]] = [(str(host or "?"), "bold")]
    model = cpu_model(cpus.get("model") or "")
    if model:
        parts.append((model, "dim"))
    mhz = cpus.get("mhz")
    if isinstance(mhz, (int, float)):
        parts.append((f"{float(mhz) / 1000:.1f} GHz", "dim"))
    temp = cpus.get("temp_c")
    if isinstance(temp, (int, float)):
        parts.append((f"{float(temp):.0f}°C", temp_style(float(temp))))
    load = [v for v in (cpus.get("load") or []) if isinstance(v, (int, float))]
    if load:
        parts.append(("load " + " ".join(f"{float(v):.2f}" for v in load), "dim"))
    warn = (f"stale {int(round(float(age)))}s", "yellow") if isinstance(age, (int, float)) and float(age) > 10 else None
    lines = [_head(parts, width, warn)]

    bar_w = max(6, min(BAR_W, width // 6))     # scaled with the pane: 10 wide at the board's usual 60 columns,
    head = Text.assemble(("CPU ", "bold"), " ", _bar(pct, bar_w), " ",   # where 24 cores share the CPU line with it
                         (f"{pct:>4.0f}%" if pct is not None else f"{DASH:>4}", level_style(pct)))
    cores = [v for v in (cpus.get("cores") or []) if isinstance(v, (int, float))]
    used, total = mem.get("used"), mem.get("total")
    ram = (f"  RAM {int(used) / GB:.0f}/{int(total) / GB:.0f}G"
           if isinstance(used, (int, float)) and isinstance(total, (int, float)) else "")
    # the core strip when the line still holds it with the memory after it: a 64-core box in a 44-column pane loses it
    if cores and len(head.plain) + 1 + len(cores) + len(ram) <= width:
        head.append(" ")
        for v in cores:                                             # one character a core, each its own level
            head.append(SPARK[min(7, max(0, int(round(float(v) * 7 / 100))))], style=level_style(float(v)))
    if ram:
        head.append(ram)
    lines.append(head[:width])

    hist_gpus = hist.get("gpus") or {}
    nspark = max(5, min(12, (width - 44) // 2))             # room for the history grows with the pane
    for i, g in enumerate(gpus[:max(0, len(gpus) - more)]):
        idx = g.get("index")
        idx = i if not isinstance(idx, int) else idx
        lines.append(_gpu_line(idx, g, hist_gpus.get(str(idx)) or [], bar_w, nspark, width))
    if more:
        lines.append(Text(f"+{more} more GPUs (widen or close the detail)", "dim")[:width])
    return Text("\n").join(lines)


def _gpu_line(idx: int, g: dict, hist: list, bar_w: int, nspark: int, width: int) -> Text:
    """One card: its name, a util bar and percent, its history, then VRAM, temperature and power. A pane too narrow
    for the lot gives ground from the right — a shorter sparkline first, then power, temperature, VRAM — so a line
    is never wider than the pane and never cuts a name in half."""
    util = g.get("util")
    util = float(util) if isinstance(util, (int, float)) else None
    name = (gpu_name(g)[:14] or "GPU")[:max(4, width - 34 - bar_w - nspark)].strip() or "GPU"
    lead = Text.assemble((f"GPU{idx}", "bold"), " ", (name, ""), " ", _bar(util, bar_w), " ",
                         (f"{util:>3.0f}%" if util is not None else DASH.rjust(4), level_style(util)))
    gu, gt = g.get("mem_used"), g.get("mem_total")
    have_vram = isinstance(gu, (int, float)) and isinstance(gt, (int, float)) and gt
    vstyle = vram_style(float(gu) / float(gt) * 100) if have_vram else "dim"
    tail = [Text(f" {DASH}/{DASH}G", "dim") if not have_vram else
            Text(f" {float(gu) / GB:.1f}/{float(gt) / GB:.1f}G", style=vstyle),
            Text(f" {DASH}°C", "dim"), Text(f" {DASH}W", "dim")]
    for pos, key, unit in ((1, "temp_c", "°C"), (2, "power_w", "W")):
        v = g.get(key)
        if isinstance(v, (int, float)):
            v = float(v)
            tail[pos] = Text(f" {v:.0f}°C" if unit == "°C" else f" {v:.1f}W",
                             style=temp_style(v) if unit == "°C" else vstyle)
    for drop in range(4):                                   # what goes first is the last thing on the line
        for n in range(nspark, 0, -1):
            t = Text.assemble(lead, " ", _spark(hist, n), *tail[:len(tail) - drop])
            if len(t.plain) <= width:
                return t
    return Text.assemble(lead, *tail)[:width]


def fits_height(entry: dict, height: int) -> tuple[int, int]:
    """How many of ``entry``'s GPUs fit in a panel on a board ``height`` lines tall, and how many that leaves out.
    The panel takes no more than half the board: the two CPU lines come first, then as many cards as are left room
    for, then a "+N more" line when any are out. Nothing here assumes a card count."""
    n = len(((entry or {}).get("latest") or {}).get("gpus") or [])
    room = max(0, (height or 0) // 2 - 1)                    # one of those lines is the panel's border
    if n + 2 <= room:
        return n, 0
    shown = min(n, room - 3) if room >= 3 else 0
    return shown, n - shown


def pick_host(hosts: dict, configured: str, this_host: str) -> str | None:
    """Which host the panel shows: the configured one when it reports; else the one with the most GPUs in its last
    sample (this host wins a tie, otherwise alphabetical); nothing at all when no host has reported."""
    if not hosts:
        return None
    if configured in hosts:
        return configured
    best = lambda h: len((((hosts.get(h) or {}).get("latest") or {}).get("gpus")) or [])  # noqa: E731
    top = max(best(h) for h in hosts)
    if top and this_host in hosts and best(this_host) == top:
        return this_host
    return min((h for h in hosts if best(h) == top), key=lambda h: (0 if h == this_host else 1, h))


class MetricsPanel(Static):
    """The board's CPU/GPU strip (id "metrics"): hidden until ``g``, then one host's last sample, which the app puts
    there with :func:`panel_text` — or one dim line saying the hub has nothing to show."""
