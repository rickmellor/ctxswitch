"""The tmux driver: a thin adapter over client/tmux.py, with no logic of its own.

Everything here is one of tmux.py's own calls; the driver only gives them the Runtime shape (rows in, message out)
so the board stops caring which manager it is driving.
"""
from __future__ import annotations

from ctxswitch.client import tmux


class TmuxRuntime:
    """Sessions live in panes of a tmux server (see client/tmux.py for what each call does)."""

    name = "tmux"

    def available(self) -> bool:
        return tmux.in_tmux()

    def reach(self, rows: list[dict], this_host: str, ssh: dict) -> dict[str, str]:
        return tmux.reach_all(rows, this_host, ssh)

    def open(self, row: dict, *, ssh: dict) -> str:
        return tmux.show(row, ssh=ssh)

    def start(self, host: str, harness: str, cwd: str, *, profile: str | None = None,
              profiles: list | None = None, ssh: dict) -> str:
        if tmux._is_remote({"host_id": host}, tmux._this_host()):
            return tmux.new_remote_agent(host, harness, cwd, ssh or {})
        return tmux.new_agent(harness, cwd, profile=profile, profiles=profiles)

    def resume_many(self, rows: list[dict], *, ssh: dict) -> str:
        return tmux.resume_many(rows, ssh=ssh)

    def end(self, row: dict, *, ssh: dict) -> str:
        if tmux._is_remote(row, tmux._this_host()):
            return tmux.end_remote(row, ssh or {})
        p = tmux.agent_pane(row["key"])
        n = tmux.end_agent(p) if p is not None else 0
        return f"ended {tmux._short_title(row)}" if n else "its agent had already exited"

    def visible_keys(self, rows: list[dict]) -> set[str]:
        return tmux.on_screen_keys(sessions=rows)

    def where(self, row: dict) -> str | None:
        """Its pane in this tmux: the tag first, then the pid walk (tmux.pane_for_session)."""
        p = tmux.pane_for_session(row)
        return p.id if p else None
