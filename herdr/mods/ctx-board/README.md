# ctx board (herdr-app mod)

The ctx board as a herdr-app mod: the session list, its two-line header and the selected session's
detail card, drawn from the hub's own view model (`GET /v1/board`, `GET /v1/board/{key}`,
`GET /v1/health`). The mod holds **no business rules** — every glyph, state word, mark and section
on screen is in the JSON the hub returns; the mod only maps it to gpui-kit elements.

## Install

```sh
mkdir -p ~/.config/herdr-app/mods
cp -r herdr/mods/ctx-board ~/.config/herdr-app/mods/ctx-board
cat > ~/.config/herdr-app/ctx.json <<'JSON'
{ "hub_url": "http://laptop:4000/v1", "token": "…", "host_id": "laptop" }
JSON
herdr-app check ~/.config/herdr-app/mods/ctx-board
```

`ctx.json` lives beside `settings.json` (`appHome()`): `hub_url`, the hub `token` (sent only as an
`Authorization: Bearer` header, never logged), the `host_id` to filter to, and any `machines` list.
Saving the mod's files reloads it in place; it docks in the left column 340 px wide (`mod.json`).

## What it shows

- **Header** — `ctx · <host> · N live · M waiting on you | nothing waiting · pool on` and
  `today $T · cloud C · local L · $B left`.
- **List** — one row per session: its number, a state glyph coloured by state (busy → working,
  waiting → blocked, idle → idle, ended → dim), the harness/account label, the pool mark (green →
  working, grey → dim, yellow → blocked), the title, and on the right `◐3m` (a question waiting),
  `+N` (exchanges since you looked) and the cost. Pool jobs sit indented under their parent with a
  `↳` and their job label; a waiting row gets a `└ …` line saying what it waits on.
- **Detail** (below the list, at most ~45 % of the height, scrollable) — the sections in the
  Textual board's order: RECAP (with `(updating…)` while the hub says it is stale), WAITING ON YOU,
  SPEND, POOL JOBS, INFO, LAST EXCHANGE, SINCE YOU LOOKED, MESSAGES, YOUR INSTRUCTIONS, AGENT SAYS,
  DECISIONS, SAME WORK ELSEWHERE, EARLIER. Empty sections are omitted.
- **Polling** — `GET /v1/health` every 2 s; the board (and the selected session's detail) is fetched
  only when `change` moves, and a redraw happens only when something actually changed, plus one
  every 30 s so the ages don't go stale. Any failure shows one line — `hub unreachable: …`.

**Selecting** — a click on a row, or `1`…`9` with the board focused (`mod.json` is `focusable`, so
`pane.go` can step onto it; it focuses itself on the `focus` command and reads keys through
`on_key_down`). Selection only loads the detail card; it opens nothing.

## Not yet

**Actions.** `main.js` routes every action through one `act(name, row, cx)` dispatch stub that logs
for now; open / start / message / answer will join it there, and everything they need
(`reply_target`, `can_message`, `host_id`, `harness`, `tmux_at`) is already on the row the hub
returns, so no rule work is required to add them.

## Layout

- `main.js` — the `View`: config, polling, state, and the only place that touches gpui-kit.
- `view.js` — pure text builders (`age`, `money`, `tzOffset`, `rowParts`, `headerLines`,
  `detailSections`, `detailTitle`, `toneColor`) returning plain strings and `{text, tone}` parts, so
  they are testable without gpui-kit or a hub.
- `test/view.test.mjs` — node's built-in runner, against real hub answers in `test/fixtures/`:

  ```sh
  node --test "herdr/mods/ctx-board/test/*.mjs"
  ```

  (On node 22 a bare directory argument is treated as a module to run rather than a folder to scan,
  so quote the glob.)
