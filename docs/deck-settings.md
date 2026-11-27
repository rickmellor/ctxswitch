# Deck settings file (shared contract for both jobs)

Path: `~/.local/state/ctxswitch/deck-settings` (env override `CTXSWITCH_DECK_SETTINGS`). JSON object, written whole,
atomically (write `<path>.tmp` then `os.replace`). Missing file or unreadable JSON = defaults. Unknown keys are kept
by the board when it rewrites the file (read, update, write).

```json
{"top_row": "pages", "brightness": 60, "animations": true}
```

| key | values | default | meaning |
|---|---|---|---|
| `top_row` | `"pages"` or `"scroll"` | `"pages"` | how the top row handles more than five sessions |
| `brightness` | int 10..100, steps of 10 | 60 | key backlight |
| `animations` | bool | true | breathe/sweep/wipe/pending animations; off = static faces, state still updates |

Reader (`ctx deck`): `load_settings(path) -> dict` with defaults filled and values validated (bad value → default
for that key). The deck watches the file's mtime once a second and applies a change live: `top_row` re-plans the top
row (reset the page/scroll offset to show the highlighted row), `brightness` calls `deck.set_brightness`,
`animations` stops/starts the animation tick. A `--brightness` CLI flag given explicitly wins over the file for that run.

Writer (the board's `DeckSettingsScreen`): `save_settings(path, settings)`; the same two helpers live in
`src/ctxswitch/common/deck_settings.py` so both sides import one implementation.
