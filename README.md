# ctxswitch

**Short-term memory for people who run a lot of coding agents at once.**

If you keep a dozen Claude Code and pi sessions going across a few machines, the hard part stops being the code.
It becomes remembering, every time you switch back to one: *what was this session for, what did I tell it, what
did I already answer, and what is it waiting on right now?* ctxswitch keeps that for you, live, and shows it
in one narrow side panel next to whatever you are doing.

```
┌─ agent (left pane) ─────────────────────────┬─ ctx tui ─────────────────┐
│                                             │ ◐ 3m  pr-review  approve? │  ← waiting on you
│   the session you are working in            │───────────────────────────│
│                                             │ ▶ ● zephyr-sdk   P2 carve │
│                                             │ ◆ ○ benchctrl    PDU FUI  │
│                                             │ ↻ – ctxswitch    footer   │
│                                             │ ⇢ ● devbox/vllm   bench    │
│                                             │───────────────────────────│
│                                             │ RECAP  you owe it a y/n   │
│                                             │ on the TLS fallback…      │
│                                             │ LAST EXCHANGE …           │
└─────────────────────────────────────────────┴───────────────────────────┘
```

## What it does
- **Watches every session on every machine.** It reads Claude Code and pi transcripts as they are written, so
  capture is automatic. Nothing depends on an agent remembering to call a tool.
- **Tracks the human↔agent conversation, not the code.** For each session it keeps your last prompt, the agent's
  last reply, the questions it asked and your verbatim answers, the plan approvals, a tool tally, and whether it
  is busy, idle, waiting on you or ended.
- **Puts what needs you on top.** An attention strip lists open questions, plan approvals, permission prompts
  ("allow Bash: rm -rf build") and pi confirm dialogs, oldest wait first. Each one clears itself when you answer.
- **Writes a one-line recap per session**, so you know what matters right now. The recap comes from a *local*
  LLM seat. Nothing leaves your network.
- **Switches sessions for you.** Inside tmux, Enter swaps a session into the main pane, resumes an ended one, or
  says why it can't (running in another terminal, or on another host). `c` / `p` start a new Claude / pi session.
  When an agent exits, the next one takes its place.
- **One source of truth.** A central hub holds all state. Every TUI on every machine shows the same thing,
  filterable by this host, a named host or all hosts.

## How it works
```
 each machine                         hub (one, e.g. a docker host)          any machine
 ctx-collector ── HTTP POST ───────►  ctx-hub: SQLite + REST API   ◄──────  ctx tui / ctx ls / ctx show
   watches ~/.claude/projects          recap tracker ──► local LLM seat
   and ~/.pi/agent/sessions            (OpenAI-compatible endpoint)
   + live-session registries
   + hook spool (Claude hooks,
     pi extension)
```
- **Collectors only connect outbound.** A machine that blocks inbound traffic still takes part fully. While
  the hub is unreachable, a local queue buffers events and replays them idempotently.
- **Deterministic first.** Anything the harness states outright (questions, approvals, idle-since, the last
  prompt) is parsed without a model, and a model never overwrites it. The LLM only adds interpretation, such as
  the recap.
- The design, phases and verified harness facts are in [DESIGN.md](DESIGN.md). Protocol spikes are in
  [spikes/SPIKES.md](spikes/SPIKES.md).

## Status
Working and in daily use. Phase 1 (hub, collectors, CLI/TUI on hard signals) is done, plus the tmux manager and
LLM recaps. Phase 2 is mostly done: Claude hooks and a pi extension report permission prompts and dialogs, and
`ctx install` / `ctx doctor` wire up and check a host. Still to come: a resume briefing, Windows-native
sessions, messaging live sessions, cross-host resume and MCP. See the phases in [DESIGN.md](DESIGN.md).

# User guide

## 1. Install

