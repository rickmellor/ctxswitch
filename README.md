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
Run `ctx-collector` without `--once` to keep watching. Copy `config.example.toml` to `~/.config/ctxswitch/config.toml`
to point at a remote hub and name this host.

## Multi-machine
Hub: `deploy/compose.yml` on gpu-host (port 7433, volume for the SQLite db). Collectors: `deploy/ctx-collector.service`
as a `systemd --user` unit on each host (`loginctl enable-linger $USER` so it survives logout). Collectors only
connect outbound, so a host that blocks inbound traffic (the laptop) still participates fully.

## Tests
```sh
.venv/bin/python -m pytest -q
```
