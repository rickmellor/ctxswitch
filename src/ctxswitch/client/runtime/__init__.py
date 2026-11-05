"""One runtime interface, two drivers: tmux and herdr-core (hc).

The board asks a `Runtime` what it can do with a row and does it, never which manager the host runs. `get_runtime`
picks the driver: tmux when this process is inside tmux, otherwise hc when hc is installed and answering, explicit
name always wins. A row is the same session dict the board already carries (`Session.to_dict` plus the liveness
fields), so both drivers read the same fields: ``key``, ``host_id``, ``harness``, ``session_id``, ``cwd``, ``pid``,
``live_status``, ``tmux_at``.

Reach is the shared vocabulary — the words `client.tmux.REACH_GLYPH` already knows (pane / resume / foreign / ssh /
attach / remote / none) — so a row says the same thing about itself whichever manager is underneath.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

REACH = ("pane", "resume", "foreign", "ssh", "attach", "remote", "none")


@runtime_checkable
class Runtime(Protocol):
    """What the board needs from whatever keeps a session's terminal alive."""

    name: str

    def available(self) -> bool:
        """Whether this manager can be used from here at all."""
        ...

    def reach(self, rows: list[dict], this_host: str, ssh: dict) -> dict[str, str]:
        """key -> how Enter can reach each row: "pane" | "resume" | "foreign" | "ssh" | "attach" | "remote" | "none"."""
        ...

    def open(self, row: dict, *, ssh: dict) -> str:
        """Bring it to the front / attach / resume it. Returns the board's one-line message."""
        ...

    def start(self, host: str, harness: str, cwd: str, *, profile: str | None = None,
              profiles: list | None = None, ssh: dict) -> str:
        """A fresh session of ``harness`` on ``host``, started so it stays alive. Returns its message."""
        ...

    def resume_many(self, rows: list[dict], *, ssh: dict) -> str:
        """Resume each marked row, one message for all of them."""
        ...

    def end(self, row: dict, *, ssh: dict) -> str:
        """End its program; its transcript stays, so the row comes back as resumable."""
        ...

    def visible_keys(self, rows: list[dict]) -> set[str]:
        """Keys of the rows on a screen someone can look at right now."""
        ...

    def where(self, row: dict) -> str | None:
        """The runtime's own address for a row: a tmux pane id, an hc terminal address."""
        ...


def _tmux_default() -> bool:
    from ctxswitch.client import tmux
    return tmux.in_tmux()


def get_runtime(name: str | None = None) -> Runtime:
    """The driver to use: an explicit "tmux" or "hc" always wins; otherwise tmux inside tmux, else hc when it's
    there. Unknown names raise ValueError rather than quietly managing the wrong thing."""
    chosen = (name or "").strip().lower() or None
    if chosen is None:
        if _tmux_default():
            chosen = "tmux"
        else:
            from ctxswitch.client.runtime.hc import HcRuntime
            chosen = "hc" if HcRuntime().available() else "tmux"
    if chosen == "tmux":
        from ctxswitch.client.runtime.tmux import TmuxRuntime
        return TmuxRuntime()
    if chosen == "hc":
        from ctxswitch.client.runtime.hc import HcRuntime
        return HcRuntime()
    raise ValueError(f"unknown runtime {name!r} (expected 'tmux' or 'hc')")


def this_host() -> str:
    """This machine's host id, named the way the tmux driver names it."""
    from ctxswitch.client.tmux import _this_host
    return _this_host()


__all__ = ["Runtime", "get_runtime", "REACH", "this_host"]
