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
2. **LLM calls stay local.** The recap and ledger trackers call an OpenAI-compatible seat on the LAN (`[[seats]]` in
   config). Never add a cloud model call.
3. **Deterministic first.** Facts the harness states outright (asks, answers, plan approvals, live/idle,
   last prompt) are parsed in code, and model output never overwrites them.
4. **Recaps are regenerated from source**, never by editing the previous recap. On seat failure, keep the old
   one and back off. **Ledger rows are extracted, never rewritten**: each cites its turn and a verbatim quote that
   must be found in the text, and rows are retired, not deleted.
5. **Capture is automatic.** Nothing may depend on an agent or the operator remembering to do something.
6. **Nothing destructive to sessions without a confirmation.** `Q` (kill the tmux session) asks y/n. Swapping
   panes never kills a running agent.

## Layout
```
src/ctxswitch/
  common/config.py        Config dataclasses + TOML load (~/.config/ctxswitch/config.toml or $CTXSWITCH_CONFIG)
  common/schema.py        shared record shapes
  collector/
    main.py               loop: initial scan → watchfiles + 5 s liveness poll + 10 min sweep → outbox → push; inbox long-poll
    deliver.py            write a message line into a Claude inbox / pi extension socket
    indexer.py            per-transcript incremental parse (byte offsets), emits turn/session events
    adapters/claude.py    Claude Code transcript → turns, asks (AskUserQuestion, ExitPlanMode, plain "?")
    adapters/pi.py        pi v3 session tree → turns
    liveness.py           Claude ~/.claude/sessions/<pid>.json + pi registry → busy/idle (verifies pid start)
    state.py              local SQLite outbox + offsets (idempotent replay)
    gitinfo.py            repo/branch labels; branch() reads HEAD for pi, which records none
    spool.py              drains the hook spool into the outbox as "hook" events
  hub/
    main.py               ctx-hub entry; wires the Recapper into the Starlette lifespan
    api.py                REST /v1/{ingest,sessions,sessions/<key>[/viewed|/messages],messages/<id>/{status,cancel},outbox,attention,hosts,health} + /v1/ws
    store.py              SQLite (WAL); schema migrations are ALTER TABLE in place
    hooks.py              hook events → permission / elicitation / prompt asks and their resolution
    tracker/recap.py      one-line recap + re-entry card per session; anchor = last turn idx:complete:open_asks:live_status
    tracker/extract.py    ledger: one seat call per exchange you typed → instructions, decisions, claims, asks, purpose
    tracker/scheduler.py  the one loop that runs both (recap first)
  client/
    cli.py                `ctx` subcommands (ls, show, send, reply, attention, hosts, tui, tmux, internal tmux-exit)
    hubclient.py          HubClient(cfg): constructed from the Config, not a URL
    tmux.py               pane discovery/tagging, swap-in, resume, agent-exit hand-off, reach classification
    tui/app.py            Textual app (the board)
    fmt.py                shared text formatting (glyphs, ages, titles), reply_target, since_viewed, workstream_label
    notify.py             tmux pop-ups: Notifier diffs the attention list per TUI
    install.py            `ctx install` / `uninstall` / `doctor`
  hook.py                 ctx-hook: Claude hook → spool file (stdlib only, runs on every hooked event)
  pi_ext/ctxswitch.ts     pi extension: live registry + UI-prompt events + message socket (symlinked into ~/.pi/agent/extensions)
deploy/                   hub Dockerfile + compose, install-collector.sh (uv bootstrap, then `ctx install`)
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
  `ssh` ↗ (ended on another host, resumable over ssh), `remote` ⇢ (live elsewhere, or no route), `none`. `tmux._reach` is the single classifier, and `show()` follows it.
- **Hooks (Phase 2):** Claude `async` hooks run `ctx-hook claude`; the pi extension writes the same record. Each
  event is one file in `~/.local/state/ctxswitch/spool/`, which the collector watches, queues and deletes. The hub
  (`hub/hooks.py`) opens an ask on PermissionRequest / permission Notification / elicitation / pi `ui_prompt_start`
  and resolves it on PostToolUse, PermissionDenied, Stop, UserPromptSubmit, SessionEnd or `ui_prompt_end`. Each
  event applies once (`hook_seen`). Esc on a permission prompt fires **no** hook: the registry's `idle` closes it.
  Tool events are spooled only while `spool/.pending/<sid>` exists, so ordinary tool calls cost one tiny process.
- **Briefing:** `hub/brief.py` builds the card; `ctx-hook brief claude` (SessionStart, `matcher: resume`,
  synchronous) and `ctx-hook brief pi <sid>` (run by the pi extension) fetch it with a 2 s timeout and print
  nothing on any failure. Turn text comes straight from the parser, so fix bad text there, not in the card.
- **Windows Claude:** harness `claude-win`, WSL only (`common/wsl.py`). Polled every 30 s by `Collector.windows_loop`
  (`Indexer.poll`), never watched. `tasklist.exe` runs in a thread; all collector state stays on the event loop.
- **Live states:** `LIVE` in `common/schema.py` (busy, waiting, idle, shell). `waiting` comes straight from
  Claude's registry while a dialog is up. Don't hardcode the list.
- **Install:** `ctx install` merges into `settings.json` (our entries are found by the string `ctx-hook`), keeps the
  pre-install original, and `uninstall` restores it byte-for-byte if nothing else changed the file since.
  Files are chowned to HOME's owner only when running as real root for someone else's HOME.
- **Recapper:** a hub asyncio task. A session is recapped once its anchor has held still for 4 s. Only sessions
  that are live or active in the last 7 days are recapped. Seat requests use `response_format` json_schema
  (strict) and `chat_template_kwargs.enable_thinking=false` (Qwen seats otherwise spend the budget on thinking).
- **Ledger extractor:** `tracker.through_idx` per session is the last exchange read; `apply_extraction` is
  idempotent on it. `validate()` drops any row whose quote isn't in its own side of the exchange; that check is the
  only defence against invented rows, so don't loosen it without re-running a calibration. LLM asks have ref
  `llm:<idx>:<n>` and are closed by `store.typed()` prompts (not task/peer turns or `<bash-input>` wrappers).

- **Messages:** `store.messages` rows; the hub only lists what's queued for a host's live sessions
  (`store.outbox`), and the collector delivers only to sockets in its own registry (`Collector.deliver_one`,
  `msg:<id>` in its state = at most once). `store._match_message` runs first in `upsert_turns`: a `sent` message
  whose normalised body (first 200 chars) is in a turn's prompt marks it delivered and rewrites the turn as yours;
  the turn completing marks it replied. Only `sent` messages match, so the same text typed by hand isn't claimed.
- **Quick-reply:** a message with `ask_ref` (`answers` in the API), checked by `store.open_ask` and
  `REPLYABLE` (`plain`, `text`) when sent. It adds no closing path: the collector / `_close_llm_asks` close the
  ask as for any prompt; `upsert_turns` then swaps the wrapped Claude text for your body on plain asks closed by
  a `via=ctx` turn, and the asks upsert keeps that answer on replay (same `answered_at`).
- **Since you looked / workstreams:** `_session_row` adds `unseen` + `seen_through` from `last_viewed_at`;
  `workstream_of` (owner/name@branch, computed at read time) and `Store.siblings` feed SAME WORK ELSEWHERE and
  the `w` grouping (header rows keyed `ws:<id>`).

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
  `docker compose -f deploy/compose.yml up -d --build`. Hub secrets and the seat URL live in an untracked
  `deploy/.env` on the hub host (`CTXSWITCH_HUB_TOKEN`, `CTXSWITCH_SEAT_URL`). The SQLite db lives in the `ctxswitch-data` volume and
  survives rebuilds.
- **Collectors:** `git pull && deploy/install-collector.sh` (it runs `ctx install`, which restarts the unit),
  then `ctx doctor`. Logs are in `~/.local/state/ctxswitch/collector.log`. A new pi extension takes effect in
  new pi sessions; new Claude sessions get the hooks (already-running ones: unverified, restart to be sure).
- **TUI:** clients run from the repo venv. Restart `ctx tui` (q, then `ctx tmux`) to pick up changes.

## What's next
Phases 2 and 3 are done. Unverified: hook/extension payloads on harness versions newer than `VALIDATED`, and the Windows
session registry with a live Windows session. Cross-host resume over ssh is done (`[ssh]` client config; `tmux.ssh_argv`). Messaging is done (`m`, `M` = the tmux paste buffer, `ctx send`), verified end to end on Claude 2.1.280 and
pi 1.2.0. Phases 4 and 5 are done (quick-reply `a`/`ctx reply`, tmux pop-ups, since-you-looked, workstreams). Next: MCP (Phase 6). Validated harness versions are in `client/install.py` `VALIDATED`; `ctx doctor` warns on others.
