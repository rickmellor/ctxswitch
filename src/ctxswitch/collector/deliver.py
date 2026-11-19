"""Write an operator message into a live session: through its own socket, or into Antigravity's pane or inbox.

Claude and pi take the same newline-delimited JSON line (spikes/SPIKES.md (a)):
  {"type": "user", "message": {"content": <text>}, "msg_id": "ctx-<id>", "session_id": <target session id>}
Claude: the inbox socket from its registry (``messagingSocketPath``). It answers nothing; the receiver checks
  ``session_id`` and drops a line meant for another session. It arrives as a peer message, held for approval when
  the session is in plan or bypass mode.
Another agent's message carries agent_header (sender's key, title and host) instead, for both harnesses.
pi: the socket the ctxswitch extension opens (pi_ext/ctxswitch.ts). It answers {"ok": true} or {"ok": false,
  "error": ...} after handing the text to pi.sendUserMessage (queued as a follow-up while pi is busy).
agy has no socket (deliver_agy): typed into its tmux pane while it sits at its prompt, else written to the inbox
  its hooks read (``~/.local/state/ctxswitch/agy-inbox/<conversation id>.jsonl``), which reaches it mid-turn.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from ctxswitch.common.schema import Liveness

log = logging.getLogger("ctxswitch.collector.deliver")

TIMEOUT = 5.0
TMUX_TIMEOUT = 3.0                         # a tmux call that hangs must not hold the inbox loop
PASTE_SETTLE_S = 1.0                       # let a multi-line paste land in the input box before Enter
HEADER = "Message from the operator (sent with ctxswitch):\n\n"


# Claude's own SendMessage only reaches sessions on its host, and a haiku session given "the send_message tool"
# picked it (2026-10-29), so Claude gets the MCP tool's full name
REPLY_TOOL = {"claude": "mcp__ctxswitch__send_message tool (ctxswitch MCP; not SendMessage)",
              "agy": "send_message tool of the ctxswitch MCP server"}


def agent_header(from_key: str, title: str | None = None, harness: str = "pi") -> str:
    """Heads another agent's message, for either harness (pi would otherwise show it as the operator's prompt)."""
    host = from_key.split("/", 1)[0]
    name = f' "{" ".join(title.split())[:80]}"' if title and title.strip() else ""
    tool = REPLY_TOOL.get(harness, "ctxswitch MCP server's send_message tool")
    return (f"Message from another agent session, not from the operator (sent with ctxswitch).\n"
            f"From: session {from_key}{name} on host {host}\n"
            f"To reply, use the {tool} with session \"{from_key}\".\n\n")


def message_text(body: str, harness: str, from_key: str | None = None, from_title: str | None = None) -> str:
    """What the session is shown: another agent's header, or the operator's where the session would otherwise
    read the message as the operator's own prompt (pi shows it as a prompt, so it gets none of them)."""
    if from_key:
        return agent_header(from_key, from_title, harness) + body
    return HEADER + body if harness in ("claude", "agy") else body


def line(harness: str, session_id: str, body: str, msg_id: int, from_key: str | None = None, from_title: str | None = None) -> bytes:
    content = message_text(body, harness, from_key, from_title)
    return (json.dumps({"type": "user", "message": {"content": content}, "msg_id": f"ctx-{msg_id}",
                        "session_id": session_id}) + "\n").encode()


async def deliver(socket_path: str, harness: str, session_id: str, body: str, msg_id: int, timeout: float = TIMEOUT,
                  from_key: str | None = None, from_title: str | None = None) -> str | None:
    """None when the session took it, else why not."""
    try:
        r, w = await asyncio.wait_for(asyncio.open_unix_connection(socket_path), timeout)
    except (OSError, asyncio.TimeoutError) as e:
        return f"can't connect to the session's socket: {e.__class__.__name__}"
    try:
        w.write(line(harness, session_id, body, msg_id, from_key, from_title))
        await asyncio.wait_for(w.drain(), timeout)
        if harness != "pi":
            return None
        raw = await asyncio.wait_for(r.readline(), timeout)
        try:
            reply = json.loads(raw or b"{}")
        except ValueError:
            return "pi extension sent an unreadable reply"
        return None if reply.get("ok") else str(reply.get("error") or "pi extension refused it")
    except (OSError, asyncio.TimeoutError) as e:
        return f"delivery failed: {e.__class__.__name__}"
    finally:
        w.close()
        try:
            await w.wait_closed()
        except OSError:
            pass


def agy_inbox_dir() -> Path:
    """One jsonl per agy conversation, appended here and claimed by that conversation's hooks (the agy hook reads
    the whole file at once, so nothing is shown twice)."""
    return Path(os.environ.get("CTXSWITCH_AGY_INBOX") or Path.home() / ".local" / "state" / "ctxswitch" / "agy-inbox")


