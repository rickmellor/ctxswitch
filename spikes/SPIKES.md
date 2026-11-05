# Phase 0 spikes

## (c) Seat bench — 2026-10-28, SAINT `http://llm.local:4000/v1`, model `saint-local-text`
One extraction call (system prompt + one exchange, ~230 prompt tokens; schema: instructions/decisions/asks/claims/reentry).

| setting | latency | completion tokens | result |
|---|---|---|---|
| default (thinking on), max_tokens 600 | 8.8–13.4 s | 600 (all reasoning) | **empty content** |
| thinking on, max_tokens 1500 | 20.6 s | 1500, reasoning_len 6595 | finish=length, no JSON |
| `chat_template_kwargs.enable_thinking=false` | 3.4 s | 179 | valid JSON, key drift (`reentry_note`) |
| `enable_thinking=false` + `response_format` json_schema strict | **2.4 s** | 187 | **valid, keys exactly per schema** |

Extraction quality on the sample: instruction "don't push … use the local pool" captured verbatim; the agent's
open question captured as an ask; no inventions. `saint-local-chat` behaves identically (same backing model).

Decisions: thinking OFF is mandatory; always send `response_format` json_schema (gateway passes it to vLLM);
budget ≈ 3 s/exchange → 12 sessions at a turn every 2 min ≈ 18 s of one slot per minute. Comfortable.

## (d) systemd --user / hosts — 2026-10-28
| host | claude | pi | linger | uv | note |
|---|---|---|---|---|---|
| laptop WSL | 2.1.280 | 1.2.0 | no | yes | systemd --user running; `wt.exe` present |
| gpu-host | 2.1.312 | 1.4.0 | **yes** | **no** | user manager `degraded` (2 failed units), still runs |
| devbox | 2.1.308 | 1.2.0 | no | yes | `degraded` (1 failed unit) |

Collectors need `loginctl enable-linger` on devbox + laptop; install uv on gpu-host. Harness versions differ per host
→ extractor must tolerate transcript-format skew (pi 1.4.0 vs 1.2.0 especially).

## (a) Claude inbox socket wire format — 2026-10-28, CONFIRMED by delivery
Reconstructed from the 2.1.280 binary and verified by posting to this session's own socket (the message was
delivered mid-turn as "Another Claude session sent a message").

Transport: connect to `messagingSocketPath` (from `~/.claude/sessions/<pid>.json`), send newline-delimited JSON,
close. Receiver drops a connection with no complete line within 30 s. Optional first line
`{"type":"auth","token":"<CLAUDE_CODE_MESSAGING_TOKEN>"}` (required on Windows only).

