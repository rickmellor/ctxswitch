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
  const p = rowParts(board.rows[0], { number: 1, now: NOW });
  assert.equal(p.number, 1);
  assert.equal(p.glyph, "●");
  assert.equal(p.state, "busy");
  assert.equal(p.tone, "working");
  assert.equal(p.label, "max");
  assert.equal(p.title, "ctx switch");
  assert.equal(p.age, "14m");                                    // age_at 03:15:16 against NOW
  assert.deepEqual(p.pool, { text: "⌂", tone: "bold green" });
  assert.equal(p.askAge, "");
  assert.equal(p.unseen, "+11");
  assert.equal(p.cost, "~$204.93");
  assert.equal(p.waiting, "");
  assert.equal(p.host, "laptop");
  assert.equal(p.harness, "claude");
  assert.equal(p.nested, false);
  assert.equal(p.prefix, "");
  assert.equal(p.children.length, 1);
});

test("rowParts: a shell row with no unseen and no pool mark", () => {
  const p = rowParts(board.rows[1], { number: 2, now: NOW });
  assert.equal(p.glyph, "$");
  assert.equal(p.state, "shell");
  assert.equal(p.tone, "foreground");
  assert.equal(p.unseen, "+13");
  assert.equal(p.askAge, "");
  assert.equal(p.children.length, 0);
  const noMark = rowParts({ ...board.rows[1], pool_mark: "", pool_mark_style: "", unseen: null }, { now: NOW });
  assert.equal(noMark.pool, null);
  assert.equal(noMark.unseen, "");
  assert.equal(noMark.number, null);                              // no number asked for
});

test("rowParts: a nested child uses its job_label and loses its marks", () => {
  const parent = rowParts(board.rows[0], { number: 1, now: NOW });
  const kid = parent.children[0];
  assert.equal(kid.nested, true);
  assert.equal(kid.prefix, "↳");
  assert.equal(kid.number, null);
  assert.equal(kid.title, "board-on-api2.md · running");          // job_label, not the prompt-ish title
  assert.equal(kid.pool, null);
  assert.equal(kid.askAge, "");
  assert.equal(kid.unseen, "");
  assert.equal(kid.cost, "~$0.00");
  assert.equal(kid.state, "busy");
});

test("rowParts: a waiting row carries its └ line and ◐ age", () => {
  const r = { ...board.rows[2], state: "waiting", glyph: "◐", waiting_on: "question: Ship it?",
    ask_age: "2026-11-05T03:27:00Z" };
  const p = rowParts(r, { number: 3, now: NOW });
  assert.equal(p.state, "waiting");
  assert.equal(p.tone, "blocked");
  assert.equal(p.glyph, "◐");
  assert.equal(p.waiting, "question: Ship it?");
  assert.equal(p.askAge, "◐3m");
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
  assert.equal(one.text, "ctx · laptop · 3 live · nothing waiting · pool on");
  assert.equal(two.text, "today $206.54 · cloud $202.80 · local $3.73 · $530 left");
  assert.equal(two.tone, "dim");                                  // the header's budget_style
  const waiting = headerLines({ ...board.header, waiting: 2, pool: null }, { host: "gpu-host" })[0];
  assert.equal(waiting.text, "ctx · gpu-host · 3 live · 2 waiting on you");
  const empty = headerLines({ spend_today: {} }, {})[1];
  assert.equal(empty.text, "today — · cloud — · local — · —");
});

test("detailSections: the Textual board's order, empties dropped", () => {
  assert.equal(health.change, board.change);                      // the poll's trigger matches the samples
  const titles = detailSections(detail, { now: NOW }).map((s) => s.title);
  assert.deepEqual(titles, [
    "RECAP", "SPEND", "POOL JOBS", "INFO", "LAST EXCHANGE", "SINCE YOU LOOKED",
    "YOUR INSTRUCTIONS", "AGENT SAYS", "DECISIONS", "EARLIER",
  ]);
  assert.equal(detailSections({}, { now: NOW }).length, 0);       // an empty card is no sections
});

test("detailSections: RECAP says (updating…) only when stale", () => {
  const of = (recap) => detailSections({ ...detail, recap }, { now: NOW })[0].title;
  assert.equal(of(detail.recap), "RECAP");
  assert.equal(of({ ...detail.recap, stale: true }), "RECAP (updating…)");
  const recap = detailSections(detail, { now: NOW })[0];
  assert.match(recap.lines[0].text, /Phase 1 plumbing is live/);
  assert.match(recap.lines[1].text, /^› Building Phase 1/);
  assert.equal(detailSections({ ...detail, recap: { text: "", stale: true } }, { now: NOW })
    .some((s) => s.title.startsWith("RECAP")), false);
});

test("detailSections: WAITING ON YOU appears when an ask opens", () => {
  const withAsk = detailSections({ ...detail, waiting_on: [{ kind: "question", text: "Which herdr?" }] }, { now: NOW });
  const section = withAsk.find((s) => s.title === "WAITING ON YOU");
  assert.equal(section.lines[0].tone, "blocked");
  assert.match(section.lines[0].text, /\[question\] Which herdr\?/);
  assert.equal(detailSections(detail, { now: NOW }).some((s) => s.title === "WAITING ON YOU"), false);
});

test("detailSections: the rest of the card reads from the sample", () => {
  const by = Object.fromEntries(detailSections(detail, { now: NOW }).map((s) => [s.title, s]));
  assert.match(by["SPEND"].lines[0].text, /^cloud \$204\.93 · 200\.3M tokens/);
  assert.match(by["POOL JOBS"].lines[0].text, /^↳ remote-board\.md · no-commit · 32 turns ~\$0\.05$/);
  assert.equal(by["POOL JOBS"].lines.length, detail.pool_jobs.length);
  assert.match(by["INFO"].lines[0].text, /14m · 46 exch · home \/home\/dev/);
  assert.match(by["INFO"].lines[0].text, /auto$/);
  assert.match(by["INFO"].lines[1].text, /^pool: on/);
  assert.match(by["LAST EXCHANGE"].lines[0].text, /^you \(1h32\): you@laptop/);
  assert.match(by["LAST EXCHANGE"].lines[1].text, /^agent \(/);
  assert.equal(by["SINCE YOU LOOKED"].lines[0].text, detail.since_line);
  assert.equal(by["SINCE YOU LOOKED"].lines[0].tone, "accent");
  assert.equal(by["SINCE YOU LOOKED"].lines.length, 1 + detail.since_viewed.rows.slice(-5).length);
  assert.match(by["YOUR INSTRUCTIONS"].lines[0].text, /^1 • Digit keys highlight/);
  assert.match(by["AGENT SAYS"].lines[0].text, /^✓ /);
  assert.equal(by["AGENT SAYS"].lines[0].tone, "done");
  assert.match(by["DECISIONS"].lines[0].text, /^✓ /);
  assert.match(by["DECISIONS"].lines[1].text, /^→ /);
  assert.match(by["EARLIER"].lines[0].text, /^#44 \[task\] <task-notification>/);
  assert.equal(by["EARLIER"].lines.length, detail.earlier.length);
});

test("detailTitle: the card's own header line", () => {
  assert.deepEqual(detailTitle(detail), { glyph: "●", title: "ctx switch", state: "busy", tone: "working" });
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
