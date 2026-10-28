# ctxswitch

Live, always-on augmented short-term memory for an operator running many coding-agent sessions
(Claude Code and pi) across several machines. One hub, one collector per host, any number of TUIs that all show
the same state. See [DESIGN.md](DESIGN.md) and [spikes/SPIKES.md](spikes/SPIKES.md).

## Status
Phase 1: hub + collector + CLI/TUI on **hard signals** (no LLM yet): live/idle/ended per session, last exchange,
tool tally, open asks (AskUserQuestion, plan approvals, plain questions), decisions with the operator's verbatim
answers, attention queue.

## Quick start (single machine)
```sh
uv sync --extra test
.venv/bin/ctx-hub &                       # http://127.0.0.1:7433, db in ~/.local/share/ctxswitch/hub.db
.venv/bin/ctx-collector --once            # index ~/.claude/projects + ~/.pi/agent/sessions, push, exit
.venv/bin/ctx ls                          # one line per session on this host
.venv/bin/ctx show <key-or-title-prefix>  # ledger of one session
.venv/bin/ctx attention                   # what needs you
.venv/bin/ctx tui                         # the board: h host filter · e ended · v viewed · enter resume · / filter
```
Run `ctx-collector` without `--once` to keep watching.

## tmux manager (recommended)
```sh
ctx tmux            # tmux session "ctx": agent pane on the left, `ctx tui` (25% width) on the right; attaches
ctx tmux --width 40 # or a fixed column count
```
Every agent session lives in its own tmux window, so it outlives your terminal client. In the TUI, **Enter** on a
session swaps its pane into the left pane (`tmux swap-pane`) — the previous occupant moves back out to its own
window, nothing is killed. Sessions you start by hand inside tmux are found automatically (agent pid → pane);
ended sessions are resumed in a new window first. **c** / **p** start a new Claude / pi session (asks for a
directory, default = the selected session's) and bring it to the left. `▶` marks the session currently on the left. Prefix+arrow moves
focus back to the TUI. Set `CTXSWITCH_TMUX_SESSION` to use another tmux session name. Copy `config.example.toml` to `~/.config/ctxswitch/config.toml`
to point at a remote hub and name this host.

## Multi-machine
Hub: `deploy/compose.yml` on gpu-host (port 7433, volume for the SQLite db). Collectors: `deploy/ctx-collector.service`
as a `systemd --user` unit on each host (`loginctl enable-linger $USER` so it survives logout). Collectors only
connect outbound, so a host that blocks inbound traffic (the laptop) still participates fully.

## Tests
```sh
.venv/bin/python -m pytest -q
```