Message line (verified minimal form):
```json
{"type":"user","message":{"content":"<text>"},"msg_id":"<sender-chosen id>"}
```
Optional fields seen in the receiver: `session_id` (if present must equal the target's sessionId, else dropped),
`from` (reply address, `uds:<sender socket path>`; the receiver posts `peer_message_status` receipts —
held/delivered/refused/expired/dropped — back to it), `uuid`, `file_attachments`.
Control lines: `{"type":"control","action":"peer_message_status"|"notify_when_idle"|"rename", ...}` — a collector
can subscribe to idle notices with `notify_when_idle` instead of polling the registry (details: capture real bytes).

Permission class lives in an envelope INSIDE `content`:
`<TAG from="uds:…" from-session="…" hop-chain="a,b" from-name="…" from-mode="<mode>" from-plugin="…">\nbody\n</TAG>`;
without it the message asserts no class → delivered to prompting receivers, held for approval by plan/bypass ones.
TAG and the allowed `from-mode` values still need confirming from real bytes → run `spikes/fake_inbox.py`
(registers a fake session `@ctxswitch-spike`, logs what a real SendMessage writes).

Transcript side: received peer messages are `user` entries carrying `origin: {kind: "peer", from, msg_id, …}` →
the collector can match a reply to the `msg_id` it sent and take the agent text of that turn.

## (b) pi `sendUserMessage` idle vs busy — pending (do with the extension in Phase 2, via `pi --mode rpc`)

## herdr (2026-11-04): can herdr-core be ctx's terminal runtime?

Context: the plan to render the ctx board inside the herdr desktop app (a herdr-app mod on herdr-core) and keep the
Textual board beside it on the same hub. Four spikes; S1/S4 need the Windows app and were run by hand later.

**S2 — do the agents report OSC 7501 to herdr-core?** `hc 0.1.18`, `hc run --bg --name s2c -- claude` (Claude
Code 2.1.314) and `-- pi` (1.2.0), then `hc get <t> --json` every 2 s:
- At the prompt: `{"app": "claude-code", "state": "idle"}`, `reported: true`; pi: `{"app": "pi", "state": "idle"}`.
- `hc send s2c "Reply with exactly the word OK"`: `working` within 2 s, then `done` with `turn` 0→1.
- `hc send s2c "Use the AskUserQuestion tool to ask me one yes/no question"`: `working`, then
  `{"state": "blocked", "kind": "question", "msg": "answer: Continue? (Yes · No)"}`, turn 2, until answered.
- herdr-core infers nothing (its docs: status comes only from 7501), so these are the agents' own reports.
**Decision:** no 7501 spoofing for Claude Code or pi. `ctx shell` still needs to emit it (bash reports nothing).
Note: a Claude started from inside another Claude's tool inherits `CLAUDE_CODE_CHILD_SESSION` and disables
transcript saving ("Transcript saving is off"); a launch from herdr or a shell doesn't.

**S3 — another host as an hc machine, through the operator's own ssh.**
`hc machine add --name gpu-host --install --via 'ssh <the usual options> rick@gpu-host'`: hc copied to the GPU
host's `~/.local/bin` and its core started there as that user; `hc machine ls` → `connected`, version 0.1.18;
`hc run --on gpu-host --cwd /home/rick --rm -- bash -lc hostname` ran as `gpu-host/t1`. A custom `--via` is what
the Windows app will use for WSL (`wsl.exe -e hc connect`) and for the other hosts (`wsl.exe -e ssh <host> hc
connect`), so Windows needs no ssh keys of its own.

**Facts for the drivers:** socket `$XDG_RUNTIME_DIR/herdr-core/default.sock` (plus a `default.holder.*.sock`);
state `~/.local/state/herdr-core/default/` (`terminals.json`, `core.json`, screens); `hc kill` takes ONE terminal;
`hc run --json` answers `{"address", "agent", "cwd", "name", "state", "turn"}`; a finished record keeps
`reason: "killed"`, `exit_code`, `status: null`. The core must run as the operator (files created by a root sandbox
had to be removed: `hc` can't chown there), so `hc server start` belongs in a user systemd unit beside
`ctx-collector`.

**S4 — a mod reaches the hub from the Windows app.** herdr-app 0.1.13 and herdr-core 0.1.18 unpacked by hand into
`%LOCALAPPDATA%\Programs\{herdr-app,herdr-core}` (the official installers need no admin either: they only write
there, the user PATH and a Start-menu shortcut; the archives are plain tar.gz/zip, checksums in
`releases.herdr.dev/{app,core}/latest.json`). The app home is `%USERPROFILE%\.config\herdr-app` on Windows too.
A 30-line mod (`mods/ctx-hello`: `readTextFile(appHome()+"/ctx.json")` for the hub URL + token, `fetch(/v1/health)`
with the bearer header every 2 s, `cx.notify()`) passed `herdr-app check`, opened with `herdr-app do tab.new --mod
ctx-hello`, and showed `hub ok · change 90017 · hosts 3 · auth true`. `herdr-app shot <png>` gives a window
screenshot from the CLI, so the app can be checked without looking at it. A mod folder created BEFORE the app's first
start was not docked into the sidebar by its `mod.json` place; a layout block (or tab.new/pane.split) places it.
Starting the Windows core through WSL interop (`herdr-core.exe server start` from WSL) left a process that never
listened; started from a Windows terminal it worked at once.

**S1 — the laptop route. Windows app: no.** herdr-core refuses `--via` on Windows (`machines.rs check_target`:
`cfg!(windows)` → "one line, run by sh here"), so a Windows core reaches WSL only through an sshd inside WSL plus
Windows-side keys; a Windows core started through WSL interop also never came up (started from a Windows terminal it
did). **Linux app under WSLg: yes.** herdr-app 0.1.13 (`~/.local/share/herdr-app`) runs with `DISPLAY=:0
WAYLAND_DISPLAY=wayland-0` (MESA/ZINK warnings, software GL, the window renders), finds the WSL core, and `hc machine
add --name gpu-host|devbox --install --via '<the operator's ssh command>'` puts both hosts in its sidebar as connected
machines. `herdr-app do tab.new --mod ctx-hello` then showed `hub ok · change 90535 · hosts 3 · auth true`. Decision
(Rick): the Linux app now, Windows later. The Bash sandbox is a user namespace (uid 0 inside = the operator), so the
cores and files it made were the operator's all along.
