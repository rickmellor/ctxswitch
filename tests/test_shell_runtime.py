"""`ctx shell` against real bash in a private tmux server: the transcript, the output tail and the registry.

Skipped without tmux or without bash 4.4+ (PS0 and EPOCHREALTIME are what the rcfile records with).
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from ctxswitch.collector.liveness import proc_start


def bash_ok() -> bool:
    """PS0 and EPOCHREALTIME, which the rcfile records with, need bash 4.4+."""
    p = shutil.which("bash")
    if not p:
        return False
    out = subprocess.run([p, "-c", "echo $BASH_VERSION"], capture_output=True, text=True).stdout
    v = out.split("(")[0].split(".")
    try:
        return (int(v[0]), int(v[1])) >= (4, 4)
    except (IndexError, ValueError):
        return False


pytestmark = pytest.mark.skipif(shutil.which("tmux") is None or not bash_ok(),
                                reason="needs tmux and bash 4.4+")
SOCK = f"ctxshell{os.getpid()}"
_n = 0
SRC = Path(__file__).resolve().parents[1] / "src"


def tm(*a, check=True):
    r = subprocess.run(["tmux", "-L", SOCK, *a], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(r.stderr)
    return r.stdout.strip()


def b64(s):
    return base64.b64decode(s).decode("utf-8")


def records(path):
    """Every line must be one ASCII JSON object; a broken line is a bug, not a skip."""
    if not path.exists():
        return []
    out = []
    for line in path.read_bytes().split(b"\n"):
        if not line.strip():
            continue
        assert line.decode("ascii"), "record lines are ASCII only"
        out.append(json.loads(line))
    return out


def wait_for(pred, timeout=8.0):
    """Poll instead of sleeping: the shell writes when it's ready, not when we expect it."""
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def pane(tmp_path, monkeypatch):
    """One private tmux server running `ctx shell --id t1` in tmp_path/work, with tmp_path as HOME."""
    global SOCK, _n
    _n += 1
    SOCK = f"ctxshell{os.getpid()}_{_n}"
    sh, work = tmp_path / "shells", tmp_path / "work"
    (work / "sub").mkdir(parents=True)
    # the operator's own setup: PS1 plus a PROMPT_COMMAND of its own, which must survive ours
    (tmp_path / ".bashrc").write_text("PS1='$ '\nPROMPT_COMMAND='echo -n'\nHISTCONTROL=ignoredups\n"
                                      f"HISTFILE={tmp_path}/global_history\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CTXSWITCH_SHELLS", str(sh))
    monkeypatch.setenv("CTXSWITCH_RUNTIME", str(tmp_path / "run"))
    tm("new-session", "-d", "-x", "200", "-y", "50", "-s", "sh",
       f"cd {work} && PYTHONPATH={SRC} {sys.executable} -c 'from ctxswitch.client.cli import main; main()' shell --id t1")
    ns = types.SimpleNamespace(pane=tm("list-panes", "-t", "sh:0.0", "-F", "#{pane_id}").splitlines()[0],
                               sh=sh, log=sh / "t1.jsonl", out=sh / "t1.out", work=work, home=tmp_path,
                               reg=tmp_path / "run" / "ctxswitch")
    assert wait_for(lambda: records(ns.log)), "the shell never wrote its open record"
    yield ns
    tm("kill-server", check=False)


def send(pane, text=""):
    if text:
        tm("send-keys", "-t", pane.pane, "-l", text)
    tm("send-keys", "-t", pane.pane, "Enter")


def reg_files(pane):
    files = sorted(pane.reg.glob("shell-*.json"))
    assert len(files) <= 1, f"more than one registry entry: {files}"
    return files


def reg_file(pane):
    return reg_files(pane)[0] if reg_files(pane) else None


def reg(pane):
    f = reg_file(pane)
    return json.loads(f.read_text()) if f else None


def reg_pid(pane):
    f = reg_file(pane)
    return int(f.stem.split("-")[1]) if f else None


def last(records_, t):
    return next(r for r in reversed(records_) if r["t"] == t)


def test_records_a_command_with_its_output(pane):
    send(pane, "echo hello")
    assert wait_for(lambda: len([r for r in records(pane.log) if r["t"] == "end"]) == 1)
    rs = records(pane.log)
    assert rs[0]["t"] == "open" and rs[0]["v"] == 1 and rs[0]["shell"] == "bash" and b64(rs[0]["cwd"]) == str(pane.work)
    st, en = rs[1], rs[2]
    assert st["t"] == "start" and b64(st["cmd"]) == "echo hello" and b64(st["cwd"]) == str(pane.work)
    assert isinstance(st["n"], int) and st["n"] > 0 and isinstance(st["ts"], float)
    assert en["t"] == "end" and en["n"] == st["n"] and en["rc"] == 0
    assert en["out"] > st["out"], "the output tail grew while the command ran"
    assert b"hello" in pane.out.read_bytes()[st["out"]:en["out"]]
    assert pane.sh.stat().st_mode & 0o777 == 0o700                     # transcripts are private
    for f in (pane.log, pane.out, pane.sh / "t1.history"):
        assert f.stat().st_mode & 0o777 == 0o600, f
    assert reg_file(pane).stat().st_mode & 0o777 == 0o600


def test_end_carries_the_exit_code(pane):
    send(pane, "false")
    assert wait_for(lambda: [r for r in records(pane.log) if r["t"] == "end"])
    assert records(pane.log)[-1]["rc"] == 1


