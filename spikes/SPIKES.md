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