### One machine (try it out)
Needs Python ≥ 3.12, [uv](https://docs.astral.sh/uv/) and, for the manager, tmux.
```sh
git clone https://github.com/rickmellor/ctxswitch.git ~/repos/ctxswitch && cd ~/repos/ctxswitch
uv sync --extra test
.venv/bin/ctx-hub &                 # hub on http://127.0.0.1:7433, db in ~/.local/share/ctxswitch/hub.db
.venv/bin/ctx-collector --once      # index your transcripts, push them, exit
.venv/bin/ctx ls                    # you should see your recent sessions
```
Put `.venv/bin` on your `PATH` (or symlink `ctx` into `~/.local/bin`) so plain `ctx` works.

### Several machines
1. **Pick a hub host.** It must be reachable from every other machine. Docker is the easy route:
   ```sh
   cat > deploy/.env <<'ENV'                      # untracked; stays on this host
   CTXSWITCH_HUB_TOKEN=<a long random string>
   CTXSWITCH_SEAT_URL=http://<llm-host>:4000/v1   # optional: OpenAI-compatible endpoint for recaps
   ENV
   docker compose -f deploy/compose.yml up -d --build
   ```
   The SQLite db lives in the `ctxswitch-data` volume and survives rebuilds.
2. **On every machine with agent sessions**, clone the repo to `~/repos/ctxswitch` and run
   ```sh
   deploy/install-collector.sh --hub http://<hub-host>:7433 --token <the hub token>
   ```
   It installs uv if needed, builds the venv, then runs `ctx install` (see below). The host's short name
   (`host_id`) defaults to its hostname; change it in `~/.config/ctxswitch/config.toml` if you like.
   Run `loginctl enable-linger $USER` so the collector keeps running when you log out.
3. **Check:** `ctx doctor` on each machine, and `ctx hosts` to see every collector the hub has heard from.

### What `ctx install` changes
| Part | Change | Undo |
|---|---|---|
| config | writes `~/.config/ctxswitch/config.toml` (mode 600, it holds the token) if `--hub`/`--token` are given or it's missing | delete it |
| collector | writes `~/.config/systemd/user/ctx-collector.service` pointing at this venv, enables and restarts it | `ctx uninstall --collector` |
| Claude Code | adds one `async` hook per event to `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR`), keeping your other hooks; backs the original up to `~/.local/state/ctxswitch/backup/` | `ctx uninstall --claude` restores the original byte-for-byte, or removes just our entries if the file changed since |
| pi | symlinks `~/.pi/agent/extensions/ctxswitch.ts` to the extension in this repo | `ctx uninstall --pi` |

Pick parts with `--claude`, `--pi`, `--collector`; `--dry-run` shows what would change. Running it again is
safe. New pi sessions load the extension; restart running Claude sessions to be sure they have the hooks.

The hooks never block or slow an agent: they run in the background, exit 0 whatever happens, and only write a
small file to `~/.local/state/ctxswitch/spool/` (tool calls are only recorded while a permission prompt is
open). Without hooks ctxswitch still works from transcripts alone; you just don't see permission prompts.

Collectors only make outbound connections, so a machine behind a firewall that drops inbound traffic works
fine. If the hub goes away, collectors queue locally and catch up when it comes back.

## 2. Daily use: the tmux manager
```sh
ctx tmux             # create or re-attach the "ctx" tmux session
ctx tmux --width 40  # TUI width as columns (default 25%)
```
You get one tmux window with two panes: **the session you're working in on the left**, the **board on the
right**. Every agent session lives in its own tmux window behind the scenes, and the board swaps whichever one
you pick into the left pane. Nothing is killed when you switch, and everything keeps running if your terminal
closes. `ctx tmux` brings it all back.

Move focus between the panes with tmux's prefix + arrow (or the mouse, if tmux mouse mode is on).

## 3. Reading the board
From top to bottom:

**Attention strip.** Up to four things that need you, oldest first: questions the agent asked, plan approvals
and sessions sitting idle at a prompt. "nothing needs you" when it's empty. The border shows the current host
filter and how many sessions are live.

**Session list.** One row per session, live ones first:
```
●  3m ◆ cc·laptop zephyr-sdk@feat/x  P2 carve: interface review ◐4m
│  │  │ │        │                   │                          └ an open question, and how long it has waited
│  │  │ │        │                   └ title (or your last prompt)
│  │  │ │        └ repo@branch (hidden when the pane is very narrow)
│  │  │ └ harness·host (cc = Claude Code, pi = pi, cw = Windows Claude; host is dropped when filtered to one)
│  │  └ what Enter can do (below)
│  └ time in the current state (live) or since last activity (ended)
└ state
```

| State | Meaning |
|---|---|
| `●` green | busy: the agent is working |
| `◐` yellow | waiting on you: an open question, plan approval, permission prompt or dialog |
| `○` cyan | idle: at the prompt, your turn |
| `$` | Claude Code reports a shell state |
| `◌` | live status unknown |
| `–` dim | ended (hidden until you press `e`) |

| Reach | Enter… |
|---|---|
| `▶` | it's already on the left |
| `◆` | brings it to the left pane (it's in ctx's tmux) |
| `↻` | resumes it (`claude --resume` / `pi --session`) in a new window, then brings it left |
| `⊘` | can't: it's running in a terminal outside tmux. Quit it there, then Enter resumes it here |
| `⇢` | can't: it's on another host. The detail pane says which. Open it there |

**Detail pane** for the selected session:
- the title, state and age, number of exchanges, and when you last looked (`seen 2h`);
- what Enter will do with it, the session key, repo, working directory, model and permission mode;
- **WAITING ON YOU**: every open question, approval or prompt, verbatim, with how long it's been waiting.
  Kinds: `question`, `plan`, `permission` (Claude wants to run a tool), `elicitation` (an MCP server wants
  input), `prompt` (a pi dialog), `plain` (its reply ended in a question);
- **RECAP**: one line on where it stands and what it needs from you (see below);
- **LAST EXCHANGE**: your last prompt and the agent's last reply, plus a tool tally for that turn;
- **DECISIONS**: the questions it asked and the answers you gave, verbatim;
- **EARLIER**: the few exchanges before that.

