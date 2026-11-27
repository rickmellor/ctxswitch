# Board control protocol (proto 1)

The ctxswitch board (`ctx tui`, Textual app `CtxApp`) listens on a unix stream socket at
`~/.local/state/ctxswitch/board.sock` (override: env `CTXSWITCH_BOARD_SOCK`). The socket file is created with mode
0600 at mount, removed on exit; a stale file is unlinked before bind. Transport is newline-delimited JSON
(NDJSON), UTF-8: one object per line in each direction. Several clients may be connected at once.

## Requests (client → board)

Every request is `{"cmd": "<name>", ...}`; an optional `"id"` (any JSON value) is echoed in the reply.

| cmd | fields | effect on the board |
|---|---|---|
| `hello` | | reply includes `"proto": 1` and the current `state` |
| `state` | | reply with the current `state` |
| `subscribe` | | reply with `state`; afterwards the board pushes `{"event":"state","state":{...}}` every time the table is re-rendered or a modal opens/closes (coalesced to at most one push per 100 ms) |
| `slot` | `n` 1..9 | `open_key(slots[n-1])` — exactly Alt+N / Enter: select and swap into the main pane |
| `select` | `n` 1..9 | `select_key(slots[n-1])` — highlight only, detail panel follows, no pane swap |
| `move` | `delta` -1 or +1 | move the highlight one numbered row up/down, clamped at the ends, **regardless of which widget has focus**; detail panel follows |
| `new` | | same as `n` (`action_new_session`) |
| `start` | | same as `s` (`action_start`) |
| `end` | | same as `k` (`action_end_session`): opens the y/n Confirm |
| `pick` | `n` 1..9 | while a Picker or StartScreen is on top: choose option n, as pressing that digit does |
| `confirm` | `yes` bool | while a Confirm is on top: answer it |
| `cancel` | | dismiss the top modal (Picker / StartScreen / Confirm) as Esc / "no" does; `ok` even if none is open |

## Replies (board → client)

`{"ok": true, "id": <echoed>, ...}` or `{"ok": false, "id": <echoed>, "error": "<short reason>"}`.
Errors (exact strings): `"no such slot"`, `"no picker open"`, `"no confirm open"`, `"unknown cmd"`, `"bad request"`.
Requests are executed on the Textual event loop (`app.call_from_thread` or an asyncio server on the app loop) and
the reply is sent after the action has run.

## `state` object

```json
{
  "proto": 1,
  "rows": [
    {"n": 1, "key": "laptop/claude/abc", "title": "Streamdeck button box", "host": "laptop",
     "harness": "claude", "state": "busy", "visible": true, "selected": false, "pool_jobs": 0}
  ],
  "slots": ["laptop/claude/abc", "..."],
  "visible_key": "laptop/claude/abc",
  "selected_key": "laptop/claude/def",
  "modal": null,
  "options": []
}
```

- `rows`: the numbered rows only, in board order (`self.slots` order); pool-job children are not rows, their count
  is `pool_jobs` on the parent. `n` is the number drawn on the board (1..SLOTS). `state` is
  `fmt.session_state()`: `busy | waiting | idle | shell | unknown | ended`. `visible` = this session is in the main
  pane (`▶`), `selected` = the highlighted row. `title` = `fmt.display_title`, `harness` = `fmt.harness_label`.
- `modal`: `null | "picker" | "start" | "confirm" | "input"` — what is on top of the screen stack (`"input"` = the
  board's text Input has focus: `/`, `m`, `a` or a directory prompt).
- `options`: when `modal` is `picker` or `start`, `[{"n": 1, "label": "claude (max)"}, ...]` for the numbered
  choices a `pick` accepts; otherwise `[]`. For `confirm`, `prompt` carries the question text.
