// The ctx board's actions, as plain data: which hub call to make and which herdr commands to run, in order.
// Nothing here touches gpui-kit, `herdr` or fetch — a function takes the row (the hub's own words), the terminal
// list and ctx.json, and answers with {command, args} steps and fetch descriptions. main.js carries them out, so
// every rule about sessions stays where it belongs: in the hub's /v1/board, /v1/launch and POSTs.
//
// A plan is { steps, notify, extra }. A step is one of:
//   { fetch: { method, path, body?, into? } }        call the hub; `into` keeps the answer for later steps
//   { command: <app command>, args? }                run(...) it
//   { command: "space.new", from, as, machine? }     run it with the stored recipe's label/cwd/argv, on `machine`
//                                                    (omitted: the recipe's own host); `as` keeps what it made
//   { command: "hc", args: [..] }                    hc(...) it
//   { label_new: true }                              label the space.new terminal this plan made with the recipe's
//                                                    key, so the next board read finds that terminal by key
// `notify` posts a toast instead; `extra` is work that goes along without being shown (marking a row viewed).

/** The machines map → the herdr machine name for a ctx host. "" means this computer, and an absent entry says the
 *  same: ctx.json's `machines` only names the ones that live elsewhere ("gpu-host": "vps"). */
export function machineFor(cfg, hostId) {
  const m = (cfg ?? {}).machines?.[hostId ?? ""];
  return typeof m === "string" && m ? m : "";
}

const live = (all) => (Array.isArray(all) ? all : []).filter((t) => (t?.state ?? "live") === "live");

const enc = (s) => encodeURIComponent(String(s ?? ""));

/**
 * The herdr terminal a row runs in, or null:
 *  - the live terminal labelled `ctx.key=<row.key>` — the collector's own tag, which wins anywhere;
 *  - else the row's `hc_at` address: plain on this computer, `<machine>/<hc_at>` on another (the machine comes
 *    from ctx.json's map; an address already carrying one still matches).
 */
export function terminalFor(row, all, cfg) {
  const r = row ?? {};
  const list = live(all);
  if (r.key) {
    const tagged = list.find((t) => (t?.labels ?? {})["ctx.key"] === r.key);
    if (tagged) return tagged;
  }
  if (!r.hc_at) return null;
  const at = String(r.hc_at);
  const machine = machineFor(cfg, r.host_id);
  return list.find((t) => String(t?.address ?? "") === (machine ? `${machine}/${at}` : at))
    ?? (machine ? list.find((t) => String(t?.address ?? "").endsWith(`/${at}`)) ?? null : null);
}

/** A terminal's pane, looked up in `panes()` by whoever has it: `terminals()` is core's list and doesn't say. */
export function paneFor(panes, address) {
  if (!address) return null;
  return (Array.isArray(panes) ? panes : []).find((p) => p?.terminal === address)?.pane ?? null;
}

/** The newest live terminal in a space — the one `space.new` just made, once core has taken it on. */
export function newestInSpace(all, spaceId) {
  if (!spaceId) return null;
  const found = live(all)
    .filter((t) => t?.space === spaceId)
    .sort((a, b) => (Number(b?.seq) || 0) - (Number(a?.seq) || 0)
      || String(b?.address ?? "").localeCompare(String(a?.address ?? "")));
  return found[0] ?? null;
}

/** `space.new`'s arguments, from a launch recipe: its label, its folder, its argv, on the machine ctx.json names. */
export function spaceArgs(recipe, cfg) {
  return {
    name: recipe?.label ?? "",
    machine: machineFor(cfg, recipe?.host),
    folder: recipe?.cwd || "~",
    argv: recipe?.argv ?? [],
  };
}

// A session's key rides in these paths as itself, slashes and all — the hub's routes are `{key:path}`, which is
// what ctx's own client sends. Only query values get encoded.
const viewed = (key) => ({ fetch: { method: "POST", path: `/v1/sessions/${key}/viewed`, body: { at: new Date().toISOString() } } });

