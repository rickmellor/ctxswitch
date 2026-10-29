"""Write an operator message into a live session through its own socket.

Both harnesses take the same newline-delimited JSON line (spikes/SPIKES.md (a)):
  {"type": "user", "message": {"content": <text>}, "msg_id": "ctx-<id>", "session_id": <target session id>}
Claude: the inbox socket from its registry (``messagingSocketPath``). It answers nothing; the receiver checks
  ``session_id`` and drops a line meant for another session. It arrives as a peer message, held for approval when
  the session is in plan or bypass mode.
Another agent's message carries agent_header (sender's key, title and host) instead, for both harnesses.
pi: the socket the ctxswitch extension opens (pi_ext/ctxswitch.ts). It answers {"ok": true} or {"ok": false,
  "error": ...} after handing the text to pi.sendUserMessage (queued as a follow-up while pi is busy).
"""
from __future__ import annotations

import asyncio
import json

TIMEOUT = 5.0
HEADER = "Message from the operator (sent with ctxswitch):\n\n"


# Claude's own SendMessage only reaches sessions on its host, and a haiku session given "the send_message tool"
# picked it (2026-10-29), so Claude gets the MCP tool's full name
REPLY_TOOL = {"claude": "mcp__ctxswitch__send_message tool (ctxswitch MCP; not SendMessage)"}


def agent_header(from_key: str, title: str | None = None, harness: str = "pi") -> str:
    """Heads another agent's message, for either harness (pi would otherwise show it as the operator's prompt)."""
    host = from_key.split("/", 1)[0]
    name = f' "{" ".join(title.split())[:80]}"' if title and title.strip() else ""
    tool = REPLY_TOOL.get(harness, "ctxswitch MCP server's send_message tool")
    return (f"Message from another agent session, not from the operator (sent with ctxswitch).\n"
            f"From: session {from_key}{name} on host {host}\n"
            f"To reply, use the {tool} with session \"{from_key}\".\n\n")


def line(harness: str, session_id: str, body: str, msg_id: int, from_key: str | None = None, from_title: str | None = None) -> bytes:
    if from_key:
        content = agent_header(from_key, from_title, harness) + body
    else:
        content = HEADER + body if harness == "claude" else body
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
