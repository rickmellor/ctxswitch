"""Open a session in a new terminal tab (resume); a session from another host is resumed there over ssh."""
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


def open_tab(s: dict, this_host: str | None = None, ssh: dict[str, str] | None = None) -> str:
    """Launch in a new Windows Terminal tab (WSL) or print the command. Returns a human message."""
    from ctxswitch.client.tmux import ssh_argv, ssh_target
    if s.get("harness") == "claude-win":
        return windows_hint(s)
    cmd = resume_command(s)
    if not cmd:
        return "no resume command for this harness"
    this_host = this_host or socket.gethostname()
    cwd = s.get("cwd") or "~"
    if s.get("host_id") != this_host:
        target = ssh_target(s.get("host_id"), ssh or {})
        if not target:
            return f"on {s.get('host_id')}: cd {cwd} && {' '.join(cmd)}"
        cmd, cwd = ssh_argv(s, target), "~"
    wt = shutil.which("wt.exe")
    if wt:
        title = (s.get("title") or s.get("session_id") or "ctx")[:40]
        subprocess.Popen([wt, "-w", "0", "nt", "--title", title, "wsl.exe", "--cd", cwd, "--", *cmd],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"opened tab: {' '.join(cmd)}"
    return f"run: cd {cwd} && {' '.join(cmd)}"