def test_commands_with_quotes_and_backslashes(pane):
    cmd = r"""cd sub && printf 'a\"b\\c\n'"""
    send(pane, cmd)
    assert wait_for(lambda: [r for r in records(pane.log) if r["t"] == "end"])
    rs = records(pane.log)
    st = last([r for r in rs if r["t"] == "start"], "start")
    assert b64(st["cmd"]) == cmd                       # every quote and backslash intact
    send(pane, "echo here")
    assert wait_for(lambda: len([r for r in records(pane.log) if r["t"] == "start"]) == 2)
    assert b64(last([r for r in records(pane.log) if r["t"] == "start"], "start")["cwd"]) == str(pane.work / "sub")


def test_history_skips_are_not_recorded(pane):
    send(pane, "echo one")
    assert wait_for(lambda: len([r for r in records(pane.log) if r["t"] == "end"]) == 1)
    n = len(records(pane.log))
    secret = base64.b64encode(b"echo secret").decode()
    send(pane, " echo secret")                          # HISTCONTROL=ignorespace
    send(pane)                                          # an empty line
    send(pane, "echo two")
    assert wait_for(lambda: len([r for r in records(pane.log) if r["t"] == "end"]) == 2)
    time.sleep(0.4)                                     # nothing may arrive late
    rs = records(pane.log)
    assert len(rs) == n + 2                             # just the start/end of `echo two`
    assert [b64(r["cmd"]) for r in rs if r["t"] == "start"] == ["echo one", "echo two"]
    assert secret not in json.dumps(rs) and "secret" not in pane.log.read_text()


def test_registry_status_and_identity(pane):
    assert wait_for(lambda: reg(pane) is not None)
    pid = reg_pid(pane)
    assert reg(pane)["status"] == "idle"
    send(pane, "sleep 2")
    assert wait_for(lambda: (reg(pane) or {}).get("status") == "busy", 5)
    assert wait_for(lambda: (reg(pane) or {}).get("status") == "idle", 8)
    r = reg(pane)
    assert r["sessionId"] == "t1" and r["pid"] == pid and r["cwd"] == str(pane.work)
    assert r["procStart"] == proc_start(pid) and r["statusSince"].endswith("Z")
    send(pane, "exit")
    assert wait_for(lambda: reg_file(pane) is None)
    assert last(records(pane.log), "close")["ts"] > 0


def test_the_user_prompt_still_sees_the_exit_code(pane):
    send(pane, "false")
    send(pane, "echo rc=$?")
    assert wait_for(lambda: "rc=1" in tm("capture-pane", "-p", "-t", pane.pane)), "the user's $? was clobbered"


def test_the_output_tail_gets_truncated(pane):
    # the real cap is 16 MiB; a cap one prompt's worth below what the next command writes proves the same rule.
    # pipe-pane's cat lands a beat late, so the cut can fall on the following prompt: keep the prompt coming.
    cap = pane.out.stat().st_size + 20
    send(pane, f"export CTX_SHELL_MAX_OUT={cap}")
    send(pane, "printf '%s' " + "a" * 90)
    for _ in range(4):
        send(pane, ":")
    assert wait_for(lambda: pane.out.stat().st_size <= cap), "the capture kept growing past the cap"
    rs = records(pane.log)
    big = [r for r in rs if r["t"] == "end" and r["out"] > cap]
    assert big, "no end record ever saw the capture over the cap"
    assert any(s["out"] < big[-1]["out"] for s in rs if s["t"] == "start" and s["n"] > big[-1]["n"]), \
        "offsets did not restart after the truncation"


def test_a_resumed_shell_adds_to_the_same_transcript(pane):
    send(pane, "echo one")
    assert wait_for(lambda: [r for r in records(pane.log) if r["t"] == "end"])
    send(pane, "exit")
    assert wait_for(lambda: any(r["t"] == "close" for r in records(pane.log)))
    assert wait_for(lambda: reg_file(pane) is None)         # so the new shell's entry is the only one
    tm("new-session", "-d", "-x", "200", "-y", "50", "-s", "again",
       f"cd {pane.work} && PYTHONPATH={SRC} {sys.executable} -c 'from ctxswitch.client.cli import main; main()' shell --id t1")
    assert wait_for(lambda: sum(r["t"] == "open" for r in records(pane.log)) == 2)
    rs = [r["t"] for r in records(pane.log)]
    assert rs == ["open", "start", "end", "close", "open"], rs          # `exit` itself isn't recorded
    p = tm("list-panes", "-t", "again:0.0", "-F", "#{pane_id}").splitlines()[0]
    pane.pane = p
    send(pane, "echo two")
    assert wait_for(lambda: sum(r["t"] == "end" for r in records(pane.log)) == 2)
    assert reg(pane)["sessionId"] == "t1"               # one id, one session, across both shells


def test_exit_closes_the_session(pane):
    send(pane, "exit")
    assert wait_for(lambda: any(r["t"] == "close" for r in records(pane.log)))
    assert wait_for(lambda: reg_file(pane) is None)
    assert not (pane.log.parent / "t1.jsonl.n").exists() and not (pane.log.parent / "t1.jsonl.started").exists()
    assert (pane.sh / "t1.history").exists()


def test_a_bashrc_histfile_and_ignoredups_dont_hide_commands(tmp_path, monkeypatch, pane):
    """~/.bashrc that sets HISTFILE and ignoredups (pane's HOME is tmp_path): this shell keeps its own history file,
    and a repeated command is recorded each time."""
    send(pane, "echo again")
    send(pane, "echo again")
    assert wait_for(lambda: sum(r["t"] == "end" for r in records(pane.log)) == 2)
    assert [b64(r["cmd"]) for r in records(pane.log) if r["t"] == "start"] == ["echo again"] * 2   # both, despite ignoredups
    send(pane, "history -w")
    assert wait_for(lambda: "echo again" in (pane.sh / "t1.history").read_text(errors="replace"))
    assert not (tmp_path / "global_history").exists() or "echo again" not in (tmp_path / "global_history").read_text()