## 4. Keys
| Key | Action |
|---|---|
| `↑` `↓` | select a session (the detail pane follows) |
| `Enter` | open it: swap it in, resume it, or say why not (see Reach) |
| `c` / `p` | start a new Claude Code / pi session. You're asked for a directory (default: the selected session's) |
| `h` | host filter: this host → all → each other host |
| `/` | filter by text (title, repo, key, last prompt). `Esc` clears |
| `e` | show or hide ended sessions |
| `v` | mark the selected session viewed (it's also marked whenever you open it, on any machine) |
| `r` | refresh now (the board also updates live over a WebSocket) |
| `q` | detach: close your terminal, keep everything running |
| `Q` | quit all: end the tmux session and every agent in it, after a y/n |

The footer shows as many keys as fit; the rest still work.

## 5. Opening and closing sessions
- **Sessions started by hand inside the ctx tmux session** are found automatically (by matching the agent's pid
  to its pane) and marked `◆`.
- **Sessions in other terminals** (`⊘`) can't be adopted, because their terminal owns the process. Quit them
  there and press Enter here to resume them inside tmux.
- **When an agent exits** (`/exit`, `quit`, Ctrl-D), its window closes and the most recently active other agent
  takes the left pane, or a plain shell if none is left. If the agent failed or exited within 5 s, the window
  waits for Enter first so you can read the error.
- **Outside tmux** (`ctx tui` on its own), Enter opens the session in a new Windows Terminal tab under WSL, or
  shows the command to run.
- **Q** ends everything in the ctx tmux session. The transcripts stay, so every session comes back as `↻`.

## 6. Recaps
The hub writes a one-line recap per session with a **local** LLM (any OpenAI-compatible endpoint that supports
`response_format` json_schema; set it as `[[seats]]` in the hub's config or `CTXSWITCH_SEAT_URL`). A recap is
regenerated a few seconds after a session's latest exchange settles. It's shown as `(updating…)` until then.
Only sessions that are live or were active in the last 7 days get one. If the endpoint is down, the board works
as normal without recaps. Turn them off with `ctx-hub --no-recap`.

## 7. Command line
| Command | Shows |
|---|---|
| `ctx ls [--host H \| --all] [--live]` | one line per session (default: this host) |
| `ctx show <key-or-title-fragment>` | one session in full: recap, open asks, decisions, recent exchanges (marks it viewed; `--no-mark` to not) |
| `ctx attention [--all]` | what needs you, oldest wait first |
| `ctx hosts` | collectors the hub knows, and when each last checked in |
| `ctx tui [--host H \| --all]` | the board without tmux |
| `ctx tmux [--width N\|N%]` | the manager |
| `ctx install [--claude --pi --collector] [--hub URL --token T] [--dry-run]` | wire this machine up (above) |
| `ctx uninstall [--claude --pi --collector]` | undo it (the collector only when named) |
| `ctx doctor` | check this machine: config, hub and token, collector, hooks, extension, spool, recaps. Exit 1 if broken |

## 8. Configuration
`~/.config/ctxswitch/config.toml` (or `$CTXSWITCH_CONFIG`). [config.example.toml](config.example.toml) has every
option. The ones you'll touch:

| Setting | |
|---|---|
| `[hub] url`, `token` | where collectors push and clients read; must match the hub's token |
| `[collector] host_id` | short stable name for this machine (default: hostname) |
| `[collector] claude_roots`, `pi_roots` | transcript directories to watch |
| `[collector] claude_win_roots` | WSL only: Windows-native Claude transcripts under `/mnt/c/Users/<you>/.claude/projects` (read-only) |
| `[collector] backfill_days` | how far back to index on first run (default 30) |
| `[[seats]]` | hub only: the LLM endpoint for recaps |
| `CTXSWITCH_TMUX_SESSION` | env: tmux session name for the manager (default `ctx`) |

## 9. Troubleshooting
Start with **`ctx doctor`**: each line is ✓, ! (works, but look) or ✗ (broken), with the fix underneath.

| Symptom | Check |
|---|---|
| a permission prompt doesn't show | `ctx doctor` (claude hooks line); restart that Claude session after installing |
| an answered prompt still shows | it clears on the agent's next tool call, turn end or idle; check the collector is running |
| `hub unreachable` | `[hub] url` in your config, and that the hub is up (`curl <url>/v1/health`) |
| a host is missing | `ctx hosts`; on that host, `systemctl --user status ctx-collector` and `~/.local/state/ctxswitch/collector.log` |
| a live session shows `⊘` | it runs outside the ctx tmux session. Quit it there and resume it with Enter |
| exiting an agent left a shell on the left | it heals on the next refresh. If it keeps happening, see `~/.local/state/ctxswitch/tmux-exit.log` |
| no recaps | the hub log (`docker logs ctxswitch-hub`) shows `recap … failed` lines if the seat is unreachable |
| a TUI change didn't show up | restart it: `q`, then `ctx tmux` |

## Tests
```sh
.venv/bin/python -m pytest -q     # the tmux tests run against a private tmux server and skip if tmux is missing
```
