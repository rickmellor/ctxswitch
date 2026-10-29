"""WSL ↔ Windows: Windows-native Claude Code keeps its transcripts under %USERPROFILE%\\.claude, readable from WSL."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


def is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower()
    except OSError:
        return False


def wsl_path(p: str) -> str:
    r"""C:\Users\me\x -> /mnt/c/Users/me/x; \\wsl.localhost\<distro>\home\me -> /home/me; anything else unchanged."""
    m = re.match(r"^([A-Za-z]):[\\/]?(.*)$", p)
    if m:
        return f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/")
    m = re.match(r"^[\\/]{2}wsl(?:\.localhost|\$)[\\/][^\\/]+[\\/]?(.*)$", p, re.IGNORECASE)
    if m:
        return "/" + m.group(1).replace("\\", "/")
    return p


def windows_home() -> Path | None:
    """%USERPROFILE% as a WSL path, or None off WSL / without interop."""
    cmd = shutil.which("cmd.exe")
    if not cmd or not is_wsl():
        return None
    try:
        r = subprocess.run([cmd, "/c", "echo %USERPROFILE%"], capture_output=True, text=True, timeout=10,
                           cwd="/mnt/c" if Path("/mnt/c").is_dir() else None, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout.strip()
    if not out or "%" in out:
        return None
    home = Path(wsl_path(out))
    return home if home.is_dir() else None


def windows_pids() -> set[int] | None:
    """PIDs of running Windows processes, or None when tasklist.exe can't be run (then liveness is unknown)."""
    exe = shutil.which("tasklist.exe")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "/FO", "CSV", "/NH"], capture_output=True, text=True, errors="replace", timeout=15,
                           cwd="/mnt/c" if Path("/mnt/c").is_dir() else None, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    import csv
    return {int(row[1]) for row in csv.reader(r.stdout.splitlines()) if len(row) > 1 and row[1].isdigit()}
