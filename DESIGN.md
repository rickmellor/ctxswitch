# ctxswitch — design

## Context
Rick runs ~a dozen simultaneous coding-agent sessions (Claude Code and pi), one per terminal tab, across
**three machines**: the work laptop (WSL, plus some Windows-native Claude), **gpu-host** (the local LLM/GPU
host) and **devbox**. The cost is the operator's short-term memory: what each session is for,
what he told it, what he already answered, what it is waiting on — re-loaded every time he tabs back.

ctxswitch is a **live, always-on augmented short-term memory for the operator** — not a primer for someone
starting cold. It watches every session on every machine and tracks the **human↔LLM interface** (not the code
work): goals and how they shifted; instructions given; questions asked and answers given; what each session is
waiting on *right now*. It calls out what needs the operator, lets Rick message live sessions or queue messages
for later, and is fully automatic — capture is transcript watching + a few hooks; nothing depends on an agent
remembering to call a tool. Interpretation runs on local SAINT seats, in parallel.

**One source of truth.** A central hub holds all state; every TUI on every machine shows the same thing, live,
filterable by this machine / a named machine / all. Memory lives on the hub, not the client PC.

Greenfield repo: `~/repos/ctxswitch`.

## Decisions log
- 2026-10-28: hub runs as a docker container on **gpu-host**; Phase 1 built interactively; NAS move is a Phase 7 runbook.
- 2026-10-28 (Phase 2): hooks are `async` command hooks running `ctx-hook claude` (stdlib only, always exit 0) that
  spool one file per event; tool events are spooled only while a permission is pending. Claude's own registry
  reports `status: "waiting"` (+ `waitingFor`) while a dialog is up, so `idle` closes hook asks that no hook
  resolved (Esc on a permission prompt fires none). The pi registry is authoritative, so a vanished pi session ends.
- 2026-10-29 (Phase 2): the resume briefing is built on the hub (`hub/brief.py`, `GET /v1/sessions/<key>/brief`)
  from hard signals only, and shown to the operator, not the model: Claude via a synchronous SessionStart hook
  (`matcher: resume`) printing `systemMessage` (async hook output is dropped), pi via `ui.notify`. `claude --resume`
  keeps the session id; `pi --session` / `pi -c` report `reason: "startup"`, so pi detects a resume by an existing
  leaf entry. Nothing is shown under 15 min away, and the hook gives up after 2 s.
