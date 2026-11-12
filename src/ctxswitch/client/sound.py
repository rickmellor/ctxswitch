"""A short sound for the board's loudest moment: the wrong-window pop-up.

The sound is made here, not shipped: a cartoon "eep!" (two quick rising chirps, the second with a wobble), written
once as a WAV under ~/.local/state/ctxswitch/sounds. Playing it never blocks and never raises.

Where it plays: the board usually runs inside tmux on WSL, which has no sound device of its own, so on WSL the WAV is
played by Windows (powershell.exe's Media.SoundPlayer, the file reached through \\\\wsl.localhost). Elsewhere: afplay
(macOS), then paplay or aplay (Linux). Nothing found: silence.
"""
from __future__ import annotations

import math
import os
import shutil
import struct
import subprocess
import wave
from pathlib import Path

RATE = 22050
SOUNDS = Path.home() / ".local/state/ctxswitch/sounds"
VERSION = 1                                     # bump when the synthesis changes, so a cached WAV is remade
POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


def _chirp(f0: float, f1: float, secs: float, wobble_hz: float = 0.0, wobble: float = 0.0,
           amp: float = 0.55) -> list[float]:
    """A sine sweeping f0→f1 (exponentially, as a voice rises), optionally wobbling, with a quick attack and decay."""
    n = int(RATE * secs)
    out, phase = [], 0.0
    for i in range(n):
        t = i / n
        f = f0 * (f1 / f0) ** t + (wobble * math.sin(2 * math.pi * wobble_hz * i / RATE) if wobble else 0.0)
        phase += 2 * math.pi * f / RATE
        env = min(1.0, i / (0.006 * RATE)) * (1.0 - t) ** 0.6     # 6 ms attack, then a falling tail
        out.append(amp * env * (math.sin(phase) + 0.25 * math.sin(2 * phase)))   # a touch of 2nd harmonic: squeakier
    return out


def eep_samples() -> list[float]:
    """The "eep!": a short rising squeak, a breath, then a longer, higher one with a wobble."""
    gap = [0.0] * int(RATE * 0.03)
    return _chirp(1100, 2400, 0.07) + gap + _chirp(1700, 3300, 0.17, wobble_hz=28, wobble=140)


def write_wav(path: Path, samples: list[float]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767)) for s in samples))
    return path


def sound_file(name: str = "eep", root: Path | None = None) -> Path:
    """The WAV for ``name`` (only "eep" today), made on first use and whenever VERSION moves on."""
    p = (root or SOUNDS) / f"{name}-v{VERSION}.wav"
    if not p.exists():
        write_wav(p, eep_samples())
    return p


def on_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def player(path: Path, wsl: bool | None = None, which=shutil.which, exists=os.path.exists) -> list[str] | None:
    """The command that plays ``path`` here, or None when nothing can."""
    if on_wsl() if wsl is None else wsl:
        ps = which("powershell.exe") or (POWERSHELL if exists(POWERSHELL) else None)
        if ps:
            distro = os.environ.get("WSL_DISTRO_NAME", "")
            win = f"\\\\wsl.localhost\\{distro}" + str(path).replace("/", "\\") if distro else str(path)
            return [ps, "-NoProfile", "-NonInteractive", "-Command",
                    f"(New-Object Media.SoundPlayer '{win}').PlaySync()"]
    for cmd in ("afplay", "paplay", "aplay"):
        exe = which(cmd)
        if exe:
            return [exe, "-q", str(path)] if cmd == "aplay" else [exe, str(path)]
    return None


def play(name: str = "eep", popen=subprocess.Popen) -> bool:
    """Play a sound in the background; True when something was started. Never raises, never waits."""
    if not name or os.environ.get("CTXSWITCH_NO_SOUND"):        # muted (the test suite sets it)
        return False
    try:
        argv = player(sound_file(name))
        if not argv:
            return False
        popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              start_new_session=True)
        return True
    except Exception:                               # noqa: BLE001 - a sound is never worth an error
        return False
