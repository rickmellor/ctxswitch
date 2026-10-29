"""Write an operator message into a live session through its own socket.

Both harnesses take the same newline-delimited JSON line (spikes/SPIKES.md (a)):
  {"type": "user", "message": {"content": <text>}, "msg_id": "ctx-<id>", "session_id": <target session id>}
Claude: the inbox socket from its registry (``messagingSocketPath``). It answers nothing; the receiver checks
  ``session_id`` and drops a line meant for another session. It arrives as a peer message, held for approval when
  the session is in plan or bypass mode.
pi: the socket the ctxswitch extension opens (pi_ext/ctxswitch.ts). It answers {"ok": true} or {"ok": false,
  "error": ...} after handing the text to pi.sendUserMessage (queued as a follow-up while pi is busy).
"""
from __future__ import annotations

import asyncio
import json

TIMEOUT = 5.0
HEADER = "Message from the operator (sent with ctxswitch):\n\n"


def line(harness: str, session_id: str, body: str, msg_id: int) -> bytes:
    content = HEADER + body if harness == "claude" else body
    return (json.dumps({"type": "user", "message": {"content": content}, "msg_id": f"ctx-{msg_id}",
                        "session_id": session_id}) + "\n").encode()


async def deliver(socket_path: str, harness: str, session_id: str, body: str, msg_id: int, timeout: float = TIMEOUT) -> str | None:
    """None when the session took it, else why not."""
    try:
        r, w = await asyncio.wait_for(asyncio.open_unix_connection(socket_path), timeout)
    except (OSError, asyncio.TimeoutError) as e:
        return f"can't connect to the session's socket: {e.__class__.__name__}"
    try:
        w.write(line(harness, session_id, body, msg_id))
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
