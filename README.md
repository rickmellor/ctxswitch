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
- **Puts what needs you on top.** An attention strip lists open questions and plan approvals, oldest wait first.
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
LLM recaps. Hooks, a pi extension, messaging live sessions, cross-host resume and MCP are still to come. See
the phases in [DESIGN.md](DESIGN.md).

## Quick start (one machine)
Needs Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).
```sh
uv sync --extra test
.venv/bin/ctx-hub &                       # http://127.0.0.1:7433, db in ~/.local/share/ctxswitch/hub.db
.venv/bin/ctx-collector --once            # index ~/.claude/projects + ~/.pi/agent/sessions, push, exit
.venv/bin/ctx ls                          # one line per session on this host
.venv/bin/ctx show <key-or-title-prefix>  # one session's ledger: recap, open asks, decisions, exchanges
.venv/bin/ctx attention                   # what needs you
.venv/bin/ctx tmux                        # the manager (below)
```
Run `ctx-collector` without `--once` to keep watching. Copy `config.example.toml` to
`~/.config/ctxswitch/config.toml` to point at a remote hub, name the host and configure the LLM seat.

## tmux manager (recommended)
```sh
ctx tmux            # tmux session "ctx": agent pane on the left, `ctx tui` (25% width) on the right; attaches
ctx tmux --width 40 # or a fixed column count
```
Every agent session lives in its own tmux window, so it outlives your terminal client. In the TUI, **Enter** on a
session swaps its pane into the left pane (`tmux swap-pane`). The previous occupant moves back out to its own
window; nothing is killed. Sessions you start by hand inside tmux are found automatically (agent pid → pane).
Ended sessions are first resumed in a new window. **c** / **p** start a new Claude / pi session (asks for a
directory, default = the selected session's) and bring it to the left. When an agent that ctx started or resumed
exits, its window closes and the most recently active other agent takes the left pane (a plain shell if none is
left). A failed run, or one shorter than 5 s, waits for Enter first so the error stays readable. Prefix+arrow
moves focus back to the TUI.

**q** detaches: your terminal closes, but the TUI and every agent keep running, and `ctx tmux` brings it all
back. **Q** ends the whole tmux session, every agent in it included, after a y/n prompt. Their transcripts stay,
so they come back as `↻`.

The column before each session says what Enter can do with it:

| | |
|---|---|
| `▶` | on the left now |
| `◆` | in ctx's tmux. Enter brings it to the left pane |
| `↻` | not running. Enter resumes it in a new window |
| `⊘` | running in a terminal outside tmux and can't be adopted. Quit it there, then Enter resumes it here |
| `⇢` | on another host. Open it there |

Other keys: `h` cycles the host filter, `/` filters, `e` shows ended sessions, `v` marks a session viewed, `r` refreshes.
Set `CTXSWITCH_TMUX_SESSION` to use another tmux session name.

## Multi-machine
- **Hub:** `docker compose -f deploy/compose.yml up -d --build` from the repo root on the hub host (port 7433,
  SQLite in a named volume). Set `CTXSWITCH_HUB_TOKEN` to require a bearer token.
- **Collectors:** `deploy/install-collector.sh` on each host. It installs uv if needed, syncs the venv, writes a
  config stub and enables a `systemd --user` unit. Run `loginctl enable-linger $USER` so the collector survives
  logout. `ctx hosts` lists the collectors the hub has heard from.

## Tests
```sh
.venv/bin/python -m pytest -q     # the tmux tests run against a private tmux server and skip if tmux is missing
```
