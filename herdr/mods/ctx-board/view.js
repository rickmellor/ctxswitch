// The ctx board's text, as plain data: strings and {text, tone} parts, never gpui-kit elements.
// Everything here is a pure function of the JSON the hub returns — the hub already decided what to
// show (state, glyph, marks, section contents), so no rule about sessions belongs in this file.

// Tones are theme colour names; main.js maps them with theme(). "bold"/"dim" are styles riding along.
export const TONE = {
  foreground: "foreground", muted: "muted", dim: "dim", accent: "accent",
  busy: "working", waiting: "blocked", done: "done", idle: "idle",
  shell: "foreground", unknown: "unknown", ended: "dim",
  green: "working", grey: "dim", yellow: "blocked",
};

const TONE_COLOR = {
  foreground: "foreground", muted: "muted", dim: "dim", accent: "accent",
  working: "working", blocked: "blocked", done: "done", idle: "idle", unknown: "unknown",
};

// A part's theme() key. The hub's style words arrive as "bold green" / "dim green" / "grey50" /
// "bold yellow": style words first, then the colour — which is what picks the theme entry.
const STYLE_WORDS = /^(bold|dim|italic|underline)$/;

export function toneColor(tone) {
  const words = String(tone ?? "").split(/[\s-]+/).filter(Boolean);
  const colour = words.filter((w) => !STYLE_WORDS.test(w)).map((w) => w.replace(/\d+$/, ""))[0];
  if (!colour) return "dim";                                      // "dim" on its own says dim
  const mapped = TONE[colour] ?? colour;
  return Object.prototype.hasOwnProperty.call(TONE_COLOR, mapped) ? mapped : "muted";
}

/** An agent claim's tone, by its kind word. */
export function claimTone(kind) {
  return { done: "done", tests_pass: "done", tests_fail: "blocked", blocked: "blocked" }[kind] ?? "muted";
}

export function hasBold(tone) {
  return /\bbold\b/.test(String(tone ?? ""));
}

const isNil = (v) => v === null || v === undefined;

export function parseMs(iso) {
  if (!iso) return null;
  const ms = Date.parse(String(iso).trim());
  return Number.isNaN(ms) ? null : ms;
}

