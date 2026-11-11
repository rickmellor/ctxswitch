"""The board's CPU/GPU panel: what the hosts doing the work last said about their silicon, in words.

Pure rendering — ``panel_text`` takes one host's entry straight out of the hub's GET /v1/metrics body (``latest``,
``age_s``, ``history``) and says it in two lines plus one per GPU, nothing wider than the pane, and
``plan_heights`` decides which of several hosts get which of the lines the panel is allowed. The collector and the
hub's side of this are elsewhere; a board on an older hub simply gets an exception and says so.

Unknown values print "—", never a guess. Nothing here assumes a number of GPUs: 10 cards is 10 lines, and the
caller trims what doesn't fit in half the board's height.
"""
from __future__ import annotations

import re

from rich.text import Text
from textual.screen import ModalScreen
from textual.widgets import Static

SPARK = "▁▂▃▄▅▆▇█"                       # one character per sample, 0..100 scaled across the eight
BAR_W = 15                                 # the widest a bar ever gets
GB = 1 << 30                                 # GiB, as btop and nvidia-smi count VRAM and RAM
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
        for p in ("NVIDIA Corporation ", "NVIDIA ", "GeForce ", " Generation", " Laptop GPU"):
            n = n.replace(p, " ").strip()           # "RTX 2000 Ada Generation Laptop GPU" -> "RTX 2000 Ada"
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


CORE_SHADE = "on grey19"                    # behind every other physical core in the thread strip


def _core_strip(threads: list, core_of) -> tuple[Text, str] | None:
    """The thread strip and its core-count label: one character a thread at its own level, grouped by the physical
    core it runs on (``core_of``, from the collector) with every other core shaded. Without a usable ``core_of``
    the threads keep /proc/stat order, unshaded and unlabelled. None when there are no threads."""
    if not threads:
        return None
    glyph = lambda v: SPARK[min(7, max(0, int(round(float(v) * 7 / 100))))]
    if not (isinstance(core_of, list) and len(core_of) == len(threads) and all(isinstance(c, int) for c in core_of)):
        return Text.assemble(*[(glyph(v), level_style(float(v))) for v in threads]), ""
    by_core: dict[int, list] = {}
    for v, c in zip(threads, core_of):
        by_core.setdefault(c, []).append(v)
    t = Text()
    for n, c in enumerate(sorted(by_core)):
        for v in by_core[c]:
            style = level_style(float(v))
            t.append(glyph(v), style=f"{style} {CORE_SHADE}" if n % 2 else style)
    return t, f"{len(by_core)}c"


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


def label_cols(gpus: list, width: int) -> tuple[int, int]:
    """(number width, name width) of the GPU labels — "GPU10", "RTX 4080" — so every bar on a panel starts in one
    column; the CPU label is padded to the same. Several hosts stacked take the max of theirs (hosts_text)."""
    bar_w, nspark = _bar_w(width), _nspark(width)
    room = max(4, width - 34 - bar_w - 5)          # names before history: the sparkline gives ground first
    idx_w = max((len(f"GPU{g.get('index', i)}") for i, g in enumerate(gpus)), default=0)
    name_w = max((len(((gpu_name(g)[:14] or "GPU")[:room].strip() or "GPU")) for g in gpus), default=0)
    return idx_w, name_w