async def _tmux(run: Callable[..., Any], *argv: str, stdin: str | None = None) -> tuple[Any, str | None]:
    """What ``run`` returned and why not (None when tmux took the command). argv, never a shell, and never longer
    than TMUX_TIMEOUT: agy's own pane is on the other end of a hung server."""
    args = {"capture_output": True, "text": True, "timeout": TMUX_TIMEOUT}
    if stdin is not None:
        args["input"] = stdin
    try:
        r = await asyncio.to_thread(run, ["tmux", *argv], **args)
    except Exception as e:                                    # noqa: BLE001 — tmux is never worth a traceback
        return None, f"tmux {argv[0]}: {e.__class__.__name__}"
    if getattr(r, "returncode", 1):
        return r, f"tmux {argv[0]}: {((getattr(r, 'stderr', '') or '').strip() or f'exit {r.returncode}')[:120]}"
    return r, None


# What agy shows while it waits on a choice (a permission or trust dialog): typing a message and Enter into one
# could pick its highlighted option. The trust dialog of agy 1.5.12 reads "↑/↓ Navigate · enter Confirm".
DIALOG_MARKS = ("Navigate", "enter Confirm", "trust this folder", "Allow", "Deny")


def _not_at_prompt(screen: str) -> str | None:
    """None when the pane shows agy waiting at an EMPTY input box (a line that is just ">") and no dialog; else why
    not. An empty box also means the operator isn't halfway through typing something of their own."""
    lines = [ln.strip() for ln in screen.splitlines() if ln.strip()][-15:]
    if any(m in ln for ln in lines for m in DIALOG_MARKS):
        return "agy is showing a dialog"
    if ">" not in lines:
        return "agy's input box isn't empty (or isn't on screen)"
    return None


async def _type_into_pane(pane: str, text: str, msg_id: int, run: Callable[..., Any]) -> str | None:
    """The message into a pane sitting at agy's prompt: a bracketed paste (plain newlines would submit a
    multi-line message line by line) and Enter once it has landed. None when it took it, else why not."""
    r, why = await _tmux(run, "display-message", "-p", "-t", pane, "#{pane_current_command}")
    if why:
        return why
    if str(getattr(r, "stdout", "") or "").strip() != "agy":
        return "that pane no longer runs agy"                 # it exited into a shell: don't type into someone's terminal
    r, why = await _tmux(run, "capture-pane", "-p", "-t", pane)
    if why:
        return why
    why = _not_at_prompt(str(getattr(r, "stdout", "") or ""))
    if why:
        return why
    buf = f"ctx-{msg_id}"
    _, why = await _tmux(run, "load-buffer", "-b", buf, "-", stdin=text)
    if why:
        return why
    _, why = await _tmux(run, "paste-buffer", "-p", "-d", "-b", buf, "-t", pane)
    if why:
        return why
    await asyncio.sleep(PASTE_SETTLE_S)                       # the paste has to be in the input box before Enter
    _, why = await _tmux(run, "send-keys", "-t", pane, "Enter")
    return why


async def deliver_agy(live: Liveness, session_id: str, body: str, msg_id: int, from_key: str | None = None,
                      from_title: str | None = None, run: Callable[..., Any] = subprocess.run,
                      inbox_dir: Path | None = None) -> str | None:
    """None once an idle agy took the message through its pane or it reached the inbox, else why neither worked.
    A busy session's PreInvocation hook injects it mid-turn, Stop catches what is left at the turn's end; an idle
    one outside tmux takes it with its next turn."""
    text = message_text(body, "agy", from_key, from_title)
    at = live.tmux_at or ""
    pane = at.rsplit(".", 1)[-1] if "." in at else None      # "<session>:<window_id>.<pane_id>"
    if live.status == "idle" and pane:
        why = await _type_into_pane(pane, text, msg_id, run)
        if why is None:
            return None
        log.info("message %d -> agy %s: %s, so the inbox", msg_id, session_id, why)
    try:
        d = inbox_dir or agy_inbox_dir()
        await asyncio.to_thread(os.makedirs, d, 0o700, True)
        p = d / f"{session_id}.jsonl"
        fd = await asyncio.to_thread(os.open, p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            await asyncio.to_thread(os.write, fd, (json.dumps({"msg_id": msg_id, "text": text}) + "\n").encode())
        finally:
            await asyncio.to_thread(os.close, fd)
        await asyncio.to_thread(os.chmod, p, 0o600)           # a file that already existed with other rights
    except OSError as e:
        return f"can't write the agy inbox: {e}"
    return None