- 2026-10-29 (Phase 2): Windows-native Claude is read from WSL as harness `claude-win`. Its roots are polled every
  30 s rather than watched (inotify doesn't fire for Windows-side writes on /mnt/c). Liveness reads the Windows
  `~/.claude/sessions` registry and checks pids with `tasklist.exe` (~1.5 s, run in a thread, only when registry
  files exist; no start-time check, so a reused pid reads live until the next poll). The registry format is
  assumed to match Linux; it was **not** seen with a live Windows session. Windows cwds are mapped to WSL paths for
  repo grouping; resume is a hint only (no injection in v1).
- 2026-10-29 (Phase 3): the ledger tracker (`hub/tracker/extract.py`) reads each completed exchange once, oldest
  first, live sessions first, one seat call per exchange you typed (task notifications, peer messages and
  `<bash-input>`-style wrappers are read past without a call). The prompt carries your active instructions with ids
  so the model can skip repeats and retire what you cancel. Every row needs a verbatim quote from its own side of
  the exchange (instructions/decisions from your prompt, claims/asks from the reply; never from the previous reply)
  or it is dropped; quotes are compared after lowercasing and stripping markdown marks (`` ` ``, `*`), which the
  model drops when copying. Per-exchange caps (3 instructions, 3 decisions, 2 claims, 3 asks) because calibration
  on real sessions showed over-extraction, not under. A plain-text ask is kept only when the collector recorded no
  hard ask for that reply (its "ends with ?" rule already covers most), and is answered by your next typed prompt.
  The purpose line is replaced only when the model flags a goal change; the old one goes to `purpose_history`.
  The recap and the extractor share one scheduler loop (recap first: it is what the board shows), and the recap
  call now also writes the ≤4-line re-entry card and sees your standing instructions. Three unparseable replies
  for one exchange skip it; an HTTP error backs off 30 s without moving past it.
  A decision row records the plain-text asks your prompt answered (`extra.answers`: asks raised since your previous
  typed prompt), so the board shows an answered question once: the ask verbatim, then your choice.
- 2026-10-28: seat calls use `chat_template_kwargs.enable_thinking=false` + `response_format` json_schema (see spikes/SPIKES.md).

## Design principles
1. **Deterministic first.** What the harness states outright (asks, approvals, idle-since, last prompt) is
   extracted without a model and never overwritten by one. The LLM adds interpretation on top.
2. **Append-mostly, source-anchored ledger.** The model *extracts* from each new exchange; it never rewrites
   history. Every row cites `(host, session, transcript line)` + a verbatim quote.
3. **Useful on day 2.** The TUI ships on hard signals before any model is wired in.
4. **Low-volume callouts.** Judgement flags (drift, contradictions) are opt-in and late.
5. **Clients only ever connect outbound.** The laptop black-holes inbound to WSL (CrowdStrike, see memory
   `reference_wsl_inbound_blocked`), so the hub must never need to reach a collector.

## Verified host / harness facts (2026-10-28)
| Host | Reach | Runtimes | Sessions |
|---|---|---|---|
| laptop WSL (this) | inbound blocked; outbound LAN ok; `wt.exe` available | claude 2.1.280, pi 1.2.0, systemd --user running | 257 Claude + 75 pi transcripts |
| laptop Windows-native | files via `/mnt/c/Users/<you>/.claude` | claude | 34 transcripts, last 2026-10-05 |
| gpu-host | ssh ok (id_rsa) | docker, Python 3.12, SAINT gateway :4000, claude/pi not on non-login PATH | 47 Claude + 78 pi |
| devbox | ssh ok (id_ed25519) | docker, Python 3.14, claude + pi | 13 Claude + 10 pi |
| NAS | my ssh keys refused; runs containers (megaplan :8932, mosquitto) | — | — |

**Claude Code 2.1.280**: transcripts `~/.claude/projects/<cwd-slug>/<sessionId>.jsonl` (`cwd`, `gitBranch`,
`ai-title`, `last-prompt`, AskUserQuestion/ExitPlanMode tool_use + results, compaction summaries, sidechains).
Live registry `~/.claude/sessions/<pid>.json` (`status busy|idle`, `statusUpdatedAt`, `messagingSocketPath`,
`procStart`; stale files exist → verify pid + `/proc/<pid>/stat` start). Hooks with `async: true`: SessionStart
(returns `additionalContext`), Notification (permission prompt / `idle_prompt` — **not in transcript**), PreCompact,
SessionEnd. Cross-session inbox socket works on Bedrock, idle receiver starts a turn, **held for approval when the
receiver is in plan/bypass mode**; third-party payload undocumented. Channels: not on Bedrock → out.

**pi 1.2.0**: transcripts `~/.pi/agent/sessions/<cwd-slug>/<ts>_<id>.jsonl` (v3 tree). Extension API:
`session_start`/`session_shutdown`, `turn_end`, `agent_settled`, `pi.sendUserMessage()` (starts a run when idle).

**Seats**: SAINT `http://llm.local:4000/v1` (key `local`); default per Rick = text/chat seat `saint-local-text`
(2 slots, shared with saint-agent `--text` and pi chat). Seat URL/model/concurrency/priority = hub config.

## Core concept: the Operator Ledger (per session)
Rows `{turn_ref, quote, extracted_by: hard|llm, confidence}`; rows are retired, not deleted.

| Field | Source | Content |
|---|---|---|
| `asks[]` | **hard** (AskUserQuestion, ExitPlanMode, Notification) + llm (plain-text "shall I…?") | what the agent waits on; `answered_by` when resolved |
| `decisions[]` | **hard** (AskUserQuestion answers, plan approve/reject + feedback) + llm (plain-text replies) | question → Rick's answer |
| `instructions[]` | llm, from Rick's prompts | standing directives ("don't push", "use the pool"), `active` flag |
| `claims[]` | llm, from agent final text | "done", "tests pass", "blocked on X" |
| `state` | **hard** | working / waiting-on-operator (since) / idle (since) / ended |
| `last_exchange` | **hard** | Rick's last prompt + agent's last final text, trimmed |
| `purpose` | llm, regenerated only on a flagged goal change; history kept | one line |
| `reentry` | llm, per exchange, ≤4 lines | "You're doing X. You told it Y. It's waiting for Z." |
| `flags[]` | llm, **opt-in, Phase 8** | drift, re-asking an answered question, "done" with failing tally, error loop |

Session identity = `(host_id, harness, session_id)`. `last_viewed_at` is per session, global to Rick (viewing on
any TUI marks it) → "since you last looked" is a row diff everywhere.

## Architecture
```
 per host ───────────────────────────────────────────┐        hub (gpu-host container, or NAS later) ───────────────
  ctx-collector (systemd --user / Task Scheduler)    │   ┌──► ingest: turns, liveness, hook events → SQLite (WAL)
   watchfiles transcripts → extractor (hard signals)─┼──►│    tracker scheduler → SAINT seats → llm rows
   Claude registry + pi registration → liveness      │ WS│    attention engine, workstreams, messages, views
   hooks / pi ext → local spool → collector          │   │    REST + WebSocket API (token auth, LAN)
   ◄── commands: inject(session, body), probe, resume│◄──┘         ▲              ▲             ▲
   local SQLite queue while hub unreachable          │        TUI (any host)   CLI ctx     MCP (any host)
```
Python 3.12 + uv everywhere (`textual`, `mcp`, `watchfiles`, `httpx`, `websockets`, `starlette`/`uvicorn`,
sqlite3). pi extension in TS. One package, three entry points: `ctx` (client), `ctx-collector`, `ctx-hub`.

### Collector (per host, outbound only)
- One persistent WebSocket to the hub; pushes extracted turns (prompt verbatim, agent final text, tool tally,
  hard signals, offsets) and liveness heartbeats; receives commands on the same socket (inject, probe,
  open-tab). Raw transcripts stay on the host; the hub can request a transcript excerpt on demand for "jump to
  source".
- Watches local transcript roots; hooks/pi-extension write to a local spool only for events the transcript is
  blind to (Notification, PreCompact, SessionEnd, SessionStart-inject). Local SQLite queue buffers when the hub
  is down; replays with idempotent `(host, session, offset)` keys.
- Laptop WSL collector additionally reads Windows-native Claude transcripts via `/mnt/c/...`; liveness via
  `tasklist.exe`; **no injection into Windows sessions in v1** (named pipe + token).
- Install: `ctx install` on each host — systemd --user unit (gpu-host/devbox report `degraded` but running;
  verify units start on each), hooks merged into `~/.claude/settings.json`, pi extension linked. Laptop WSL:
  systemd --user is running; a Task Scheduler/wsl.exe fallback is documented if WSL idles out.
- Backfill: hard signals for all existing transcripts on first run; no LLM over history.

### Hub (one instance)
- Store: `hosts`, `sessions`, `turns`, `ledger_rows`, `ledger_meta`, `workstreams`, `messages`, `views`,
  `seat_stats`, `collector_queue_ack`. SQLite WAL on a persistent volume; nightly copy to the NAS.
- Tracker: extraction-only per exchange (+ current short lists of active instructions/open asks for de-dup and
  linking); per-session serialised, coalesced, prioritised (just-went-waiting > busy); per-seat concurrency;
  shared seats lowest priority with backoff; `response_format` json_schema if the gateway passes it, else
  prompt-side schema + repair. LLM rows only for sessions live or active in the last N days (default 7) or opened.
- Attention: open hard ask, idle_prompt, agent claim of done/blocked. Sorted by wait; dedup per item; the hub
  emits `attention.new` events, each client decides whether to toast (config: laptop yes, others no).
- Workstreams: auto-group by `(repo_remote_or_root, branch)` — **across hosts**, since the same repo is cloned
  on several machines; sibling-session instructions shown labelled with host+session, never applied.
- Messaging: `queued → sent → delivered → replied | failed`. Live pi: command to that host's collector →
  extension socket → `pi.sendUserMessage`; reply = text at next `agent_settled`. Live Claude: collector → inbox
  socket (payload from spike), best-effort (plan/bypass receivers hold for approval; TUI shows deliverability
  before sending). Ended/workstream: delivered via SessionStart `additionalContext` / pi `session_start` on the
  host where it resumes. Quick-reply to an ask = message + decision row.
- Resume/focus from any TUI: same host → `wt.exe -w 0 nt … claude --resume <id>` (WSL) or new terminal;
  other Linux host → `ssh -t <host> claude --resume <id>` in a new local tab; laptop sessions from another
  host → not reachable (inbound blocked) → TUI says so and shows the command to run there.
- API: REST for queries/mutations, WebSocket for live updates; bearer token from config; LAN only (bind to
  the LAN interface; optional TLS later). Clock skew: store transcript timestamps + hub receipt time.

### Clients
- **TUI** `ctx tui` (any host): machine filter (`this` / `<host>` / `all`, key `h`); attention queue on top;
  workstreams → sessions (host badge, harness, ●busy/◐waiting/○idle/–ended, waiting-since, purpose); right =
  re-entry card, asks, instructions, decisions, claims, "since you last looked"; bottom = message thread.
  Keys: `a` answer ask, `m` message, `Enter` resume/focus, `/` search, `v` mark viewed, `e` edit/retire a row.
- **MCP** `ctx mcp` (stdio on each host, talks to the hub) + skill: `list_sessions`, `get_ledger`,
  `search_decisions`, `send_message`, `read_inbox`. Optional.
- **CLI**: `ctx ls [--attention] [--host X|--all] | show | send | resume | install | uninstall | doctor`;
  `doctor` checks collector↔hub link, hooks, extension, seat reachability, Claude version vs validated payload.

## Repo layout
```
ctxswitch/  pyproject.toml README.md DESIGN.md config.example.toml deploy/{hub.Dockerfile,compose.yml,install-collector.sh}
  src/ctxswitch/ common/{schema.py,protocol.py,config.py}
                 collector/{main.py,indexer.py,extractor.py,liveness.py,spool.py,queue.py,deliver.py,adapters/{claude.py,pi.py,claude_win.py}}
                 hub/{main.py,api.py,store.py,ingest.py,tracker/{scheduler.py,seats.py,prompts.py},attention.py,workstreams.py,messages.py}
                 client/{cli.py,tui/app.py,mcp_server.py,launch.py,notify.py,install.py}
  adapters/pi/ctxswitch.ts   skills/ctxswitch/SKILL.md   tests/fixtures/{claude,pi}/*.jsonl
```

## Phases
0. **Spikes + DESIGN.md in repo**: (a) Claude inbox socket from a non-child process — payload, permission-class
   field, idle→turn, transcript trace; (b) pi `sendUserMessage` idle vs busy; (c) seat bench on the text seat —
   one extraction call's latency/tokens on real exchanges, 12-session throughput alongside a saint-agent `--text`
   job, json_schema pass-through; (d) systemd --user viability on all three hosts (`degraded` on gpu-host/devbox).
1. Hub store + ingest API + collector (indexer, extractor, liveness, queue) + **TUI on hard signals** with host
   filter + `ctx ls/show`. Run hub locally first; deploy to gpu-host at the end of the phase.
2. Hooks + pi extension + spool; Notification-driven permission asks; resume briefing (hard-signal version);
   `install`/`doctor` on all three hosts; Windows-native read-only adapter.
   **Done** (2026-10-29): hooks, pi extension, spool, permission/elicitation/prompt asks, `install`/`uninstall`/
   `doctor`, resume briefing, Windows-native read-only adapter.
3. Tracker: extraction rows (instructions, decisions, claims, plain-text asks), reentry/purpose; scheduler.
   **Done** (2026-10-29): ledger rows with verbatim quotes, purpose + history, re-entry card, operator retire
   (`POST /v1/sessions/<key>/ledger/<id>/retire`), shown in the TUI, `ctx show` and the resume briefing.
   Not done: a TUI key to retire a row (`e` is taken by "show ended"); confidence scores.
4. Attention events + per-host toasts; cross-host workstreams; "since you last looked".
5. Messaging: pi inject + reply; Claude inject (per spike); queued delivery; quick-reply; cross-host resume.
6. MCP + skill.
7. Hub hardening: backups to NAS, token rotation, optional TLS; move-to-NAS runbook.
8. Experimental flags (drift, re-ask, cross-session contradictions), off by default.

## Verification
- Extractor unit tests on trimmed real transcripts from both harnesses and all three hosts (AskUserQuestion
  Q/A, plan rejection with feedback, interrupts, compaction, sidechains, pi branches, Windows path forms);
  liveness with stale registry files; queue replay idempotence (duplicate offsets, hub restart mid-batch).
  Assert test counts, not just rc.
- Protocol: collector↔hub contract tests; hub restart → collectors reconnect and drain; laptop test with the hub
  unreachable for 10 min → nothing lost, TUI shows "collector offline since".
- Tracker calibration with **free labels**: hide AskUserQuestion rows, check the model recovers Q/A from
  surrounding text → precision/recall per seat; this session is a fixture (must carry the "live tracking, not a
  primer" and "centralised, multi-machine" instructions and the rejected-plan asks).
- Load: 12 replayed sessions across 3 collectors → hard signals visible on every TUI <2 s after turn end;
  LLM rows within the Phase 0 budget; SAINT down → TUI fully functional, rows marked stale.
- E2E by hand: Claude on devbox + pi on the laptop appear in the TUI on gpu-host and the laptop with identical
  state → agent asks a question → attention item on both, toast on the laptop only → answer from the gpu-host
  TUI → decision row with turn ref visible on the laptop → exit, queue a message, `Enter` on devbox's TUI
  resumes over ssh → briefing + message delivered.
- `ctx uninstall` restores each host's settings.json byte-for-byte from the backup.
