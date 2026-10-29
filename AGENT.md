# AGENT.md: orientation for coding agents

Read this before changing anything. [README.md](README.md) covers what the tool is for and how it is used.
[DESIGN.md](DESIGN.md) covers the target design and phases. **Where DESIGN.md and the code disagree, the code
is the truth.** For example, DESIGN.md describes a collector WebSocket, but collectors actually push with plain
HTTP `POST /v1/ingest`.

## The product in one paragraph
ctxswitch is a live memory of the **human↔agent conversation** across many Claude Code and pi sessions on
several machines. It is not a summary of code work and not a cold-start primer. Collectors on each host parse
transcripts and push to one hub. The hub stores sessions and turns in SQLite and serves a REST API. TUIs and the
CLI on any host read from the hub. The TUI runs as a narrow tmux side pane (about 40 columns), so everything it
shows must survive that width.

## Invariants: don't break these
1. **Collectors connect outbound only.** One host blocks all inbound traffic. The hub must never need to reach
   a collector.
2. **LLM calls stay local.** The recap tracker calls an OpenAI-compatible seat on the LAN (`[[seats]]` in
   config). Never add a cloud model call.
3. **Deterministic first.** Facts the harness states outright (asks, answers, plan approvals, live/idle,
   last prompt) are parsed in code, and model output never overwrites them.
4. **Recaps are regenerated from source**, never by editing the previous recap. On seat failure, keep the old
   one and back off.
5. **Capture is automatic.** Nothing may depend on an agent or the operator remembering to do something.
6. **Nothing destructive to sessions without a confirmation.** `Q` (kill the tmux session) asks y/n. Swapping
   panes never kills a running agent.

## Layout
```
src/ctxswitch/
  common/config.py        Config dataclasses + TOML load (~/.config/ctxswitch/config.toml or $CTXSWITCH_CONFIG)
  common/schema.py        shared record shapes
  collector/
    main.py               loop: initial scan → watchfiles + 5 s liveness poll + 10 min sweep → outbox → push
    indexer.py            per-transcript incremental parse (byte offsets), emits turn/session events
    adapters/claude.py    Claude Code transcript → turns, asks (AskUserQuestion, ExitPlanMode, plain "?")
    adapters/pi.py        pi v3 session tree → turns
    liveness.py           Claude ~/.claude/sessions/<pid>.json + pi registry → busy/idle (verifies pid start)
    state.py              local SQLite outbox + offsets (idempotent replay)
    gitinfo.py            repo/branch labels
  hub/
    main.py               ctx-hub entry; wires the Recapper into the Starlette lifespan
    api.py                REST /v1/{ingest,sessions,sessions/<key>[/viewed],attention,hosts,health} + /v1/ws live updates
    store.py              SQLite (WAL); schema migrations are ALTER TABLE in place
    tracker/recap.py      one-line recap per session; anchor = last turn idx:complete:open_asks:live_status
  client/
    cli.py                `ctx` subcommands (ls, show, attention, hosts, tui, tmux, internal tmux-exit)
    hubclient.py          HubClient(cfg): constructed from the Config, not a URL
    tmux.py               pane discovery/tagging, swap-in, resume, agent-exit hand-off, reach classification
    tui/app.py            Textual app (the board)
    fmt.py                shared text formatting (glyphs, ages, titles)
deploy/                   hub Dockerfile + compose, collector systemd unit + installer
spikes/                   Phase 0 findings (Claude inbox socket, pi injection, seat behaviour)
tests/                    pytest; transcript fixtures are synthetic (tests/claude_rows.py), never real content
```

## Key mechanics
- **Session key** = `host_id/harness/session_id`. `last_viewed_at` is global: viewing on any TUI marks it.
- **tmux model:** session `ctx` (`$CTXSWITCH_TMUX_SESSION`). Window `mgr` holds pane 0 (the *viewer*) and the TUI.
  Each agent has its own window and is swapped into the viewer with `swap-pane`. Panes carry the options
  `@ctx_key` (session key) and `@ctx_role` (`tui` | `agent`). A hand-started agent is matched to its pane by
  walking pids.
- **Agent exit:** windows ctx creates run `sh -c '<agent>; ctx tmux-exit --rc $? --started $start || exec $SHELL'`.
  `tmux.on_agent_exit` hands the viewer to the most recently active other agent, or a fresh shell, and closes the
  window. `reap_viewer` is the safety net for an agent pane that dropped to a shell anyway. `SHELLS` deliberately
  excludes `sh`, because `sh` is the wrapper itself. Failures are logged to `~/.local/state/ctxswitch/tmux-exit.log`.
- **Reach** (what Enter can do): `pane` ◆, `resume` ↻, `foreign` ⊘ (live in a terminal outside tmux),
  `remote` ⇢ (other host), `none`. `tmux._reach` is the single classifier, and `show()` follows it.
- **Recapper:** a hub asyncio task. A session is recapped once its anchor has held still for 4 s. Only sessions
  that are live or active in the last 7 days are recapped. Seat requests use `response_format` json_schema
  (strict) and `chat_template_kwargs.enable_thinking=false` (Qwen seats otherwise spend the budget on thinking).

## Working on it
```sh
uv sync --extra test
.venv/bin/python -m pytest -q        # assert the count in the summary line, not just rc
```
- The tmux tests start a **private** tmux server per test (`-L ctxtest…`) and route `tmux.subprocess.run` to
  it. They never touch a real tmux session.
- **TUI changes:** check them headless at about 40 columns with `App.run_test(size=(40, 24))`, then inspect
  widget regions or `export_screenshot()`. The Footer truncates on the right, so put the important bindings first
  and keep labels short. Inputs in normal flow (not `dock: bottom`) so the footer can't cover them.
- An agent sandbox often can't see the operator's own tmux server, which may run as a different user or socket.
  Reproduce with a private server rather than guessing.
- Keep the style of the surrounding code: dense, short comments only where the reason isn't obvious, and
  dataclasses over dicts at module boundaries.

## Deploying
- **Hub:** copy the tree to the hub host (exclude `.venv .git spikes/out __pycache__`), then run
  `docker compose -f deploy/compose.yml up -d --build`. The SQLite db lives in the `ctxswitch-data` volume and
  survives rebuilds.
- **Collectors:** `git pull && deploy/install-collector.sh`, then `systemctl --user restart ctx-collector`. Logs
  are in `~/.local/state/ctxswitch/collector.log`.
- **TUI:** clients run from the repo venv. Restart `ctx tui` (q, then `ctx tmux`) to pick up changes.

## What's next
Phase 2 in DESIGN.md: Claude hooks (Notification permission prompts aren't in transcripts), a pi extension,
`ctx install`/`doctor`, and a Windows-native read-only adapter. After that come messaging (inject into live
sessions), cross-host resume over ssh, and MCP.
