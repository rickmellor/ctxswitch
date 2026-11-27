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
| `cancel` | | dismiss what is on top — the Picker / StartScreen / Confirm / Spend screen / Stream Deck settings screen, or the board's text box — as Esc / "no" does; `ok` even if none is open |
| `gpu` | | `g`: the CPU/GPU panel on or off (also `metrics`) |
| `pool` | | `P`: this machine's local-pool panel on or off (also `pool_panel`) |
| `spend` | | `$`: the spend screen. A toggle — while it is the top screen this closes it, anywhere else it opens one |
| `deck_settings` | | `D`: the Stream Deck's settings screen. A toggle, the same way `spend` is |
| `deck_set` | any of `top_row` / `brightness` / `animations` | one or more of the deck's settings, validated by the same rules the settings file has (see `docs/deck-settings.md`): `top_row` `"pages"` or `"scroll"`, `brightness` an int 10..100, `animations` a bool. Saves what is valid and replies `{"ok": true, "settings": {…}, "state": {…}}` with the whole set as it now stands; one bad value saves nothing and replies `{"ok": false, "error": "bad setting"}`. Works with or without the screen up, and re-renders it when it is |
| `exchange` | | `x`: send the selected session elsewhere (opens a Picker, which `pick` / `cancel` then drive). Needs a selected session |
| `detach` | | `q`: inside tmux detach (everything keeps running); outside it, quit the board |

## Other board keys

Every remaining key on the board has a command named after its action (`CtxApp.BINDINGS`, the `action_` prefix
dropped), so this table is the whole keyboard map read another way. Each runs that action on the app's loop and
replies `{"ok": true, "state": {…}}`; what it does is what the key does, including any prompt or notice it raises.
The commands in the first table cover the rest of the keyboard: `new` (`n`), `start` (`s`), `end` (`k`), `slot` /
`select` (`Alt+N` / a digit) and the five above.

| key | cmd | does |
|---|---|---|
| `Enter` | `resume` | open the highlighted row: swap it in, resume it, or say why not |
| `m` | `message` | message it: the text box comes up (`modal` becomes `"input"`), Enter sends, Esc / `cancel` drops it |
| `a` | `reply` | answer the question it is waiting on, same box |
| `M` | `message_buffer` | send it the tmux paste buffer, after a y |
| `W` | `message_work` | message its workstream: whichever session is, or next is, live on it |
| `t` | `misplace_move` | a prompt marked ↯: move it where it looks like it belongs, after a y |
| `o` | `misplace_keep` | keep the ↯ prompt where it is (nothing is sent) |
| `d` | `misplace_drop` | drop the ↯ prompt (the agent gets Escape and a disregard message) |
| `Q` | `kill_all` | end the tmux session and every agent in it, after a y |
| `h` | `cycle_host` | host filter: this host → all → each other host |
| `/` | `filter` | filter by text: the box comes up with what is filtered now |
| `e` | `toggle_ended` | show or hide ended sessions |
| `v` | `mark_viewed` | mark the highlighted session viewed |
| `w` | `toggle_ws` | group the list by workstream, or back to a flat list |
| `r` | `refresh` | refresh now (and re-read the pool panel while it is shown) |
| `p` | `toggle_popups` | pop-ups off / on for this run |
| `i` | `retire` | retire one of YOUR INSTRUCTIONS: the box comes up asking which |
| `?` | `help` | help, filling the pane (Esc closes it; it is not a modal a client drives) |
| `G` | `metrics_host` | pick which hosts the CPU/GPU panel stacks (opens a Picker) |
| `T` | `pool_review` | the pool panel: review on, or off |
| `R` | `pool_reviewer` | the pool panel: the next verified reviewer |
| `+` / `-` | `pool_rounds_up` / `pool_rounds_down` | the pool panel: one review round more / fewer, 1–5 |
| `D` | `deck_settings` | the Stream Deck's settings: `t` the top row (`pages` ⇄ `scroll`), `+` / `-` the backlight ±10 (10..100), `a` the animations on/off. Each one is written to the settings file as it is set; `deck_set` is the same from the wire |
| `<` / `>` | `wider` / `narrower` | the board 5 columns wider / narrower (inside ctx tmux) |
| `[` / `]` | `list_shorter` / `list_taller` | the session list one line shorter / taller |
| `\` | `list_auto` | the list's height back to automatic |
| `Esc` | `clear_filter` | clear the text filter, or drop out of the box, and give the table focus |

Commands that act on the highlighted row — `resume`, `message`, `reply`, `message_buffer`, `message_work`,
`misplace_move`, `misplace_keep`, `misplace_drop`, `mark_viewed`, `retire`, `exchange` — answer
`{"ok": false, "error": "nothing selected"}` when nothing is. Two of them, `message` and `message_work`, take a
workstream header (`w`) for a row as well: that is what those keys do on one.

## Replies (board → client)

`{"ok": true, "id": <echoed>, ...}` or `{"ok": false, "id": <echoed>, "error": "<short reason>"}`.
Errors (exact strings): `"no such slot"`, `"no picker open"`, `"no confirm open"`, `"nothing selected"`,
`"unknown cmd"`, `"bad request"`.
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
  "options": [],
  "panels": {"gpu": false, "pool": false},
  "settings": {"top_row": "pages", "brightness": 60, "animations": true}
}
```

- `rows`: every real session, in board order (`self.slots` order); pool-job children are not rows, their count
  is `pool_jobs` on the parent. `n` is the row's position in that order: the board draws the number only for `n` ≤ 9,
  while `slot` / `select` accept any `n` listed here. `state` is
  `fmt.session_state()`: `busy | waiting | idle | shell | unknown | ended`. `visible` = this session is in the main
  pane (`▶`), `selected` = the highlighted row. `title` = `fmt.display_title`, `harness` = `fmt.harness_label`.
- `modal`: `null | "picker" | "start" | "confirm" | "spend" | "deck_settings" | "input"` — what is on top of the
  screen stack (`"input"` = the board's text Input has focus: `/`, `m`, `a` or a directory prompt). `cancel` closes any
  of them, the Spend and Stream Deck settings screens included.
- `options`: when `modal` is `picker` or `start`, `[{"n": 1, "label": "claude (max)"}, ...]` for the numbered
  choices a `pick` accepts; otherwise `[]`. For `confirm`, `prompt` carries the question text.
- `panels`: the two panels a key shows and hides — `gpu` is `g`'s CPU/GPU panel (`action_metrics`), `pool` is `P`'s
  local-pool panel (`action_pool_panel`). Both bool, and they follow the key: a client that lights a key while its
  panel is up can ask the board whether it is.
- `settings`: the Stream Deck's settings as the settings file has them (defaults filled, values checked), so a client
  can read them without touching the file — `deck_set` is how it changes them. Read off the file each time, so it is
  current even when something else wrote it.
