"""Open a session in a new terminal tab (resume) — same host only in Phase 1; other hosts get the command."""
from __future__ import annotations

import shutil
import socket
import subprocess


def resume_command(s: dict) -> list[str] | None:
    harness, sid = s.get("harness"), s.get("session_id")
    if harness == "claude":
        return ["claude", "--resume", sid]
    if harness == "pi":
        return ["pi", "--session", s.get("meta", {}).get("transcript_path") or s.get("transcript_path") or sid]
    return None


def windows_hint(s: dict) -> str:
    """Windows-native Claude sessions are read-only: say how to resume one, from Windows."""
    return f"Windows session: in a Windows terminal, cd {s.get('cwd') or '~'} && claude --resume {s.get('session_id')}"


def open_tab(s: dict, this_host: str | None = None) -> str:
    """Launch in a new Windows Terminal tab (WSL) or print the command. Returns a human message."""
    if s.get("harness") == "claude-win":
        return windows_hint(s)
    cmd = resume_command(s)
    if not cmd:
        return "no resume command for this harness"
    this_host = this_host or socket.gethostname()
    cwd = s.get("cwd") or "~"
    if s.get("host_id") != this_host:
        return f"on {s.get('host_id')}: cd {cwd} && {' '.join(cmd)}"
    wt = shutil.which("wt.exe")
    if wt:
        title = (s.get("title") or s.get("session_id") or "ctx")[:40]
        subprocess.Popen([wt, "-w", "0", "nt", "--title", title, "wsl.exe", "--cd", cwd, "--", *cmd],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"opened tab: {' '.join(cmd)}"
    return f"run: cd {cwd} && {' '.join(cmd)}"