def _bar_w(width: int) -> int:
    return max(6, min(BAR_W, width // 6))        # scaled with the pane: 10 wide at the board's usual 60 columns


def _nspark(width: int) -> int:
    return max(5, min(12, (width - 44) // 2))    # room for the history grows with the pane


def panel_text(host: str, entry: dict, width: int, more: int = 0, cols: tuple[int, int] | None = None) -> Text:
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

    bar_w = _bar_w(width)
    idx_w, name_w = cols or label_cols(gpus, width)     # the CPU's bar and percent sit in the GPUs' columns
    label = "CPU".ljust(idx_w + 1 + name_w if idx_w else 4)
    head = Text.assemble((label, "bold"), " ", _bar(pct, bar_w), " ",
                         (f"{pct:>3.0f}%" if pct is not None else DASH.rjust(4), level_style(pct)))
    cores = [v for v in (cpus.get("cores") or []) if isinstance(v, (int, float))]
    # the thread strip (btop's view of the box) when it fits: a 64-core box in a 44-column pane loses it. It is
    # a snapshot of every thread NOW, not a history: with the topology, a physical core's threads sit side by side
    # and every other core is shaded, so a 12-core/24-thread box reads as 12 segments; "12c" says so when it fits.
    strip = _core_strip(cores, cpus.get("core_of"))
    if strip is not None and len(head.plain) + 1 + len(strip[0].plain) <= width:
        head.append(" ")
        head.append_text(strip[0])
        if strip[1] and len(head.plain) + len(strip[1]) + 1 <= width:
            head.append(" " + strip[1], style="dim")
    lines.append(head[:width])
    # memory on its own row, its bar and percent in the same columns as the CPU's and the cards'
    used, total = mem.get("used"), mem.get("total")
    have = isinstance(used, (int, float)) and isinstance(total, (int, float)) and total
    mpct = float(used) / float(total) * 100 if have else None
    row = Text.assemble(("MEM".ljust(len(label)), "bold"), " ", _bar(mpct, bar_w), " ",
                        (f"{mpct:>3.0f}%" if mpct is not None else DASH.rjust(4), vram_style(mpct) if have else "dim"))
    row.append(f" {float(used) / GB:.0f}/{float(total) / GB:.0f}G" if have else f" {DASH}/{DASH}G", style="dim")
    lines.append(row[:width])

    hist_gpus = hist.get("gpus") or {}
    nspark = _nspark(width)
    shown = gpus[:max(0, len(gpus) - more)]
    for i, g in enumerate(shown):
        idx = g.get("index")
        idx = i if not isinstance(idx, int) else idx
        lines.append(_gpu_line(idx, g, hist_gpus.get(str(idx)) or [], bar_w, nspark, width, name_w, idx_w))
    if more:
        lines.append(Text(f"+{more} more GPUs (widen or close the detail)", "dim")[:width])
    return Text("\n").join(lines)


def _gpu_line(idx: int, g: dict, hist: list, bar_w: int, nspark: int, width: int, name_w: int = 0,
              idx_w: int = 0) -> Text:
    """One card: its name, a util bar and percent, its history, then VRAM, temperature and power. A pane too narrow
    for the lot gives ground from the right — a shorter sparkline first, then power, temperature, VRAM — so a line
    is never wider than the pane and never cuts a name in half."""
    util = g.get("util")
    util = float(util) if isinstance(util, (int, float)) else None
    room = max(4, width - 34 - bar_w - 5)          # names before history: the sparkline gives ground first
    name = ((gpu_name(g)[:14] or "GPU")[:room].strip() or "GPU").ljust(min(name_w, room))
    lead = Text.assemble((f"GPU{idx}".ljust(idx_w), "bold"), " ", (name, ""), " ", _bar(util, bar_w), " ",
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
    """How many of one host's GPUs fit in a panel on a board ``height`` lines tall, and how many that leaves out —
    :func:`plan_heights` for the single-host case, which is what a board that picked no hosts shows."""
    n = gpu_count(entry)
    per, _ = plan_heights([n], height)
    shown = per[0] if per and per[0] is not None else 0
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


def gpu_count(entry: dict) -> int:
    """How many cards a host's last sample named — what the G picker counts after its name."""
    return len((((entry or {}).get("latest") or {}).get("gpus")) or [])


FIXED = 3                                       # lines every shown host keeps: title, CPU, MEM


def plan_heights(counts: list[int], height: int) -> tuple[list[int | None], int]:
    """Which of several stacked hosts get which of the lines a panel on a board ``height`` lines tall may take.

    ``counts`` is one entry per host, in sorted order, holding what :func:`gpu_count` says of it; the answer is
    ``(per, more_hosts)``, one entry per host the same way round: how many of that host's card lines fit, or ``None``
    for a host the panel has no room for at all, plus how many hosts that left out.

    The panel may take no more than half the board and one of those lines is its border. What every host shown keeps
    is its three lines — the title, CPU and memory lines — so the hosts that fit at all are however many of those
    pairs fit, last ones dropped first (``more_hosts``). The card lines left over go out host by host in that same
    sorted order. A host that lost some of its cards ends with a "+N more GPUs" line, which itself costs a line, so
    lines go back from the last host with any until the whole lot, the "+N more" lines included, is inside the
    height. Nothing the answer promises can exceed it.
    """
    counts = [int(c) if isinstance(c, (int, float)) else 0 for c in (counts or [])]
    room = max(0, (height or 0) // 2 - 1)
    for keep in range(min(len(counts), room // FIXED), -1, -1):    # hosts whose title, CPU and MEM fit
        per = [0] * keep
        used = FIXED * keep
        for i in range(keep):                                      # the cards, host by host, sorted
            take = min(counts[i], max(0, room - used))
            per[i], used = take, used + take
        def total() -> int:                                        # every line the panel would hold, "+N more" ones too
            return (FIXED * keep + sum(per) + sum(1 for i in range(keep) if per[i] < counts[i])
                    + (1 if len(counts) > keep else 0))
        while total() > room and any(per):                         # a "+N more GPUs" line costs a line too
            j = max(i for i in range(keep) if per[i])
            per[j] -= 1
        if total() <= room:
            return per + [None] * (len(counts) - keep), len(counts) - keep
    return [None] * len(counts), len(counts)


def hosts_text(hosts: dict, chosen: list[str], width: int, height: int,
               configured: str = "", this_host: str = "") -> Text:
    """The chosen hosts stacked in the panel, sorted: each its own :func:`panel_text` block, and one dim line for a
    host the hub has no sample from — it stopped reporting, or never had ``metrics_every`` set.

    An empty ``chosen`` is the old automatic choice: the one host :func:`pick_host` picks from the hub's body, its
    ``configured`` name and ``this_host`` breaking a tie. The panel may take half the board's ``height`` at most;
    :func:`plan_heights` says how that splits between the hosts, and each one that lost card lines gets the
    ``more`` count that :func:`panel_text` turns into "+N more GPUs".
    """
    hosts = hosts or {}
    if not hosts:
        return Text(METRICS_NONE_MSG, "dim")
    names = sorted(chosen) if chosen else [pick_host(hosts, configured, this_host)]
    counts = [gpu_count(hosts.get(n) or {}) for n in names]
    every = [label_cols(((hosts.get(n) or {}).get("latest") or {}).get("gpus") or [], width) for n in names]
    cols = (max((c[0] for c in every), default=0), max((c[1] for c in every), default=0))   # one column, all hosts     # a host the hub never heard of counts no cards:
    plan, more_hosts = plan_heights(counts, height)             # its one dim line is cheaper than a block of them
    parts = []
    for name, shown, count in zip(names, plan, counts):
        if shown is None:
            continue
        if name not in hosts:                                      # one dim line says the whole host
            parts.append(Text(f"{name} · no samples (is metrics_every set on it?)", "dim")[:width])
            continue
        parts.append(panel_text(name, hosts[name], width, max(0, count - shown), cols))
    if more_hosts:
        parts.append(Text(f"+{more_hosts} more hosts (widen the board, or G to pick fewer)", "dim")[:width])
    return Text("\n").join(parts)


class MetricsPanel(Static):
    """The board's CPU/GPU strip (id "metrics"): hidden until ``g``, then the chosen hosts' last samples, which the
    app puts there with :func:`hosts_text` — or one dim line saying nothing is reporting."""


class HostPicker(ModalScreen):
    """``G``: which hosts the CPU/GPU panel shows, several at once. One key each: a digit toggles that host on or
    off, Enter keeps the set, Escape or ``G`` closes it keeping what was already chosen — closing is never a way of
    losing ticks. Dismisses the chosen names, sorted; never None."""
    DEFAULT_CSS = """
    HostPicker { align: center bottom; background: $background 30%; }
    HostPicker > Static { width: 100%; height: auto; border: round $accent; padding: 0 1; background: $surface; }
    """

    def __init__(self, names: list[str], counts: dict[str, int], chosen: list[str]):
        super().__init__()
        self.names = list(names or [])                        # every host that reported, sorted
        self.counts = dict(counts or {})                      # host -> cards in its last sample
        self.picked: set[str] = {h for h in (chosen or []) if h in self.names}

    def body(self) -> Text:
        t = Text("show which hosts:  ", style="bold")
        if not self.names:
            return t.append("no host has reported CPU/GPU samples: set [collector] metrics_every = 2 on it", "dim")
        for n, name in enumerate(self.names, 1):
            cards = self.counts.get(name) or 0
            t.append(f"{n} [{'x' if name in self.picked else ' '}] {name}", "bold")
            t.append(f"  {cards} GPU{'s' if cards != 1 else ''}" if cards else "  no GPUs", "dim")
            t.append("   ")
        t.append("digit toggles · Enter, Esc or G closes", "dim")
        return t

    def compose(self) -> ComposeResult:
        yield Static(self.body())

    def on_key(self, ev) -> None:
        """Enter, Escape and G all close the picker keeping the set as it stands in it: closing is never a way of
        losing the ticks made in it. A digit toggles one host. Nothing else reaches the board's keys."""
        ev.stop(); ev.prevent_default()
        if ev.key in ("escape", "g", "enter"):
            self.dismiss(sorted(self.picked))
        elif ev.key.isdigit() and 1 <= int(ev.key) <= len(self.names):
            self.picked ^= {self.names[int(ev.key) - 1]}
            self.query_one(Static).update(self.body())


METRICS_NONE_MSG = "no host sends CPU/GPU samples: set [collector] metrics_every = 2 on the hosts to watch"
METRICS_HUB_MSG = "this hub has no CPU/GPU samples (it predates them)"
