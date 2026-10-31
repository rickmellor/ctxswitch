# Pool jobs as subordinates, and spend: the contract the collector, hub and board build to

## A. Token usage on turns (collector → hub)
Turn (common/schema.py) gains:  usage: dict | None   model: str   cost_usd: float | None
- usage keys, always all four ints when present: {"input","output","cache_read","cache_creation"}; summed over every
  assistant entry of the turn. Claude: message.usage.{input_tokens, output_tokens, cache_read_input_tokens,
  cache_creation_input_tokens}. pi: message.usage.{input, output, cacheRead, cacheWrite} (reasoning is inside output).
  Shell / agy / unknown: None.
- model: the model named on the turn's last assistant entry (Claude message.model e.g. "claude-opus-5-5"; pi
  message.model e.g. "saint-auto", with message.provider available: record model as f"{provider}/{model}" when provider
  is not "anthropic", else just model). "" when unknown.
- cost_usd: pi's message.usage.cost.total summed when > 0 (pi prices cloud providers itself), else None.

## B. Pool-job link (collector → hub), from ~/.claude/saint-agent-ledger.jsonl ($CTXSWITCH_SAINT_LEDGER overrides)
saint-agent appends one JSON line per event. Two shapes:
  start (new, written at launch): {"event":"start","ts":ISO,"job":"sa-…[-jN]","sid":"<child claude session uuid>",
         "session":"<parent CLAUDE_CODE_SESSION_ID or null>","origin":hostname,"workdir":path,"brief":"<brief path>","profile":"pool|pinned|text"}
  end (as today, plus "sid"): {"ts":ISO,"job":…,"sid":…,"session":…,"origin":…,"workdir":…,"profile":…,"status":"ok|error:…|…",
         "turns":int|"-","dur_s":int,"expect":[a,b],"commits":…,"dirty":…,"tokens":{"input","cache_read","cache_creation","output"},"out":path,…}
  Older end lines have NO "sid": link them by workdir + time instead: the child transcript's cwd == workdir and the
  transcript's first entry timestamp is within [ts - dur_s - 300 s, ts].
SessionMeta (common/schema.py) gains, for a Claude session that is a pool job (default "" / None otherwise):
  parent_key: str      = f"{this host_id}/claude/{session}" when the ledger's session is set (the launching session
                         runs on the same host as the ledger), else ""
  job: str             the job id;   job_status: str  ("running" until an end line exists, then its status)
  job_brief: str       basename of the brief path ("" if unknown);  job_dur_s: int | None;  job_turns: int | None
  job_tokens: dict | None   the end line's tokens (same four keys as usage)
The hub stores parent_key/job/job_status/job_brief as sessions columns; the rest stays in meta.

## C. Hub (store + api)
- GET /v1/sessions rows gain: parent_key, job, job_status, job_brief, children (int: sessions whose parent_key is this
  key), and spend: {"usd": float|null, "tokens": int, "model": str} (usd = this session's priced cloud cost, null when
  its model is unpriced or it has no usage; tokens = total of the four types).
- GET /v1/sessions/{key} gains the same fields plus children_keys: [key…] (newest first) and
  spend: {"usd","tokens","by_type":{4 keys},"model","account","unpriced":bool, "local": {"energy_usd","tokens","requests"}|null}
  where local comes from SAINT /usage/v1?session=<session_id> (cached 60 s; null if no usage endpoint or unreachable).
