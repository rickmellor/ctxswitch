"""``ctx shell`` — a bash that records itself, so plain commands show on the board as harness ``shell``.

The recording is done by ctx.bashrc in this package (bash 4.4+: PS0 marks a command's start, PROMPT_COMMAND its
end); this module hands that file to bash and points it at three files under shells_dir(): ``<id>.jsonl`` the
transcript, ``<id>.out`` the pane's raw output (appended by tmux pipe-pane, so the transcript can carry output by
byte offsets instead of copying it) and ``<id>.history`` the shell's own HISTFILE. Liveness goes to the same
registry pi uses (``<runtime>/ctxswitch/shell-<pid>.json``), so the collector tells busy from idle the same way.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import NoReturn

#: the rcfile bash reads after the user's own ~/.bashrc
RC = Path(__file__).with_name("ctx.bashrc")


def shells_dir() -> Path:
    """Where a shell's transcript, output tail and history live: $CTXSWITCH_SHELLS, else the state dir."""
    if os.environ.get("CTXSWITCH_SHELLS"):
        return Path(os.environ["CTXSWITCH_SHELLS"])
    return Path.home() / ".local" / "state" / "ctxswitch" / "shells"


def run(session_id: str | None = None) -> NoReturn:
    """exec an interactive recording bash for session ``session_id`` (a fresh uuid4 when not given)."""
    from ctxswitch.collector.liveness import runtime_dir

    sid = session_id or str(uuid.uuid4())
    d = shells_dir()
    d.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        os.chmod(d, 0o700)                            # mkdir's mode goes through the umask for parents
    except OSError:
        pass
    log, out, hist = d / f"{sid}.jsonl", d / f"{sid}.out", d / f"{sid}.history"
    reg_dir = runtime_dir() / "ctxswitch"
    reg_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    for f in (log, hist, out):                        # pipe-pane's `cat >>` would create .out world-readable
        try:
            f.touch(mode=0o600, exist_ok=True)
        except OSError:
            pass
    env = dict(os.environ)
    # inside a tmux pane: mirror the pane's output into .out (-o: only this pane's output, not its history).
    # once, here — a resumed shell reuses the same .out, and a second pipe-pane would stack a second cat.
    if os.environ.get("TMUX_PANE") and shutil.which("tmux"):
        subprocess.run(["tmux", "pipe-pane", "-o", "-t", os.environ["TMUX_PANE"], f"cat >> {shlex.quote(str(out))}"],
                       capture_output=True, check=False)
    env.update({"CTX_SHELL_ID": sid, "CTX_SHELL_LOG": str(log), "CTX_SHELL_OUT": str(out),
                "CTX_SHELL_REG_DIR": str(reg_dir), "HISTFILE": str(hist), "CTX_SHELL_HIST": str(hist)})
    os.execvpe("bash", ["bash", "--rcfile", str(RC), "-i"], env)
