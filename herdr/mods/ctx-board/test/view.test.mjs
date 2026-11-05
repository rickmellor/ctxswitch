// The board's text builders, tested as plain data: no gpui-kit, no herdr — just the hub's JSON.
// Run: node --test herdr/mods/ctx-board/test/

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { age, money, tzOffset, rowParts, headerLines, detailSections, detailTitle, toneColor } from "../view.js";

const here = dirname(fileURLToPath(import.meta.url));
const board = JSON.parse(readFileSync(join(here, "fixtures", "board.json"), "utf8"));
const detail = JSON.parse(readFileSync(join(here, "fixtures", "board-detail.json"), "utf8"));
const health = JSON.parse(readFileSync(join(here, "fixtures", "health.json"), "utf8"));

// The samples were captured at a fixed moment, so the tests name their own "now".
const NOW = Date.parse("2026-11-05T03:30:00Z");

test("age: seconds, minutes, hours, days, and the unknown", () => {
  assert.equal(age("2026-11-05T03:29:15Z", NOW), "45s");
  assert.equal(age("2026-11-05T03:30:00Z", NOW), "0s");
  assert.equal(age("2026-11-05T03:29:59.500Z", NOW), "0s");
  assert.equal(age("2026-11-05T03:28:59Z", NOW), "1m");          // 60 s tips over to minutes
  assert.equal(age("2026-11-05T03:27:00Z", NOW), "3m");
  assert.equal(age("2026-11-05T02:30:01Z", NOW), "59m");         // 3599 s is still minutes
  assert.equal(age("2026-11-05T02:30:00Z", NOW), "1h00");        // 3600 s tips over to hours
  assert.equal(age("2026-11-05T01:20:00Z", NOW), "2h10");
  assert.equal(age("2026-11-04T03:31:00Z", NOW), "23h59");       // 86340 s
  assert.equal(age("2026-11-04T03:30:00Z", NOW), "1d");          // 86400 s tips over to days
  assert.equal(age("2026-10-29T03:30:00Z", NOW), "7d");
  assert.equal(age(null, NOW), "?");
  assert.equal(age("", NOW), "?");
  assert.equal(age("not a date", NOW), "?");
  assert.equal(age(undefined), "?");
  assert.equal(age("2026-11-05T04:00:00Z", NOW), "0s");          // never negative
});

test("money: two decimals, em-dash for nothing", () => {
  assert.equal(money(206.537374), "$206.54");
  assert.equal(money(0), "$0.00");
  assert.equal(money(530), "$530.00");
  assert.equal(money(3.7331), "$3.73");
  assert.equal(money(null), "—");
  assert.equal(money(undefined), "—");
});

test("tzOffset: the hub's ±HH:MM from getTimezoneOffset", () => {
  // getTimezoneOffset is minutes behind UTC: UTC+2 reports -120, UTC-5:30 reports 330.
  assert.equal(tzOffset({ getTimezoneOffset: () => 0 }), "+00:00");
  assert.equal(tzOffset({ getTimezoneOffset: () => -120 }), "+02:00");
  assert.equal(tzOffset({ getTimezoneOffset: () => 330 }), "-05:30");
  assert.equal(tzOffset({ getTimezoneOffset: () => -30 }), "+00:30");
  assert.match(tzOffset(), /^[+-]\d{2}:\d{2}$/);                 // a real Date gives a real offset
});

test("rowParts: a busy top-level row", () => {
  const p = rowParts(board.rows[3], { number: 4, now: NOW });      // OAuth login: busy, told the pool is on, one job
  assert.equal(p.number, 4);
  assert.equal(p.glyph, "●");
  assert.equal(p.state, "busy");
  assert.equal(p.tone, "working");
  assert.equal(p.label, "cc");                                     // the hub's label, as given: no profile logic here
  assert.equal(p.title, "OAuth login");
  assert.match(p.age, /^\d+[smhd]/);                               // age_at is an instant; the fixture's is relative
  assert.deepEqual(p.pool, { text: "⌂", tone: "bold green" });
  assert.equal(p.askAge, "");
  assert.equal(p.unseen, "");
  assert.equal(p.cost, "");
  assert.equal(p.waiting, "");
  assert.equal(p.host, "laptop");
  assert.equal(p.harness, "claude");
  assert.equal(p.nested, false);
  assert.equal(p.prefix, "");
  assert.equal(p.children.length, 1);
});