- GET /v1/spend?from=ISO&to=ISO[&host=]  (ISO instants; turns whose started_at is in [from, to)):
  {"window":{"from","to"},
   "cloud":{"usd":float,"tokens":{4},"by_account":[{"account","usd","tokens","sessions"}],"by_model":[{"model","usd","tokens","priced":bool}]},
   "local":{"energy_usd":float|null,"if_cloud_usd":float|null,"tokens":int,"requests":int,"price_as":str,"source":"saint"|null,"error":str|null},
   "jobs":[{"job","key","parent_key","parent_title","status","brief","started_at","dur_s","turns","tokens":int,"local_usd":float|null,"if_cloud_usd":float|null}],
   "sessions":[{"key","title","host_id","harness","account","usd","tokens","model"}]  (top 30 by usd, then tokens),
   "total_usd": cloud.usd + (local.energy_usd or 0),  "unpriced_models":[str…], "rates":"default"|"config"}
  Accounts: a Claude session's meta.profile ("" → "default"); other harnesses: the harness name.
  Pricing: hub/spend.py. norm_model(): lowercase; strip a leading region ("us."/"eu."/"global."/"apac.") and
  "anthropic."; strip a trailing "-v1:0"-style suffix; a "provider/model" from pi keeps the provider prefix and is
  local/unpriced unless provider is "anthropic"/"bedrock" (then strip it). RATES (USD per million, Bedrock on-demand as
  billed to Rick, read from ccwb on 2026-10-30; key = canonical model, matched by longest common prefix so
  "claude-haiku-4-5-20251001" finds "claude-haiku-4-5"):
    claude-opus-5-5: input 4.4, output 22.0, cache_creation 5.5, cache_read 0.22
    claude-opus-5:   input 5.5, output 27.5, cache_creation 6.875, cache_read 0.55
    claude-sonnet-5: input 2.2, output 11.0, cache_creation 2.75, cache_read 0.22
    claude-fable-5-1: input 11.0, output 55.0, cache_creation 13.75, cache_read 0.275
    claude-haiku-4-5: input 1.1, output 5.5, cache_creation 1.375, cache_read 0.11
    claude-sonnet-4-6: input 3.3, output 16.5, cache_creation 4.125, cache_read 0.33
  [hub.prices] in config.toml overrides/extends: prices."claude-opus-5-5" = {input=…, output=…, cache_creation=…, cache_read=…}.
  A turn with cost_usd set (pi) uses that instead of the table. An unpriced model contributes tokens, never $0.
  Local: HubConfig.usage_url (default: the lowest-priority seat's url with "/v1" stripped + "/usage/v1"; "" disables);
  HubConfig.local_price_as = "claude-opus-5-5". if_cloud_usd = SAINT window tokens_by_type priced with that model's
  rates. Per job: local_usd = job_tokens total × (window energy_cost_usd / window total_tokens) (null if no SAINT);
  if_cloud_usd = job_tokens priced at local_price_as. SAINT is fetched with a 5 s timeout, cached 60 s per URL; an
  error gives local.energy_usd null and local.error = "<ExcName>: …".

## D. Board (client)
- Children (sessions with parent_key) are listed directly under their parent, indented two columns with "↳ " before
  the reach glyph, in the number column nothing (Alt+N skips them), ordered by started_at. Pinned order applies to
  top-level sessions only. A child whose parent isn't on the list (filtered out, or unknown) is shown at top level.
- The waiting line under a row ("what it's waiting on") now starts "└ " instead of "↳ " (the return arrow means
  "subordinate" everywhere: legend, help, README, scripts/diagrams.py row diagram, scripts/screenshots.py).
- A child row ends with its cost: f" ~${local_usd:.2f}" (dim) when known, else f" {tokens/1e6:.1f}M tok"; and its
  label is the job's brief basename + status rather than the (noisy) title, e.g. "job1-runtime.md · ok · 72 turns".
- Header (the #summary widget) becomes two lines; line 2: f"today ${total:.2f} · cloud {c:.2f} · local {l:.2f}" with
  accounts in brackets after cloud when there is more than one and it fits ("cloud 12.40 (bedrock 11.1 · max 1.3)");
  "local —" when SAINT is unavailable. Fetched from /v1/spend with from = local midnight (the board's tz), to = now,
  refreshed with the rest of the data.
- Detail pane: a SPEND section (after the reach/key lines): "cloud $1.23 · 2.4M tokens · claude-opus-5-5 · bedrock"
  (or "unpriced model <m>: 2.4M tokens"), then "local $0.48 energy · 17.3M tokens · 342 requests" when present, and
  for a parent "N pool jobs: $x local est." Same in `ctx show` (fmt.show_lines): a "  spend : …" line.
- `$` opens the Spend screen (a Screen like Help; Esc/`$`/q close): sections Today / 7 days / 30 days each with the
  totals line and by-account; then "Pool jobs (30 days)" table (job, parent title, status, turns, dur, tokens,
  ~$local, if-cloud); then "Sessions (30 days)" top rows; then notes: unpriced models, rates source, "estimates at
  Bedrock on-demand rates; `bedrock-spend` has the bill; local = energy estimate from SAINT". `ctx spend
  [--days N] [--json]` prints the same from the CLI.
- HELP_KEYS gets ("$", "spend: today's cost, cloud and local, per account, per pool job").
