# ctx board (herdr-app mod)

The ctx board as a herdr-app mod: the session list, its two-line header and the selected session's
detail card (`GET /v1/board`, `GET /v1/board/{key}`, `GET /v1/health`), and the keys that act on
them. The mod holds **no business rules** — every glyph, state word, mark and section on screen is
in the JSON the hub returns, and what to run comes from `GET /v1/launch`. `actions.js` turns those
answers into `{command, args}` steps, so the decisions are testable without gpui-kit or a hub and
`main.js` is only plumbing: fetch, run, paint.

## Install

```sh
mkdir -p ~/.config/herdr-app/mods
cp -r herdr/mods/ctx-board ~/.config/herdr-app/mods/ctx-board
cat > ~/.config/herdr-app/ctx.json <<'JSON'
{
  "hub_url": "http://laptop:4000/v1",
  "token": "…",
  "host_id": "laptop",
  "machines": { "gpu-host": "vps" }
}
JSON
herdr-app check ~/.config/herdr-app/mods/ctx-board
```

`ctx install --herdr` is meant to do those three steps (mod files, `ctx.json`, the key section);
today that flag isn't in the CLI yet, so the steps above are the install. Saving a mod file reloads
it in place; the mod docks in the left column 340 px wide (`mod.json`).

- **`ctx.json`** lives beside `settings.json` (`appHome()`) and is the only file with the hub
  `token`, which is sent as one `Authorization: Bearer` header and never logged.
- **`machines`** maps ctx's `host_id` onto the **herdr machine name** that reaches it
  (`herdr-app do space.new --machine …`, `hc machine ls`). Only the hosts that live *elsewhere* are
  named: an absent entry (or `null`) means this computer, which is `machine: ""`. Terminal
  addresses follow the same rule — `t7` here, `vps/t3` on a mapped host — and that is how a row's
  `hc_at` finds its terminal when no `ctx.key` label matched.
- **keys** — merge `keys.example.json`'s `"mod:ctx-board"` section into
  `~/.config/herdr-app/keys.json` (the file is yours once you edit it, so new defaults never land
  on their own). They only reach the mod while it holds the keyboard: `mod.json` is `focusable`, so
  `pane.go` can step onto it, and it focuses itself on the `focus` command.

## What it shows

- **Header** — `ctx · <host> · N live · M waiting on you | nothing waiting · pool on` and
  `today $T · cloud C · local L · $B left`, with the filter's own line (`all hosts · + ended`) when
  it isn't the plain one.
- **List** — one row per session: its number, a state glyph coloured by state, the harness/account
  label, the pool mark, the title, and on the right `◐3m` (a question waiting), `+N` (exchanges
  since you looked) and the cost. Pool jobs sit indented under their parent with a `↳`; a waiting
  row gets a `└ …` line saying what it waits on.
- **Detail** (below the list, at most ~45 % of the height, scrollable) — the sections in the
  Textual board's order: RECAP (with `(updating…)` while the hub says it is stale), WAITING ON YOU,
  SPEND, POOL JOBS, INFO, LAST EXCHANGE, SINCE YOU LOOKED, MESSAGES, YOUR INSTRUCTIONS, AGENT SAYS,
  DECISIONS, SAME WORK ELSEWHERE, EARLIER. Empty sections are omitted.
- **Polling** — `GET /v1/health` every 2 s; the board (and the selected session's detail) is fetched
  only when `change` moves, and a redraw happens only when something actually changed, plus one
  every 30 s so the ages don't go stale.

## Keys → herdr commands → hub routes

`enter` open · `n`/`s` start · `m` message · `a` answer · `i` retire · `k` end · `h` host filter ·
`e` ended rows · `escape` clear · `1`…`9` select · `up`/`down` move. A click selects, a second click
on the same row opens.

| key | what happens | herdr | hub |
| --- | --- | --- | --- |
| `enter` / double-click | its terminal is here → show it (a parked one gets a tab pane first). Ended and not here → resume it: the hub's recipe becomes a space on its host. Live and not here → a toast says whose herdr it runs in. | `agent.focus {terminal}`, `terminal.keep {terminal, place:"tab"}`, `space.new {name, machine, folder, argv}`, `hc label <addr> ctx.key=<key>` | `GET /v1/launch?key=`, `POST /v1/sessions/{key}/viewed` (every case — that is what clears the `+N`) |
| `n` / `s` | pick host → harness → folder, then start that. The hosts are `/v1/hosts`' own words: this computer first, then the others; the harness is that host's claude accounts by name, `pi`, and a ctx `shell` when it has one. | `space.new …` (+ the label step when the recipe carries a key) | `GET /v1/hosts`, `GET /v1/launch?host=&harness=[&profile=&cwd=]` |
| `m` | one line to send. Refused up front with the hub's own `can_message` reason when there is one. | — | `POST /v1/sessions/{key}/messages {body, from_host}` |
| `a` | answers what it asked: the body carries `answers: reply_target.ref`. Refused with `reply_why` when `reply_target` is null. | — | as `m` |
| `i` | lists `detail.instructions` (`n • text`); a click, or its digit, retires that one. | — | `POST /v1/sessions/{key}/ledger/{id}/retire` |
| `k` | confirms, then kills its terminal; with no terminal here it says where it actually runs. | `terminal.kill {terminal}` | — |
| `h` | this host → each other host → every host → back here. | — | `GET /v1/board?host=…` (`host=` is every host; an omitted host is *this* computer's, so the filter starts at `host_id`) |
| `e` | adds the ended rows (`ended=1`). | — | `GET /v1/board?…&ended=1` |
| `escape` | closes a popup if one is up, else clears the filters and the selection. | — | — |
| `1`…`9`, `up`/`down` | select a row — selection loads the detail card and opens nothing. | — | `GET /v1/board/{key}` |

Resuming and starting both end at `space.new`, whose machine comes from that map (`""` = this
computer). When the recipe knows a key that nothing else carries — a fresh `ctx shell` — the mod
labels the terminal it just made, so the next board read finds it by `labels["ctx.key"]`; otherwise
the collector's own label is already there.

The popups (host/harness picker, the message and folder fields, the instruction list, the end
confirm) are this same mod opened as an overlay with plain props: `Esc` closes them, and so does the
action they are asking about. A missing or unreadable `ctx.json`, or a hub that won't answer, shows
one line — `hub unreachable: …` — and the board keeps its shape.

## Layout

- `main.js` — the `View`: config, polling, state, popups, and the only place that touches gpui-kit.
- `actions.js` — the decisions as plain data: `terminalFor`, `openPlan`, `startPlan`, `endPlan`,
  `messageRequest`, `sendCheck`, `retireRequest`, `hostCycle`, `boardPath`, `launchPath`, `hostChoices`,
  `harnessChoices`, `spaceArgs`, `parseCommand`, `moveSelection`. No gpui-kit, no `herdr`, no fetch.
- `view.js` — the text builders (`age`, `money`, `tzOffset`, `rowParts`, `headerLines`,
  `detailSections`, `detailTitle`, `toneColor`), likewise plain strings and `{text, tone}` parts.
- `keys.example.json` — the `mod:ctx-board` section to merge into `keys.json`.
- `test/` — node's built-in runner, against real hub answers in `test/fixtures/`:

  ```sh
  node --test "herdr/mods/ctx-board/test/*.mjs"
  ```

  (On node 22 a bare directory argument is treated as a module to run rather than a folder to scan,
  so quote the glob.)
