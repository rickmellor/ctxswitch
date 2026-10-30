"""Parser for shell sessions: a bash started by ``ctx shell`` that records every command it runs.

The transcript (<SHELL_DIR>/<id>.jsonl, see the shell contract) is one ASCII JSON record per line; text that can hold
anything (cwd, command) travels base64-encoded, so no quote, backslash or newline can break a line. One Turn per
``start`` record: the command is user_text (origin "shell"), its ``end`` supplies the exit code and the slice of the
pane's raw output file (<id>.out) recorded between the two ``out`` offsets.

  open    ts, cwd(b64), shell          once, when the shell starts or resumes
  start   n, ts, cwd(b64), cmd(b64), out    before a command runs
  end     n, ts, rc, out               after it (same n)
  close   ts                          when the shell exits

A command bash leaves out of history (a leading space) and an empty command line produce no records at all, so the
``n`` values are not contiguous; a resumed shell may even reuse one, hence an end pairs with the newest unmatched
start of the same n.
"""
from __future__ import annotations

import base64
import binascii
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ctxswitch.common.schema import Turn, trim

TAIL_BYTES = 64 * 1024        # never read more than this much of a command's output
TAIL_CHARS = 2000             # ... and ship at most this many characters of it
TAIL_LINES = 20               # the last so many cleaned lines


def _iso(ts: Any) -> str:
    """Epoch seconds -> ISO with Z, the same shape the other adapters' timestamps have."""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _dur(secs: float) -> str:
    """A wall-clock duration the way a person reads one: 0.4s, 12s, 3m05s, 2h03m."""
    secs = max(0.0, secs)
    if secs < 10:
        return f"{secs:.1f}s"
    whole = int(round(secs))
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        m, s = divmod(whole, 60)
        return f"{m}m{s:02d}s"
    h, r = divmod(whole, 3600)
    return f"{h}h{r // 60:02d}m"


def _int(v: Any, default: int) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else default


def _b64(s: Any) -> str:
    """Decode a record's base64 field; anything malformed reads as empty rather than raising."""
    if not isinstance(s, str) or not s:
        return ""
    try:
        return base64.b64decode(s.encode("ascii", "ignore"), validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError, UnicodeError):
        return ""


_ESC = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"            # CSI: ... <final byte>
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC: ... BEL or ST
    r"|\x1b[@-Z\\-_]"                      # other two-character escapes
    r"|\x1b."                              # a lone ESC: drop it with whatever follows
)