test("rowParts: an idle pi row with no unseen and no pool mark", () => {
  const p = rowParts(board.rows[4], { number: 5, now: NOW });      // release notes
  assert.equal(p.glyph, "○");
  assert.equal(p.state, "idle");
  assert.equal(p.tone, "idle");
  assert.equal(p.label, "pi");
  assert.equal(p.pool, null);
  assert.equal(p.unseen, "");
  assert.equal(p.askAge, "");
  assert.equal(p.children.length, 0);
  const noNumber = rowParts({ ...board.rows[4], unseen: null }, { now: NOW });
  assert.equal(noNumber.number, null);                             // no number asked for
});

test("rowParts: a nested child uses its job_label and loses its marks", () => {
  const kid = rowParts(board.rows[3], { number: 4, now: NOW }).children[0];
  assert.equal(kid.nested, true);
  assert.equal(kid.prefix, "↳");
  assert.equal(kid.number, null);
  assert.equal(kid.title, "oauth-tests.md · ok · 72 turns");       // job_label, not the prompt-ish title
  assert.equal(kid.pool, null);
  assert.equal(kid.askAge, "");
  assert.equal(kid.unseen, "");
  assert.equal(kid.cost, "2.0M tok");
  assert.equal(kid.state, "idle");
});

test("rowParts: a waiting row carries its └ line, ◐ age and +N", () => {
  const p = rowParts(board.rows[2], { number: 3, now: NOW });      // rate limiter, asking a plain question
  assert.equal(p.state, "waiting");
  assert.equal(p.tone, "blocked");
  assert.equal(p.glyph, "◐");
  assert.equal(p.waiting, "Token bucket or sliding window?");
  assert.equal(p.askAge, "◐" + board.rows[2].ask_age);             // the hub already says "3m"; the mod adds the ◐
  assert.equal(p.unseen, "+1");
  const r = { ...board.rows[2], ask_age: "12m", unseen: 7 };
  const q = rowParts(r, { now: NOW });
  assert.equal(q.askAge, "◐12m");
  assert.equal(q.unseen, "+7");
});

test("rowParts: nothing blows up on an empty or odd row", () => {
  const p = rowParts({}, {});
  assert.equal(p.glyph, "?");
  assert.equal(p.tone, "dim");
  assert.equal(p.title, "");
  assert.equal(p.age, "?");
  assert.deepEqual(p.children, []);
});

test("headerLines: the sample header's two lines", () => {
  const [one, two] = headerLines(board.header, { host: "laptop", now: NOW });
  assert.equal(one.text, "ctx · laptop · 6 live · 2 waiting on you · pool on");
  assert.equal(two.text, "today $0.00 · cloud $0.00 · local — · —");   // no SAINT, no budget in the fixture
  assert.equal(two.tone, "dim");                                  // the header's budget_style
  const quiet = headerLines({ ...board.header, waiting: 0, pool: null }, { host: "gpu-host" })[0];
  assert.equal(quiet.text, "ctx · gpu-host · 6 live · nothing waiting");
  const money = headerLines({ ...board.header, spend_today: { total_usd: 206.537, cloud_usd: 202.8, local_usd: 3.7331,
    budget_left_usd: 530, budget_style: "yellow" } }, { host: "laptop" })[1];
  assert.equal(money.text, "today $206.54 · cloud $202.80 · local $3.73 · $530 left");
  assert.equal(money.tone, "yellow");
  const empty = headerLines({ spend_today: {} }, {})[1];
  assert.equal(empty.text, "today — · cloud — · local — · —");
});

test("detailSections: the Textual board's order, empties dropped", () => {
  const titles = detailSections(detail, { now: NOW }).map((s) => s.title);
  assert.deepEqual(titles, [
    "RECAP", "WAITING ON YOU", "INFO", "LAST EXCHANGE", "SINCE YOU LOOKED", "MESSAGES",
    "YOUR INSTRUCTIONS", "AGENT SAYS", "EARLIER",
  ]);                                                              // no pool jobs, no decisions, no siblings: dropped
  assert.equal(detailSections({}, { now: NOW }).length, 0);       // an empty card is no sections
});

test("detailSections: RECAP says (updating…) only when stale", () => {
  const of = (recap) => detailSections({ ...detail, recap }, { now: NOW })[0].title;
  assert.equal(of(detail.recap), "RECAP");
  assert.equal(of({ ...detail.recap, stale: true }), "RECAP (updating…)");
  const recap = detailSections(detail, { now: NOW })[0];
  assert.match(recap.lines[0].text, /^Middleware in; waiting on you/);
  assert.match(recap.lines[1].text, /^› /);                        // the re-entry card's lines
  assert.equal(detailSections({ ...detail, recap: { text: "", stale: true } }, { now: NOW })
    .some((s) => s.title.startsWith("RECAP")), false);
});