/**
 * Enter / double-click on a row.
 *  - its terminal is here → show it; give it a pane first when it has none (a sub-agent sent back has none);
 *  - ended and not here → ask the hub how to resume it (`GET /v1/launch?key=`), then start that space on its host;
 *  - live and not here → it runs in someone else's herdr: say so.
 * Every case marks the row viewed, which is what clears the hub's +N.
 */
export function openPlan(row, all, cfg, pane = null) {
  const r = row ?? {};
  const t = terminalFor(r, all, cfg);
  if (t) {
    const at = String(t.address ?? "");
    // A terminal sent back with `terminal.park` has no pane: give it one in a tab before focusing it.
    const steps = pane ? [] : [{ command: "terminal.keep", args: { terminal: at, place: "tab" } }];
    steps.push({ command: "agent.focus", args: { terminal: at } });
    return { steps, extra: [viewed(r.key)] };
  }
  if (r.state === "ended") {
    return {
      steps: [
        { fetch: { method: "GET", path: `/v1/launch?key=${enc(r.key)}`, into: "recipe" } },
        { command: "space.new", from: "recipe", as: "made", machine: machineFor(cfg, r.host_id) },
        { label_new: true },
      ],
      extra: [viewed(r.key)],
    };
  }
  return {
    steps: [],
    notify: { title: "ctx", body: `running elsewhere: open it on ${r.host_id || "its host"}`, level: "info" },
    extra: [viewed(r.key)],
  };
}

/**
 * `n` / `s`, and the resume path once the recipe is in hand: start the recipe's space, then label what it made
 * when the recipe knows the session's key (a new shell's key comes back from the hub for exactly this).
 */
export function startPlan(recipe, cfg) {
  const steps = [{ command: "space.new", args: spaceArgs(recipe, cfg), as: "made" }];
  if (recipe?.key) steps.push({ label_new: true });
  return { steps };
}

/** `k`: end the row's terminal, or say where it actually runs. */
export function endPlan(row, all, cfg) {
  if (!cfg) return { steps: [], notify: { title: "ctx", body: "no ctx.json: nothing to end", level: "warning" } };
  const t = terminalFor(row ?? {}, all, cfg);
  if (!t) return { steps: [], notify: { title: "ctx", body: "not in herdr: end it where it runs", level: "warning" } };
  return { steps: [{ command: "terminal.kill", args: { terminal: String(t.address ?? "") } }] };
}

/**
 * `m` / `a`: the POST to make, or the hub's own reason not to.
 * `m` refuses on `can_message`; `a` refuses without a `reply_target` and says `reply_why` — the hub decided both,
 * this only repeats them. `a` answers with the target's ref, which is what makes it an answer and not a nudge.
 */
export function messageRequest(row, text, cfg, mode = "m") {
  const r = row ?? {};
  const body = String(text ?? "");
  if (!body.trim()) return { refuse: "nothing to send" };
  if (mode === "a" && !r.reply_target) return { refuse: r.reply_why || "nothing to answer" };
  if (typeof r.can_message === "string" && r.can_message) return { refuse: r.can_message };
  const out = { path: `/v1/sessions/${r.key}/messages`, body: { body, from_host: cfg?.host_id ?? null } };
  if (mode === "a") out.body.answers = r.reply_target.ref;
  return out;
}

/** `i`: the POST that retires one standing instruction. The hub's ledger row id is what retires it. */
export function retireRequest(row, instruction) {
  const r = row ?? {};
  const id = instruction?.id ?? instruction?.n;
  if (id === null || id === undefined || id === "") return { refuse: "no such instruction" };
  return { path: `/v1/sessions/${r.key}/ledger/${enc(id)}/retire`, body: {} };
}

/**
 * `h`: this host → each other host, in the header's order → every host, and back to this host.
 * `current` is the host on screen, null for every host; `thisHost` is ctx.json's host_id, where the cycle starts.
 */
