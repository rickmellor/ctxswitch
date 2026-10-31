"""Draw the README's diagrams (docs/img/*.svg) as hand-made SVG, in the board's palette.

    .venv/bin/python scripts/diagrams.py

architecture.svg  how it works: collectors → hub → your LLM and every client
row.svg           one row of the board, every part called out
messages.svg      a message's life, from queued to replied

Text is placed per segment (not per character), so a viewer's monospace font that's a little wider or narrower
only shifts a segment, never the callouts. No external fonts: GitHub serves SVGs in a sandbox that can't fetch them.
"""
from __future__ import annotations

from html import escape
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "img"
BG, PANEL, PANEL2, EDGE, TEXT, DIM = "#121212", "#1e1e1e", "#262626", "#3a3a3a", "#e0e0e0", "#8a8a8a"
GREEN, YELLOW, CYAN, BLUE, MAGENTA, RED, ORANGE, PURPLE = ("#98e024", "#ffa62b", "#58d1eb", "#0178d4", "#e45eff",
                                                         "#f92672", "#d97757", "#ae81ff")
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'DejaVu Sans Mono', monospace"


class Svg:
    def __init__(self, w: int, h: int):
        self.w, self.h, self.parts = w, h, []
        self.colors: set[str] = set()

    def add(self, s: str) -> None:
        self.parts.append(s)

    def rect(self, x, y, w, h, fill=PANEL, stroke=EDGE, rx=12, sw=1.5, extra=""):
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}" {extra}/>')

    def text(self, x, y, s, size=14, fill=TEXT, weight=400, anchor="start", mono=False, italic=False):
        fam = MONO if mono else SANS
        style = ' font-style="italic"' if italic else ""
        self.add(f'<text x="{x}" y="{y}" font-family="{fam}" font-size="{size}" font-weight="{weight}" fill="{fill}" '
                 f'text-anchor="{anchor}"{style}>{escape(s)}</text>')

    def spans(self, x, y, segs, size=15, mono=True):
        """Text segments [(text, colour, weight)] in one line."""
        inner = "".join(f'<tspan fill="{c}" font-weight="{w}">{escape(t)}</tspan>' for t, c, w in segs)
        self.add(f'<text x="{x}" y="{y}" font-family="{MONO if mono else SANS}" font-size="{size}" xml:space="preserve">{inner}</text>')

    def arrow(self, d, color, dashed=False, width=2.2, both=False):
        self.colors.add(color)
        m = color.lstrip("#")
        dash = ' stroke-dasharray="7 6"' if dashed else ""
        start = f' marker-start="url(#s{m})"' if both else ""
        self.add(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}"{dash} marker-end="url(#a{m})"{start}/>')

    def pill(self, x, y, w, h, label, color, size=13, mono=False, fill=None):
        self.rect(x, y, w, h, fill=fill or PANEL2, stroke=color, rx=h // 2, sw=1.4)
        self.text(x + w / 2, y + h / 2 + size * 0.36, label, size=size, fill=color, weight=600, anchor="middle", mono=mono)

    def card(self, x, y, w, h, title, color, subtitle=""):
        """A rounded card with a coloured title bar."""
        self.rect(x, y, w, h, fill=PANEL, stroke=EDGE, rx=14, extra='filter="url(#shadow)"')
        self.add(f'<path d="M{x + 14},{y} h{w - 28} a14,14 0 0 1 14,14 v24 h-{w} v-24 a14,14 0 0 1 14,-14 z" fill="{color}" opacity="0.18"/>')
        self.add(f'<rect x="{x}" y="{y + 38}" width="{w}" height="1.2" fill="{color}" opacity="0.55"/>')
        self.text(x + 16, y + 25, title, size=16, fill=color, weight=700)
        if subtitle:
            self.text(x + w - 14, y + 25, subtitle, size=12, fill=DIM, anchor="end")

    def save(self, name: str) -> None:
        markers = "".join(
            f'<marker id="a{c.lstrip("#")}" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>'
            f'<marker id="s{c.lstrip("#")}" viewBox="0 0 10 10" refX="1.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>' for c in sorted(self.colors))
        defs = (f'<defs>{markers}<filter id="shadow" x="-10%" y="-10%" width="120%" height="130%">'
                f'<feDropShadow dx="0" dy="3" stdDeviation="5" flood-color="#000" flood-opacity="0.45"/></filter>'
                f'<linearGradient id="bg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#181818"/>'
                f'<stop offset="1" stop-color="{BG}"/></linearGradient></defs>')
        body = "\n".join(self.parts)
        (OUT / name).write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" width="{self.w}" height="{self.h}">\n'
            f'{defs}<rect width="{self.w}" height="{self.h}" rx="16" fill="url(#bg)"/>\n{body}\n</svg>\n')


def architecture() -> None:
    s = Svg(1180, 660)
    s.text(40, 52, "How ctxswitch works", size=24, weight=700)
    s.text(40, 78, "every machine pushes to one hub; every board, CLI and agent reads the same live state", size=14, fill=DIM)

    # machines
    for i, (host, agents) in enumerate((("laptop", ("Claude Code", "pi")), ("devbox", ("Claude Code", "pi")),
                                        ("gpu-host", ("Claude Code",)))):
        y = 112 + i * 168
        s.card(40, y, 330, 148, host, CYAN, "a machine with agents")
        for j, a in enumerate(agents):
            s.pill(58 + j * 132, y + 54, 122, 28, a, ORANGE if a == "Claude Code" else PURPLE, size=13)
        s.rect(58, y + 94, 294, 38, fill=PANEL2, stroke=GREEN, rx=8, sw=1.4)
        s.text(72, y + 113, "ctx-collector", size=14, fill=GREEN, weight=700, mono=True)
        s.text(72, y + 127, "transcripts · live registry · hook spool", size=11, fill=DIM)
        s.arrow(f"M354,{y + 113} C430,{y + 113} 440,{300} 488,{300}", GREEN)            # push
        s.arrow(f"M488,{338} C430,{338} 440,{y + 124} 356,{y + 124}", YELLOW, dashed=True, width=1.6)   # pulled messages
    s.text(408, 150, "push (outbound only)", size=12, fill=GREEN, weight=600)
    s.text(392, 560, "messages, long-polled by the collector", size=12, fill=YELLOW, weight=600)

    # hub
    s.card(490, 190, 330, 290, "ctx-hub", BLUE, "one, e.g. in docker")
    rows = [("SQLite", "sessions · turns · asks · ledger · messages", TEXT),
            ("REST + WebSocket", ":7433, bearer token, live change feed", TEXT),
            ("attention", "what waits on you, for boards and pop-ups", YELLOW),
            ("workstreams", "the same repo + branch on any host", CYAN),
            ("tracker", "recaps + the ledger, quote-checked", MAGENTA)]
    for k, (name, what, col) in enumerate(rows):
        y = 246 + k * 44
        s.rect(508, y, 294, 36, fill=PANEL2, stroke=EDGE, rx=8, sw=1)
        s.text(520, y + 16, name, size=13, fill=col, weight=700)
        s.text(520, y + 30, what, size=11, fill=DIM)

    # llm
    s.card(870, 112, 270, 130, "your local LLM", MAGENTA)
    s.text(886, 166, "any OpenAI-compatible endpoint", size=12, fill=TEXT)
    s.text(886, 184, "vLLM · LiteLLM · llama.cpp · …", size=12, fill=DIM)
    s.text(886, 202, "json_schema answers, thinking off", size=12, fill=DIM)
    s.text(886, 224, "nothing leaves your network", size=12, fill=GREEN, weight=600)
    s.arrow("M802,440 C850,440 840,177 868,177", MAGENTA)
    s.add(f'<text x="862" y="236" font-family="{MONO}" font-size="11" fill="{MAGENTA}" transform="rotate(-90 862 236)" text-anchor="middle">/chat/completions</text>')

    # clients
    s.card(870, 290, 270, 300, "anywhere you look", GREEN)
    clients = [("ctx tmux", "the board beside your agents", GREEN), ("ctx tui", "the board on its own", GREEN),
               ("ctx ls · show · send", "search · reply · retire", CYAN), ("ctx mcp", "agents read it, message each other", ORANGE),
               ("pop-ups", "a tmux box when something needs you", YELLOW)]
    for k, (name, what, col) in enumerate(clients):
        y = 344 + k * 46
        s.rect(888, y, 234, 38, fill=PANEL2, stroke=EDGE, rx=8, sw=1)
        s.text(900, y + 17, name, size=13, fill=col, weight=700, mono=True)
        s.text(900, y + 31, what, size=11, fill=DIM)
    s.arrow("M804,307 C838,307 836,372 868,372", GREEN, both=True)

    s.rect(40, 616, 1100, 1, fill=EDGE, stroke=EDGE, rx=0, sw=0)
    s.text(40, 640, "Deterministic first: questions, approvals, idle-since and the last prompt are parsed without a model. "
                    "The LLM only adds the recap and the ledger.", size=12.5, fill=DIM)
    s.save("architecture.svg")


def row() -> None:
    s = Svg(1180, 566)
    s.text(40, 52, "Reading a row", size=24, weight=700)
    s.text(40, 78, "one session per row, numbered in the order it joined the list; its pool jobs nest under it", size=14, fill=DIM)
    s.rect(40, 130, 1100, 146, fill="#0d0d0d", stroke=EDGE, rx=10)
    cw = 10.6                                  # nominal column width; each segment is placed at its own x
    segs = [  # (column, text, colour, weight, name, what it says)
        (0, "2", DIM, 400, "number", "Alt+2 opens it, from any pane"),
        (2, "◐", YELLOW, 700, "state", "● busy · ◐ waiting on you · ○ idle · – ended"),
        (5, "3m", DIM, 400, "age", "how long it has been in this state"),
        (9, "◆", TEXT, 700, "reach", "Enter: ▶ it's here · ◆ bring it · ↻ resume · ↗ ssh · ⇢ ⊘ can't"),
        (11, "cc·laptop", DIM, 400, "harness · host", "cc Claude Code · pi · cw Windows Claude"),
        (21, "zephyr-sdk@feat/x", TEXT, 400, "workstream", "repo@branch, matched across hosts"),
        (39, "P2 carve: interface review", YELLOW, 700, "title", "its name, or your last prompt"),
        (67, "◐4m", YELLOW, 700, "open question", "how long it has waited"),
        (72, "+2", CYAN, 700, "unseen", "exchanges since you last looked"),
    ]
    x0, y0 = 62, 190
    for col, t, c, w, _, _ in segs:
        s.spans(x0 + col * cw, y0, [(t, c, w)], size=18)
    s.spans(x0 + 2 * cw, y0 + 30, [("└ approve the plan?", YELLOW, 400)], size=18)
    # a pool job this session launched, nested under it: no number, its brief instead of a title, its own cost
    s.spans(x0 + 2 * cw, y0 + 66, [("↳ ", ORANGE, 400), ("◆ ", TEXT, 700), ("cc·laptop  ", DIM, 400),
                                   ("job1-runtime.md · ok · 72 turns", TEXT, 400), ("  ~$0.03", DIM, 400)], size=18)

    def badge(cx, cy, n, c):
        s.add(f'<circle cx="{cx}" cy="{cy}" r="11" fill="{PANEL2}" stroke="{c}" stroke-width="1.6"/>')
        s.text(cx, cy + 4.5, str(n), size=12, fill=c, weight=700, anchor="middle")

    items = [(col, t, c, name, what) for col, t, c, _, name, what in segs] + [
        (2, "└", YELLOW, "what it's waiting on", "under a waiting row: its question, or the dialog it has up"),
        (2, "↳", ORANGE, "pool job", ("a job this session launched (saint-agent), nested under it with its "
                                      "brief, how it ended and its local cost estimate"))]
    for n, (col, t, c, name, what) in enumerate(items, 1):
        c = TEXT if c == DIM else c
        if n <= len(segs):
            cx = x0 + col * cw + len(t) * cw / 2
            badge(cx, 112, n, c)
            s.add(f'<path d="M{cx},123 L{cx},{y0 - 20}" stroke="{c}" stroke-width="1.2" opacity="0.7"/>')
        else:
            badge(x0 + 2 * cw - 22, y0 + (24 if n == len(segs) + 1 else 60), n, c)
        gx, gy = 40 + ((n - 1) % 2) * 560, 312 + ((n - 1) // 2) * 40
        badge(gx + 12, gy, n, c)
        s.text(gx + 34, gy + 5, name, size=14, fill=c, weight=700)
        s.text(gx + 200, gy + 5, what, size=12.5, fill="#b8b8b8")
    s.text(40, 546, "Numbers stay put while sessions change state; they only close up when a session above leaves the list. "
                    "Hover a row, or press ?, for the same in words.", size=12.5, fill=DIM)
    s.save("row.svg")


def messages() -> None:
    s = Svg(1180, 470)
    s.text(40, 52, "A message's life", size=24, weight=700)
    s.text(40, 78, "m or ctx send types it into the session as a new prompt; the board follows it to the reply",
           size=14, fill=DIM)
    steps = [("…", "queued", YELLOW, "on the hub, until the session's", "host picks it up (now if it's live,", "else when it next runs)"),
             ("→", "sent", YELLOW, "the collector wrote it into the", "session: Claude's inbox socket,", "or pi's extension"),
             ("✓", "delivered", GREEN, "it's in the transcript: the", "agent is working on it", "(exchange #12)"),
             ("↩", "replied", GREEN, "the exchange finished; the", "reply's first lines show", "under MESSAGES")]
    for k, (g, name, col, *lines) in enumerate(steps):
        x = 40 + k * 280
        s.rect(x, 112, 240, 168, fill=PANEL, stroke=col, rx=14, sw=1.6, extra='filter="url(#shadow)"')
        s.text(x + 26, 160, g, size=30, fill=col, weight=700, mono=True)
        s.text(x + 64, 156, name, size=19, fill=col, weight=700)
        for j, ln in enumerate(lines):
            s.text(x + 22, 200 + j * 19, ln, size=12.5, fill="#b8b8b8")
        if k < 3:
            s.arrow(f"M{x + 244},196 L{x + 276},196", col)
    # off-ramps
    s.rect(40, 312, 240, 64, fill=PANEL2, stroke=DIM, rx=12, sw=1.2)
    s.spans(58, 340, [("– ", DIM, 700), ("cancelled", TEXT, 700)], size=16)
    s.text(58, 362, "ctx send --cancel <id> while queued", size=11.5, fill=DIM)
    s.arrow("M160,284 L160,308", DIM, width=1.6)
    s.rect(320, 312, 240, 64, fill=PANEL2, stroke=RED, rx=12, sw=1.2)
    s.spans(338, 340, [("✗ ", RED, 700), ("failed", TEXT, 700)], size=16)
    s.text(338, 362, "socket refused or gone: send again", size=12, fill=DIM)
    s.arrow("M440,284 L440,308", RED, width=1.6)
    # variants
    s.rect(600, 312, 540, 64, fill=PANEL2, stroke=CYAN, rx=12, sw=1.2)
    s.text(618, 337, "to a workstream (W, ctx send --work)", size=13.5, fill=CYAN, weight=700)
    s.text(618, 359, "waits unbound; the first session live on that repo + branch, any host, takes it", size=12, fill=DIM)
    s.rect(600, 388, 540, 64, fill=PANEL2, stroke=ORANGE, rx=12, sw=1.2)
    s.text(618, 413, "from another agent (MCP send_message)", size=13.5, fill=ORANGE, weight=700)
    s.text(618, 435, "headed with its session and host, never \"you\"; one in flight per pair, 20 an hour", size=12, fill=DIM)
    s.rect(40, 388, 520, 64, fill=PANEL2, stroke=GREEN, rx=12, sw=1.2)
    s.text(58, 413, "answering a question (a, ctx reply)", size=13.5, fill=GREEN, weight=700)
    s.text(58, 435, "the same path, linked to the question; the ledger keeps your words", size=12, fill=DIM)
    s.save("messages.svg")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    architecture(); row(); messages()
    for name in ("architecture.svg", "row.svg", "messages.svg"):
        print(f"docs/img/{name}", (OUT / name).stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main()
