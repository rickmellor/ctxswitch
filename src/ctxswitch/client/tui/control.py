"""The board's control socket: a unix socket another program drives `ctx tui` through.

The owner's Stream Deck daemon is the client (the whole wire contract is in docs/board-control.md). It can't see the
board's row numbers or which session sits in the main pane — only this process knows `slots` and `visible_key` — so
the endpoint lives here, and every command runs a board action on the app's own event loop (an asyncio server on that
loop, so no `call_from_thread` anywhere).

The socket is 0600 under `~/.local/state/ctxswitch/board.sock` (env `CTXSWITCH_BOARD_SOCK`). A board that can't bind
one — a sandbox that refuses unix sockets, a directory it can't write — logs it and carries on: the board works, it
just can't be driven.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ctxswitch.client import fmt

if TYPE_CHECKING:                                        # only for the annotations: no cycle at runtime
    from ctxswitch.client.tui.app import CtxApp

PROTO = 1
COALESCE_S = 0.1                                         # one pushed state per this much, however many renders
PUSH_S = 30.0                                            # a subscriber that can't read this long is dropped
HOME_PATH = Path.home() / ".local/state/ctxswitch/board.sock"   # the operator's own board, when nothing redirects it
SOCK_FILE = Path(os.environ.get("CTXSWITCH_BOARD_SOCK") or HOME_PATH)


def under_test() -> bool:
    """Whether a test harness is running this board. The suite mounts the app by the hundred, and one binding the
    operator's own path would take the control endpoint off the board they are actually driving."""
    return "pytest" in sys.modules and not os.environ.get("CTXSWITCH_BOARD_SOCK")


def top_modal(app: "CtxApp"):
    """The screen on top, when it's one the control protocol can answer: the Picker, the Start screen or the y/n.
    The board pushes others (help, spend, the host picker) and those are nobody's to drive from outside."""
    from ctxswitch.client.tui.app import Confirm, Picker, StartScreen
    top = app.screen_stack[-1] if len(app.screen_stack) > 1 else None
    return top if isinstance(top, (Picker, StartScreen, Confirm)) else None


def input_focused(app: "CtxApp") -> bool:
    """Whether the board's text Input has focus with something to type into it: that's state's `"input"` modal.
    Hidden (its mode back to "filter" and nothing typed) it sits in the layout unfocused, which is not a modal."""
    try:
        box = app.query_one("#filter")
    except Exception:                                    # noqa: BLE001 - not composed yet
        return False
    return bool(box.display and box.has_focus) or app.input_mode != "filter"


def modal_of(app: "CtxApp") -> str | None:
    """What's on top of the screen stack, as state names it: picker / start / confirm / input, or None."""
    top = top_modal(app)
    if top is not None:
        from ctxswitch.client.tui.app import Confirm, Picker
        if isinstance(top, Picker):
            return "picker"
        return "start" if not isinstance(top, Confirm) else "confirm"
    return "input" if input_focused(app) else None


def options_of(app: "CtxApp") -> list[dict]:
    """The numbered choices a `pick` accepts right now: the Picker's options, or the Start screen's profile keys (the
    ones its digit keys start). [] for anything else."""
    from ctxswitch.client.tui.app import Picker, StartScreen
    top = top_modal(app)
    opts = top.options if isinstance(top, Picker) else top._opts() if isinstance(top, StartScreen) else []
    return [{"n": n, "label": str(label)} for n, (label, _, _) in enumerate(opts, 1)]


def prompt_of(app: "CtxApp") -> str:
    """The y/n's question, "" for anything else."""
    from ctxswitch.client.tui.app import Confirm
    top = top_modal(app)
    return str(getattr(top, "prompt", "") or "") if isinstance(top, Confirm) else ""


def board_state(app: "CtxApp") -> dict:
    """The board as state: its numbered rows in board order, which one is in the main pane, which is highlighted,
    what modal is up and the choices it offers. A pool-job child isn't a row: it counts in its parent's `pool_jobs`,
    the same nesting `render_table` draws (through `_nest`, so the two can't drift apart)."""
    from ctxswitch.client.tui.app import SLOTS
    slots = list(app.slots)
    visible = {s["key"]: s for s in app._visible()}
    order, nested = app._nest(list(visible.values()))
    kids: dict[str, int] = {}
    for s in order:
        if s["key"] in nested:
            kids[fmt.link(s, "parent_key")] = kids.get(fmt.link(s, "parent_key"), 0) + 1
    by_key = {s["key"]: s for s in app.sessions}
    rows = []
    for n, key in enumerate(slots[:SLOTS], 1):
        s = by_key.get(key) or {"key": key}
        rows.append({"n": n, "key": key, "title": fmt.display_title(s), "host": s.get("host_id") or "",
                     "harness": fmt.harness_label(s, getattr(app.cfg, "claude_profiles", None)),
                     "state": fmt.session_state(s), "visible": key == app.visible_key,
                     "selected": key == app.selected, "pool_jobs": kids.get(key, 0)})
    out = {"proto": PROTO, "rows": rows, "slots": slots, "visible_key": app.visible_key,
           "selected_key": app.selected, "modal": modal_of(app), "options": options_of(app)}
    if out["modal"] == "confirm":
        out["prompt"] = prompt_of(app)
    return out