export function hostCycle(hosts, current, thisHost) {
  const list = (Array.isArray(hosts) ? hosts : []).map((h) => String(h)).filter(Boolean);
  const me = thisHost ?? null;
  if (current === null || current === undefined) {                                // every host → this one
    if (me && list.includes(me)) return me;
    return list[0] ?? me ?? null;                                                 // this host isn't listed: start where the hub has news
  }
  const others = list.filter((h) => h !== me);
  if (current !== me) {
    const at = others.indexOf(current);                                          // an other host → the next, or all
    return at >= 0 && at + 1 < others.length ? others[at + 1] : null;
  }
  return others.length ? others[0] : null;                                       // this host → the first other, or all
}

/** `/v1/board`'s query for the filter state: `host` is a name, "" for every host, and absent means this
 *  computer's own — where the filter starts, before ctx.json has even been read. */
export function boardPath(filter, cfg) {
  const f = filter ?? {};
  const host = typeof f.host === "string" ? f.host : (cfg?.host_id ?? "");
  return `/v1/board?host=${enc(host)}&screen_host=${enc(cfg?.host_id ?? "")}&ended=${f.ended ? 1 : 0}` +
    `&tz=${enc(f.tz ?? "+00:00")}`;
}

/** `GET /v1/launch?host=&harness=[&profile=&cwd=]` for one launch-picker choice. */
export function launchPath(choice, cwd) {
  const c = choice ?? {};
  const p = [`host=${enc(c.host)}`, `harness=${enc(c.harness)}`];
  if (c.profile) p.push(`profile=${enc(c.profile)}`);
  if (cwd) p.push(`cwd=${enc(cwd)}`);
  return `/v1/launch?${p.join("&")}`;
}

/** The picker's first step: this host first, then the others the hub has heard from. */
export function hostChoices(hosts, thisHost) {
  const list = (Array.isArray(hosts) ? hosts : []).map((h) => String(h?.host_id ?? h ?? "")).filter(Boolean);
  const me = String(thisHost ?? "");
  return list.includes(me) ? [me, ...list.filter((h) => h !== me)] : list;
}

/**
 * A host's launch choices, in the order the picker shows them: the claude accounts it published by name, then pi,
 * then a plain shell — and only what that host says it has installed. `shell` is the ctx shell, so a host with no
 * ctx has nothing to start beyond the agents it does have.
 */
export function harnessChoices(info) {
  const i = info ?? {};
  const agents = i.agents ?? {};
  const out = [];
  for (const p of i.profiles ?? []) {
    if (p?.name) out.push({ harness: "claude", profile: String(p.name), label: `claude · ${p.name}` });
  }
  if (!out.length && agents.claude) out.push({ harness: "claude", label: "claude" });
  if (agents.pi) out.push({ harness: "pi", label: "pi" });
  if (agents.ctx) out.push({ harness: "shell", label: "shell" });
  return out;
}

/**
 * keys.json delivers "<mod>.<command>" as one string, its shorthand after a colon ("ctx-board.select:3").
 * A mod's own namespaced argument is literal text (an app command would parse it into an argument), so this is
 * the one place that decides what each command means.
 */
export function parseCommand(str) {
  const s = String(str ?? "");
  const rest = s.includes(".") ? s.slice(s.indexOf(".") + 1) : s;
  const colon = rest.indexOf(":");
  const command = colon < 0 ? rest : rest.slice(0, colon);
  const raw = colon < 0 ? null : rest.slice(colon + 1);
  if (command === "select") {
    const n = Number(raw);
    return { command, index: Number.isInteger(n) && n >= 1 && n <= 9 ? n : null };
  }
  return { command, index: null };
}

/** Where a digit, or up/down, moves the selection over the numbered rows. */
export function moveSelection(rows, selected, delta, index) {
  const list = Array.isArray(rows) ? rows : [];
  if (index) return list[index - 1]?.key ?? null;
  const at = list.findIndex((r) => r?.key === selected);
  if (at < 0) return list[0]?.key ?? null;
  const next = delta < 0 ? Math.max(0, at - 1) : Math.min(list.length - 1, at + 1);
  return list[next]?.key ?? null;
}
