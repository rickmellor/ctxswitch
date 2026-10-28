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

## (a) Claude inbox socket wire format — in progress
Field names visible in the 2.1.280 binary: `peer_message`, `reply_to`, `fromSession`; receiver logs
`[uds-messaging] Dropping message: session_id mismatch (got …, expected …)` → payload carries the target
session id. Verification needs real bytes: `spikes/fake_inbox.py` (run by the operator; the auto-mode
classifier refuses to let the agent register a fake session).