class ControlServer:
    """The NDJSON endpoint: one object per line each way, several clients at once, state pushed to whoever subscribes."""

    def __init__(self, app: "CtxApp", path: Path | None = None):
        self.app = app
        # the environment read here rather than as the default's value, which would freeze it at import: a test, or
        # a second board pointed at its own socket, sets it after the module was already loaded
        self.path = Path(path) if path is not None else Path(os.environ.get("CTXSWITCH_BOARD_SOCK") or HOME_PATH)
        self.subs: list = []                             # each: {"writer":…, "pending":bool, "last":float, "slow":bool}
        self._server = None
        self._timer = None
        self._pending = False

    def _said(self, why: str) -> None:
        """Why this board has no endpoint, on the app's log. A board that never got as far as having a log — a stub
        in a test — has nothing to say it on."""
        log = getattr(self.app, "log", None)
        if log is not None:
            log.warning(f"board control socket {self.path}: {why}")

    async def start(self) -> None:
        """Bind it. A board that can't — a sandbox that refuses unix sockets, a state directory it can't write —
        says so on its own log and runs without the endpoint, and so does one that finds another board already
        answering that path: the socket belongs to whichever board is really running."""
        if under_test() and self.path == HOME_PATH:
            self._said("a test board binds no control socket")   # the suite runs boards by the hundred: they must
            return                                              # never take over the one someone is driving
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if await self._live():
                self._server = None
                self._said("another board is already answering it")
                return
            self.path.unlink(missing_ok=True)            # a stale file from a board that died
            self._server = await asyncio.start_unix_server(self.handle, path=str(self.path))
            os.chmod(self.path, 0o600)
        except Exception as e:                           # noqa: BLE001 - the board must still run
            self._server = None
            self._said(f"{e.__class__.__name__}: {e}")

    async def _live(self) -> bool:
        """Whether something answers on this path already: a stale file refuses the connection, a live board takes
        the handshake, and that's the difference between unlinking it and leaving another board's endpoint alone."""
        try:
            _r, writer = await asyncio.wait_for(asyncio.open_unix_connection(path=str(self.path)), timeout=0.5)
        except Exception:                                # noqa: BLE001 - refused or absent: nobody is there
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:                                # noqa: BLE001
            pass
        return True

    async def stop(self) -> None:
        """Close the listener and take the file away, so a client connecting later gets "no such file" rather than
        hanging on a socket nothing listens on."""
        for sub in list(self.subs):
            try:
                sub["writer"].close()
            except Exception:                            # noqa: BLE001
                pass
        self.subs.clear()
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._pending = False
        srv, self._server = self._server, None
        if srv is not None:
            srv.close()
            try:
                await srv.wait_closed()
            except Exception:                            # noqa: BLE001 - the app is going down
                pass
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    def push_state(self) -> None:
        """Note that the board changed. The push itself goes out at most once per COALESCE_S, so a burst of re-renders
        costs one line per subscriber, not one per render."""
        if not self.subs:
            return
        self._pending = True
        if self._timer is None:
            self._timer = self.app.set_timer(COALESCE_S, self._flush)

    async def _flush(self) -> None:
        self._timer = None
        if not self._pending:
            return
        self._pending = False
        if not self.subs:
            return
        line = (json.dumps({"event": "state", "state": board_state(self.app)}) + "\n").encode()
        now = asyncio.get_running_loop().time()
        for sub in list(self.subs):
            if sub["pending"]:                           # it hasn't taken the last line yet
                if now - sub["last"] > PUSH_S:
                    self._drop(sub)                      # a client that stopped reading: stop writing to it
                    continue
                sub["slow"] = True                       # it gets the newest state instead of this one
                self._pending = True
                continue
            sub["pending"] = True
            try:
                sub["writer"].write(line)
                await sub["writer"].drain()
                sub["pending"] = False
            except Exception:                            # noqa: BLE001 - a client that died mid-write
                self._drop(sub)
        if self._pending and self.subs:                  # a back-pressure push, so it can't cascade
            self._timer = self.app.set_timer(COALESCE_S, self._flush)

    def _drop(self, sub: dict) -> None:
        if sub in self.subs:
            self.subs.remove(sub)
        try:
            sub["writer"].close()
        except Exception:                                # noqa: BLE001
            pass

    def dispatch(self, req: dict) -> dict:
        """Run one request on the app's loop and say what came of it. The reply is sent after the action ran."""
        app = self.app
        cmd = req.get("cmd")
        if cmd == "hello":
            return {"ok": True, "proto": PROTO, "state": board_state(app)}
        if cmd == "state":
            return {"ok": True, "state": board_state(app)}
        if cmd == "subscribe":
            return {"ok": True, "state": board_state(app)}
        if cmd in ("slot", "select"):
            n = req.get("n")
            if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= len(app.slots):
                return {"ok": False, "error": "no such slot"}
            if cmd == "slot":
                app.open_key(app.slots[n - 1])
            else:
                app.select_key(app.slots[n - 1])
            return {"ok": True, "state": board_state(app)}
        if cmd == "move":
            delta = req.get("delta")
            if not isinstance(delta, int) or isinstance(delta, bool) or delta not in (-1, 1):
                return {"ok": False, "error": "bad request"}
            return ({"ok": True, "state": board_state(app)} if app.move_selection(delta)
                    else {"ok": False, "error": "no such slot"})
        if cmd in ("new", "start", "end"):
            {"new": app.action_new_session, "start": app.action_start, "end": app.action_end_session}[cmd]()
            return {"ok": True, "state": board_state(app)}
        if cmd == "pick":
            n = req.get("n")
            top = top_modal(app)
            if not isinstance(n, int) or isinstance(n, bool) or n < 1:
                return {"ok": False, "error": "bad request"}
            from ctxswitch.client.tui.app import Confirm
            if top is None or isinstance(top, Confirm):
                return {"ok": False, "error": "no picker open"}
            return ({"ok": True, "state": board_state(app)} if top.choose(n)
                    else {"ok": False, "error": "no such option", "state": board_state(app)})
        if cmd == "confirm":
            top = top_modal(app)
            from ctxswitch.client.tui.app import Confirm
            if not isinstance(top, Confirm):
                return {"ok": False, "error": "no confirm open"}
            top.answer(bool(req.get("yes")))
            return {"ok": True, "state": board_state(app)}
        if cmd == "cancel":
            top = top_modal(app)
            if top is not None:
                top.cancel()
            return {"ok": True, "state": board_state(app)}
        return {"ok": False, "error": "unknown cmd"}

    async def handle(self, reader, writer) -> None:
        """One client: a line in, a reply out, until it hangs up or stops reading."""
        sub = {"writer": writer, "pending": False, "last": 0.0, "slow": False}
        try:
            while True:
                try:
                    raw = await reader.readline()
                except (asyncio.IncompleteReadError, ConnectionError, ValueError):
                    return               # ValueError: a line past the reader's own limit
                if not raw:
                    return
                try:
                    req = json.loads(raw.decode("utf-8", "replace"))
                except (ValueError, UnicodeDecodeError):
                    req = None
                if not isinstance(req, dict):
                    reply = {"ok": False, "error": "bad request"}     # the connection stays open
                elif req.get("cmd") == "subscribe":
                    if sub not in self.subs:
                        self.subs.append(sub)
                    reply = self.dispatch(req)
                    sub["last"], sub["pending"] = asyncio.get_running_loop().time(), False
                else:
                    try:
                        reply = self.dispatch(req)
                    except Exception as e:               # noqa: BLE001 - a board action must not kill the client's line
                        reply = {"ok": False, "error": f"board: {e.__class__.__name__}"}
                rid = req.get("id") if isinstance(req, dict) else None
                if rid is not None:
                    reply["id"] = rid
                try:
                    writer.write((json.dumps(reply) + "\n").encode())
                    await writer.drain()
                except Exception:                        # noqa: BLE001 - it went away mid-reply
                    break
                if reply.get("ok") and req.get("cmd") == "subscribe":
                    sub["last"] = asyncio.get_running_loop().time()
                    sub["pending"] = False
        finally:
            self._drop(sub)
            try:
                writer.close()
            except Exception:                            # noqa: BLE001
                pass