/** fmt.age: "45s" under a minute, "3m" under an hour, "2h05m" under a day, else "1d". "?" when unknown. */
export function age(iso, now) {
  const ms = parseMs(iso);
  if (ms === null) return "?";
  const at = isNil(now) ? Date.now() : now;
  const sec = Math.max(0, Math.floor((at - ms) / 1000));
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h${String(Math.floor((sec % 3600) / 60)).padStart(2, "0")}`;
  return `${Math.floor(sec / 86400)}d`;
}

/** Money with two decimals, "—" for nothing to say. */
export function money(usd) {
  return isNil(usd) ? "—" : `$${Number(usd).toFixed(2)}`;
}

/** new Date().getTimezoneOffset() minutes → the hub's ±HH:MM. */
export function tzOffset(date) {
  const d = date ?? new Date();
  const off = d.getTimezoneOffset();
  const sign = off <= 0 ? "+" : "-";
  const abs = Math.abs(off);
  return `${sign}${String(Math.floor(abs / 60)).padStart(2, "0")}:${String(abs % 60).padStart(2, "0")}`;
}

const short = (s, n) => {
  const t = String(s ?? "").replace(/\s+/g, " ").trim();
  return t.length <= n ? t : t.slice(0, n - 1) + "…";
};

const poolMark = (row) => (row.pool_mark ? { text: row.pool_mark, tone: row.pool_mark_style || "dim" } : null);

/** One board row → the pieces main.js paints: number, glyph, label, mark, title, right-side marks,
 *  the "└ …" line under a waiting row, and its children (same shape, one level deeper). */
export function rowParts(row, opts = {}) {
  const r = row ?? {};
  const now = opts.now;
  const nested = Boolean(r.nested);
  const title = nested ? r.job_label || r.title || "" : r.title || r.job_label || "";
  const parts = {
    key: r.key ?? "",
    number: nested ? null : (opts.number ?? null),
    glyph: r.glyph || "?",
    state: r.state || "unknown",
    tone: TONE[r.state] ?? "dim",
    label: r.label ?? "",
    prefix: nested ? "↳" : "",
    pool: poolMark(r),
    title: short(title, 90),
    age: age(r.age_at, now),
    askAge: r.ask_age ? `◐${r.ask_age}` : "",                 // the hub already says how long ("3m"); the mod adds the ◐
    unseen: Number.isFinite(r.unseen) && r.unseen > 0 ? `+${r.unseen}` : "",
    cost: r.cost_mark ?? "",
    waiting: r.waiting_on ? short(r.waiting_on, 120) : "",
    host: r.host_id ?? "",
    harness: r.harness ?? "",
    nested,
    children: (r.children ?? []).map((c) => rowParts(c, { now, nested: true })),
  };
  return parts;
}

/** The header's two lines. */
export function headerLines(header, opts = {}) {
  const h = header ?? {};
  const host = opts.host || (h.hosts ?? [])[0] || "all";
  const waiting = h.waiting ?? 0;
  let one = `ctx · ${host} · ${h.live ?? 0} live · ${waiting ? `${waiting} waiting on you` : "nothing waiting"}`;
  if (h.pool) one += ` · pool ${h.pool}`;
  const s = h.spend_today ?? {};
  const two = `today ${money(s.total_usd)} · cloud ${money(s.cloud_usd)} · local ${money(s.local_usd)}` +
    ` · ${isNil(s.budget_left_usd) ? "—" : `$${Math.trunc(s.budget_left_usd)} left`}`;
  return [
    { text: one, tone: "foreground" },
    { text: two, tone: s.budget_style || "dim" },
  ];
}

/** The detail card's own header: its glyph, title and state word. */
export function detailTitle(detail) {
  const d = detail ?? {};
  return {
    glyph: d.glyph || "?",
    title: d.title || "-",
    state: d.state || "unknown",
    tone: TONE[d.state] ?? "dim",
  };
}

const infoLine = (info, now) =>
  `${age(info.age_at, now)} · ${info.turn_count ?? 0} exch · ${info.repo || "-"} ${info.cwd || ""} ` +
  `${info.model || ""} ${info.permission_mode || ""}`.replace(/\s+/g, " ").trim();

/** The detail card's sections, in the Textual board's order, empties dropped.
 *  Each is {title, lines: [{text, tone}]}; main.js turns them into elements. */
export function detailSections(detail, opts = {}) {
  const d = detail ?? {};
  const now = opts.now;
  const out = [];
  const push = (title, lines) => { if (lines && lines.length) out.push({ title, lines }); };
  const line = (text, tone = "foreground") => ({ text: short(text, 400), tone });

  const rec = d.recap ?? {};
  if (rec.text) {
    const lines = [line(rec.text, "muted")];
    for (const l of rec.reentry_lines ?? []) lines.push(line(`› ${l}`, "muted"));
    push(`RECAP${rec.stale ? " (updating…)" : ""}`, lines);
  }

  if ((d.waiting_on ?? []).length) {
    push("WAITING ON YOU", d.waiting_on.map((a) => line(`[${a.kind ?? "?"}] ${a.text ?? ""}`, "blocked")));
  }

  if (d.spend?.line) push("SPEND", [line(d.spend.line, "dim")]);

  if ((d.pool_jobs ?? []).length) {
    push("POOL JOBS", d.pool_jobs.map((j) => line(`↳ ${j.label || "?"} ${j.cost_mark || "—"}`, "dim")));
  }

  const info = d.info ?? {};
  const knows = ["age_at", "turn_count", "repo", "cwd", "model", "pool_line"].some((k) => !isNil(info[k]) && info[k] !== "");
  if (knows) {
    const infoLines = [line(infoLine(info, now), "dim")];
    if (info.pool_line) infoLines.push(line(info.pool_line, "dim"));
    push("INFO", infoLines);
  }

  const ex = d.last_exchange ?? {};
  if (ex.you || ex.agent) {
    const lines = [];
    if (ex.you) lines.push(line(`${ex.you.who || "you"} (${age(ex.you.at, now)}): ${ex.you.text ?? ""}`));
    if (ex.agent) lines.push(line(`${ex.agent.who || "agent"} (${age(ex.agent.at, now)}): ${ex.agent.text ?? ""}`, "muted"));
    push("LAST EXCHANGE", lines);
  }

  if (d.since_line) {
    const lines = [line(d.since_line, "accent")];
    for (const r of (d.since_viewed?.rows ?? []).slice(-5)) lines.push(line(`${r.kind}: ${r.text ?? ""}`, "dim"));
    push("SINCE YOU LOOKED", lines);
  }

  if ((d.messages ?? []).length) {
    push("MESSAGES", d.messages.map((m) => line(`${m.glyph || "?"} ${m.body ?? ""}${m.note ? ` — ${m.note}` : ""}`, "muted")));
  }

  if ((d.instructions ?? []).length) {
    push("YOUR INSTRUCTIONS", d.instructions.map((r) => line(`${r.n} • ${r.text ?? ""}`)));
  }

  if ((d.claims ?? []).length) {
    push("AGENT SAYS", d.claims.map((c) => line(`${c.glyph || "·"} ${c.text ?? ""}`, claimTone(c.kind))));
  }

  if ((d.decisions ?? []).length) {
    const lines = [];
    for (const q of d.decisions) {
      if (q.question) lines.push(line(`✓ ${q.question}`, "dim"));
      lines.push(line(`→ ${q.answer ?? ""}`, "muted"));
    }
    push("DECISIONS", lines);
  }

  if ((d.siblings ?? []).length) {
    push(`SAME WORK ELSEWHERE${d.workstream_label ? ` (${d.workstream_label})` : ""}`,
      d.siblings.map((x) => line(`${x.glyph || "?"} ${x.label || x.host_id || ""} ${x.title ?? ""} ${age(x.age_at, now)}`,
        TONE[x.state] ?? "dim")));
  }

  if ((d.earlier ?? []).length) {
    push("EARLIER", d.earlier.map((t) =>
      line(`#${t.idx ?? 0} ${t.origin ? `[${t.origin}] ` : ""}${t.user_text ?? ""}`, "dim")));
  }
  return out;
}