test("detailSections: WAITING ON YOU follows the open asks", () => {
  const section = detailSections(detail, { now: NOW }).find((s) => s.title === "WAITING ON YOU");
  assert.equal(section.lines[0].tone, "blocked");
  assert.match(section.lines[0].text, /\[plain\] Token bucket or sliding window\?/);
  assert.equal(detailSections({ ...detail, waiting_on: [] }, { now: NOW }).some((s) => s.title === "WAITING ON YOU"), false);
  const asked = detailSections({ ...detail, waiting_on: [{ kind: "question", text: "Which herdr?" }] }, { now: NOW })
    .find((s) => s.title === "WAITING ON YOU");
  assert.match(asked.lines[0].text, /\[question\] Which herdr\?/);
});

test("detailSections: the rest of the card reads from the sample", () => {
  const by = Object.fromEntries(detailSections(detail, { now: NOW }).map((s) => [s.title, s]));
  assert.match(by["INFO"].lines[0].text, /^\S+ · 2 exch · api@feat\/rate-limit \/home\/me\/api/);
  assert.match(by["LAST EXCHANGE"].lines[0].text, /^you \([^)]*\): don't touch the billing endpoints/);
  assert.match(by["LAST EXCHANGE"].lines[1].text, /^agent \(/);
  assert.equal(by["SINCE YOU LOOKED"].lines[0].text, detail.since_line);
  assert.equal(by["SINCE YOU LOOKED"].lines[0].tone, "accent");
  assert.equal(by["SINCE YOU LOOKED"].lines.length, 1 + detail.since_viewed.rows.slice(-5).length);
  assert.match(by["MESSAGES"].lines[0].text, /keep the limits per API key, not per IP/);
  assert.match(by["YOUR INSTRUCTIONS"].lines[0].text, /^1 • Don't touch the billing endpoints/);
  assert.match(by["AGENT SAYS"].lines[0].text, /^✓ Middleware done, tests pass/);
  assert.equal(by["AGENT SAYS"].lines[0].tone, "done");
  assert.match(by["EARLIER"].lines[0].text, /^#0 \[human\] add per-key rate limiting to the public API/);
  assert.equal(by["EARLIER"].lines.length, detail.earlier.length);
  const jobs = detailSections({ ...detail, pool_jobs: [{ label: "x.md · ok · 3 turns", cost_mark: "~$0.05" }] }, { now: NOW })
    .find((s) => s.title === "POOL JOBS");
  assert.match(jobs.lines[0].text, /^↳ x\.md · ok · 3 turns ~\$0\.05$/);
  const decided = detailSections({ ...detail, decisions: [{ question: "Q?", answer: "A." }] }, { now: NOW })
    .find((s) => s.title === "DECISIONS");
  assert.match(decided.lines[0].text, /^✓ Q\?/);
  assert.match(decided.lines[1].text, /^→ A\./);
});

test("detailTitle: the card's own header line", () => {
  assert.deepEqual(detailTitle(detail), { glyph: "◐", title: "rate limiter", state: "waiting", tone: "blocked" });
  assert.equal(detailTitle({}).title, "-");
});

test("detailSections: long text is folded to one line and clipped", () => {
  const long = detailSections({ ...detail, recap: { text: "one\ntwo   three\n" + "x".repeat(500), reentry_lines: [] },
    earlier: [] }, { now: NOW })[0];
  assert.equal(long.lines.length, 1);
  assert.equal(long.lines[0].text.length, 400);
  assert.ok(long.lines[0].text.endsWith("…"));
});

test("toneColor: the hub's style words map onto theme colours", () => {
  assert.equal(toneColor("bold green"), "working");
  assert.equal(toneColor("dim green"), "working");
  assert.equal(toneColor("grey50"), "dim");
  assert.equal(toneColor("bold yellow"), "blocked");
  assert.equal(toneColor("dim"), "dim");
  assert.equal(toneColor("dim"), "dim");
  assert.equal(toneColor("red"), "muted");   // nothing in the theme by that name
  assert.equal(toneColor(null), "dim");
  assert.equal(toneColor("bold"), "dim");
});