def _clean(raw: bytes) -> str:
    """utf-8 with replacement, escapes removed, \\r overwrites resolved, trailing blank lines dropped."""
    text = raw.decode("utf-8", "replace")
    text = _ESC.sub("", text)
    text = text.replace("\x07", "").replace("\x08", "")
    # a terminal ends lines with \r\n: drop that \r first, then a \r inside the line is an overwrite (progress bars)
    lines = [ln.rstrip("\r").rsplit("\r", 1)[-1] for ln in text.split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def output_tail(path: Path | None, start: int, end: int) -> str:
    """What the command printed: <id>.out[start:end], cleaned, last 20 lines, at most 2000 characters. The slice is
    read only at its end, so a rewritten or truncated file (end < start) simply yields nothing."""
    if path is None or end <= start:
        return ""
    lo, hi = max(start, end - TAIL_BYTES), end
    try:
        with open(path, "rb") as f:
            f.seek(lo)
            raw = f.read(hi - lo)
    except OSError:
        return ""
    if len(raw) < hi - lo:                              # truncated: the offsets no longer line up
        return ""
    tail = _clean(raw).split("\n")[-TAIL_LINES:]
    text = "\n".join(tail)
    return text[-TAIL_CHARS:]


class ShellParser:
    def __init__(self, max_text: int = 4000, out_path: Path | None = None):
        self.max_text = max_text
        self.out_path = Path(out_path) if out_path else None
        self.meta: dict[str, Any] = {"harness_version": "bash", "title": ""}
        self._done: list[Turn] = []
        self._cur: Turn | None = None
        self._n = 0
        self._open: dict[int, tuple[int, float]] = {}   # history number -> (turn idx, start ts): awaiting its end
        self._outstart: dict[int, int] = {}             # turn idx -> .out offset when its command started
        self.bad_lines = 0

    def state(self) -> dict[str, Any]:
        return {"meta": self.meta, "cur": self._cur.to_dict() if self._cur else None, "n": self._n,
                "open": {str(k): list(v) for k, v in self._open.items()},
                "outstart": {str(k): v for k, v in self._outstart.items()}, "bad": self.bad_lines}

    @classmethod
    def from_state(cls, st: dict[str, Any], max_text: int = 4000, out_path: Path | None = None) -> ShellParser:
        p = cls(max_text, out_path)
        p.meta = st["meta"]; p._n = st["n"]; p.bad_lines = st["bad"]
        p._open = {int(k): (v[0], v[1]) for k, v in st.get("open", {}).items()}
        p._outstart = {int(k): v for k, v in st.get("outstart", {}).items()}
        p._cur = Turn.from_dict(st["cur"]) if st["cur"] else None
        return p

    def drain(self) -> list[Turn]:
        out, self._done = self._done, []
        return out

    @property
    def current(self) -> Turn | None:
        return self._cur

    def finish(self) -> None:
        """The session ended without a close: whatever command was still running counts as interrupted."""
        self._interrupt()

    def feed(self, e: dict[str, Any], offset_end: int) -> None:
        t = e.get("t")
        ts = e.get("ts")
        iso = _iso(ts)
        if iso:
            self.meta["last_activity"] = iso
            self.meta.setdefault("started_at", iso)
        if t == "open":
            cwd = _b64(e.get("cwd"))
            if cwd:
                self.meta["cwd"] = cwd
        elif t == "start":
            self._start(e, iso, offset_end)
        elif t == "end":
            self._end(e, iso, offset_end)
        elif t == "close":
            self._interrupt(iso)
        else:
            self.bad_lines += 1

    def _start(self, e: dict[str, Any], iso: str, offset_end: int) -> None:
        n = e.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or not iso:
            self.bad_lines += 1
            return
        cwd = _b64(e.get("cwd"))
        if cwd:
            self.meta["cwd"] = cwd
        # a start while one is open: the earlier command never got its end (the shell was killed and resumed, and a
        # resumed shell counts history again), so it ended as interrupted, not at some point inside this one.
        self._interrupt()
        turn = Turn(idx=self._n, started_at=iso, ended_at=iso, user_text=trim(_b64(e.get("cmd")), self.max_text),
                    user_origin="shell", assistant_text="", offset_end=offset_end)
        self._n += 1
        self._cur = turn
        self._open[n] = (turn.idx, float(e.get("ts") or 0))
        self._outstart[turn.idx] = _int(e.get("out"), 0)

    def _end(self, e: dict[str, Any], iso: str, offset_end: int) -> None:
        n = e.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or n not in self._open:
            self.bad_lines += 1
            return
        idx, start_ts = self._open.pop(n)
        # the start we paired with is the current turn; a start consumed before a collector restart comes back in
        # ``cur`` from the persisted state, so anything else here is a transcript we can't follow.
        if self._cur is None or self._cur.idx != idx:
            self.bad_lines += 1
            return
        cur = self._cur
        rc = _int(e.get("rc"), 0)
        start_off = self._outstart.pop(idx, None)       # where this command's output begins in .out
        tail = "" if start_off is None else output_tail(self.out_path, start_off, _int(e.get("out"), start_off))
        cur.assistant_text = f"exit {rc} · {_dur(max(0.0, float(e.get("ts") or start_ts) - start_ts))}" + \
                             (f"\n{tail}" if tail else "")
        cur.ended_at = iso or cur.ended_at
        cur.complete = True
        cur.offset_end = offset_end               # the transcript's offset: monotonic, the hub's idempotence key
        self._done.append(cur)
        self._cur = None

    def _interrupt(self, iso: str | None = None) -> None:
        if self._cur is None:
            return
        self._outstart.pop(self._cur.idx, None)
        self._cur.assistant_text = "interrupted"
        if iso:                                               # the command ran until the shell exited
            self._cur.ended_at = iso
        self._cur.complete = True
        self._done.append(self._cur)
        self._cur = None
