// The board's actions, tested as plain data: actions.js says what to fetch and which herdr commands to run,
// and nothing here needs gpui-kit, the `herdr` module or a hub. Run: node --test herdr/mods/ctx-board/test/

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  machineFor, terminalFor, paneFor, newestInSpace, spaceArgs, openPlan, startPlan, endPlan, messageRequest,
  retireRequest, hostCycle, boardPath, launchPath, hostChoices, harnessChoices, parseCommand, moveSelection,
} from "../actions.js";

const here = dirname(fileURLToPath(import.meta.url));
const board = JSON.parse(readFileSync(join(here, "fixtures", "board.json"), "utf8"));

// ctx.json as a person with one machine off this computer would write it: only the other hosts are named.
const CFG = { hub_url: "http://laptop:4000/v1", token: "t", host_id: "laptop", machines: { "gpu-host": "vps" } };

// `hc ls --json` records, the shape terminals() answers with: address, labels, state, and the space it lives in.
const t = (address, extra = {}) => ({ address, state: "live", labels: {}, seq: 1, ...extra });
const LABELED = t("t7", { labels: { "ctx.key": "laptop/claude/7f3a91c2" }, space: "s2", seq: 3 });
const PARKED = t("t8", { labels: { "ctx.key": "laptop/claude/7f3a91c2" }, state: "parked" });
const LOCAL_HC = t("t9", { space: "s4" });
const REMOTE_HC = t("vps/t3", { space: "s5" });
const LOCAL_SHELL = t("t1");
const REMOTE_SHELL = t("vps/t1");

const row = (over = {}) => ({
  key: "laptop/claude/7f3a91c2", host_id: "laptop", harness: "claude", state: "busy", title: "OAuth login",
  hc_at: null, can_message: null, reply_target: null, reply_why: null, ...over,
});
const recipe = (over = {}) => ({
  host: "laptop", harness: "claude", profile: "bedrock", argv: ["claude", "--resume", "fdf857"],
  cwd: "/home/dev/scratch/x", label: "claude[bedrock]", key: null, installed: true, ...over,
});
const cmd = (command, args) => ({ command, args });
const launchStep = (machine) => ({ command: "space.new", from: "recipe", as: "made", machine });
const LABEL = { label_new: true };
// The hub's `{key:path}` routes take the key as it is; `at` is this instant, so it comes out of the plan before
// anything is compared, and gets checked on its own once.
const viewed = (key) => ({ fetch: { method: "POST", path: `/v1/sessions/${key}/viewed`, body: { at: "NOW" } } });
const noNow = (plan) => (plan.extra?.[0]?.fetch?.path?.endsWith("/viewed")
  ? { ...plan, extra: [{ fetch: { ...plan.extra[0].fetch, body: { at: "NOW" } } }, ...plan.extra.slice(1)] }
  : plan);

// ── finding the terminal ──────────────────────────────────────────────────────────────────────────────

test("machineFor: only the map's other hosts name a machine", () => {
  assert.equal(machineFor(CFG, "gpu-host"), "vps");
  assert.equal(machineFor(CFG, "laptop"), "");              // this computer: no entry
  assert.equal(machineFor(CFG, "devbox"), "");              // unmapped: read as here
  assert.equal(machineFor({ host_id: "laptop" }, "gpu-host"), "");
  assert.equal(machineFor(null, "gpu-host"), "");
  assert.equal(machineFor(CFG, null), "");
  assert.equal(machineFor({ machines: { "gpu-host": null } }, "gpu-host"), "");
});

test("terminalFor: the ctx.key label wins, anywhere and on any host", () => {
  assert.equal(terminalFor(row(), [LOCAL_SHELL, LABELED], CFG), LABELED);
  assert.equal(terminalFor(row({ host_id: "gpu-host" }), [LABELED], CFG), LABELED);   // labelled: no map consulted
  assert.equal(terminalFor(row({ key: "laptop/claude/dead" }), [LABELED], CFG), null);
  assert.equal(terminalFor(row(), [], CFG), null);
  assert.equal(terminalFor(null, [LABELED], CFG), null);
});

test("terminalFor: a parked terminal with the label is not the live one", () => {
  assert.equal(terminalFor(row(), [PARKED], CFG), null);
  assert.equal(terminalFor(row(), [PARKED, LABELED], CFG), LABELED);
  assert.equal(terminalFor(row(), [t("t2", { state: "ended", labels: { "ctx.key": "laptop/claude/7f3a91c2" } })], CFG), null);
});

test("terminalFor: no label, so hc_at — plain here, machine-prefixed through ctx.json's map", () => {
  assert.equal(terminalFor(row({ hc_at: "t9" }), [LOCAL_HC, REMOTE_HC], CFG), LOCAL_HC);
  assert.equal(terminalFor(row({ host_id: "gpu-host", hc_at: "t3" }), [LOCAL_HC, REMOTE_HC], CFG), REMOTE_HC);
  // an address that already carries some other machine's prefix still matches the row's hc_at
  assert.equal(terminalFor(row({ host_id: "gpu-host", hc_at: "t3" }), [t("wire/t3")], CFG).address, "wire/t3");
  assert.equal(terminalFor(row({ hc_at: "t9" }), [LOCAL_SHELL], CFG), null);
  assert.equal(terminalFor(row({ hc_at: "t9" }), [t("t9", { state: "ended" })], CFG), null);
  assert.equal(terminalFor(row({ hc_at: null }), [LOCAL_HC], CFG), null);
});

test("terminalFor: a labelled terminal still wins over hc_at", () => {
  assert.equal(terminalFor(row({ hc_at: "t9" }), [LOCAL_HC, LABELED], CFG), LABELED);
});

test("paneFor / newestInSpace: what space.new made and where it lives", () => {
  const panes = [{ pane: "p1", terminal: "t7" }, { pane: "p2", terminal: "t9" }, { pane: "p3", terminal: null }];
  assert.equal(paneFor(panes, "t9"), "p2");
  assert.equal(paneFor(panes, "t404"), null);
  assert.equal(paneFor(panes, null), null);
  const inSpace = [t("t1", { space: "s9", seq: 1 }), t("t4", { space: "s9", seq: 8 }), t("t5", { space: "s9", seq: 3 })];
  assert.equal(newestInSpace(inSpace, "s9").address, "t4");
  assert.equal(newestInSpace(inSpace, "s404"), null);
  assert.equal(newestInSpace(inSpace, null), null);
  assert.equal(newestInSpace([t("t4", { space: "s9", state: "ended" })], "s9"), null);
  // no seq to compare by: the higher address is the later one
  assert.equal(newestInSpace([t("t1", { space: "s9" }), t("t2", { space: "s9" })], "s9").address, "t2");
});

test("spaceArgs: a launch recipe as space.new's arguments", () => {
  assert.deepEqual(spaceArgs(recipe(), CFG),
    { name: "claude[bedrock]", machine: "", folder: "/home/dev/scratch/x", argv: ["claude", "--resume", "fdf857"] });
  assert.deepEqual(spaceArgs(recipe({ host: "gpu-host" }), CFG),
    { name: "claude[bedrock]", machine: "vps", folder: "/home/dev/scratch/x", argv: ["claude", "--resume", "fdf857"] });
  assert.deepEqual(spaceArgs({ label: "shell", host: "devbox" }, CFG),
    { name: "shell", machine: "", folder: "~", argv: [] });       // no cwd: that machine's home
});

// ── open (Enter / double-click) ───────────────────────────────────────────────────────────────────────

test("openPlan: its terminal is here and has a pane — just show it", () => {
  const plan = noNow(openPlan(row(), [LABELED], CFG, "p1"));
  assert.deepEqual(plan, { steps: [cmd("agent.focus", { terminal: "t7" })], extra: [viewed("laptop/claude/7f3a91c2")] });
  assert.match(openPlan(row(), [LABELED], CFG).extra[0].fetch.body.at, /^\d{4}-\d{2}-\d{2}T/);   // now, as ISO
});

test("openPlan: its terminal is here without a pane — keep it somewhere, then show it", () => {
  const plan = noNow(openPlan(row(), [LABELED, LOCAL_HC], CFG, null));
  assert.deepEqual(plan, { steps: [cmd("terminal.keep", { terminal: "t7", place: "tab" }), cmd("agent.focus", { terminal: "t7" })],
    extra: [viewed("laptop/claude/7f3a91c2")] });
});

test("openPlan: ended and not here — ask the hub how to resume, then start that space on its host", () => {
  const plan = noNow(openPlan(row({ state: "ended", hc_at: null }), [LOCAL_SHELL], CFG));
  assert.deepEqual(plan, {
    steps: [{ fetch: { method: "GET", path: "/v1/launch?key=laptop%2Fclaude%2F7f3a91c2", into: "recipe" } },
      launchStep(""), LABEL],
    extra: [viewed("laptop/claude/7f3a91c2")],
  });
  assert.deepEqual(noNow(openPlan(row({ state: "ended", host_id: "gpu-host" }), [LOCAL_SHELL], CFG)).steps[1],
    launchStep("vps"));                                            // ctx.json says gpu-host is the machine "vps"
  assert.deepEqual(noNow(openPlan(row({ state: "ended", host_id: "devbox" }), [LOCAL_SHELL], CFG)).steps[1],
    launchStep(""));                                               // unmapped: this computer, said plainly
});

test("openPlan: live and not here — it runs in someone else's herdr", () => {
  const plan = noNow(openPlan(row({ host_id: "devbox" }), [LOCAL_SHELL], CFG));
  assert.deepEqual(plan, {
    steps: [], extra: [viewed("laptop/claude/7f3a91c2")],
    notify: { title: "ctx", body: "running elsewhere: open it on devbox", level: "info" },
  });                                                              // viewed still goes: it clears the +N
});

// ── start (n / s) ─────────────────────────────────────────────────────────────────────────────────────

test("startPlan: a recipe is one space.new; a key nothing else carries also gets labelled on", () => {
  assert.deepEqual(startPlan(recipe(), CFG).steps, [{ ...cmd("space.new",
    { name: "claude[bedrock]", machine: "", folder: "/home/dev/scratch/x", argv: ["claude", "--resume", "fdf857"] }), as: "made" }]);
  // a recipe that names no key starts nothing extra: the collector labels the session it can see anyway
  assert.deepEqual(startPlan(recipe({ key: null }), CFG).steps.length, 1);

  const fresh = startPlan(recipe({ key: "laptop/shell/<uuid>", host: "gpu-host", label: "shell", cwd: "~", argv: ["ctx", "shell"] }), CFG);
  assert.deepEqual(fresh.steps, [
    { command: "space.new", args: { name: "shell", machine: "vps", folder: "~", argv: ["ctx", "shell"] }, as: "made" }, LABEL,
  ]);
  assert.deepEqual(startPlan(null, CFG).steps,
    [{ command: "space.new", args: { name: "", machine: "", folder: "~", argv: [] }, as: "made" }]);
});

test("hostChoices / harnessChoices: the picker's steps, from the hosts' own words", () => {
  const hosts = [{ host_id: "devbox", info: {} }, { host_id: "gpu-host", info: {} }, { host_id: "laptop", info: {} }];
  assert.deepEqual(hostChoices(hosts, "laptop"), ["laptop", "devbox", "gpu-host"]);
  assert.deepEqual(hostChoices(["devbox", "gpu-host"], "laptop"), ["devbox", "gpu-host"]);   // not listed: as-is
  assert.deepEqual(hostChoices(null, "laptop"), []);

  assert.deepEqual(harnessChoices({ profiles: [{ name: "bedrock" }, { name: "work" }], agents: { claude: true, pi: true, agy: true, ctx: true } }),
    [{ harness: "claude", profile: "bedrock", label: "claude · bedrock" },
      { harness: "claude", profile: "work", label: "claude · work" },
      { harness: "pi", label: "pi" }, { harness: "shell", label: "shell" }]);                 // agy: the hub's word, not a choice
  assert.deepEqual(harnessChoices({ agents: { claude: true } }), [{ harness: "claude", label: "claude" }]);
  assert.deepEqual(harnessChoices({ agents: { ctx: true } }), [{ harness: "shell", label: "shell" }]);
  assert.deepEqual(harnessChoices({ agents: {} }), []);
  assert.deepEqual(harnessChoices(null), []);
});

test("launchPath: the picker's answer as the hub's question", () => {
  assert.equal(launchPath({ host: "laptop", harness: "claude", profile: "bedrock" }, "~/api"),
    "/v1/launch?host=laptop&harness=claude&profile=bedrock&cwd=~%2Fapi");
  assert.equal(launchPath({ host: "gpu-host", harness: "shell" }, ""), "/v1/launch?host=gpu-host&harness=shell");
  assert.equal(launchPath({ host: "a b", harness: "c&d" }, null), "/v1/launch?host=a%20b&harness=c%26d");
});

// ── end (k) ───────────────────────────────────────────────────────────────────────────────────────────

test("endPlan: kill its terminal, or say where it actually runs", () => {
  assert.deepEqual(endPlan(row(), [LABELED], CFG), { steps: [cmd("terminal.kill", { terminal: "t7" })] });
  const away = endPlan(row({ host_id: "devbox" }), [LOCAL_SHELL], CFG);
  assert.deepEqual(away, { steps: [], notify: { title: "ctx", body: "not in herdr: end it where it runs", level: "warning" } });
  assert.deepEqual(endPlan(row(), [LABELED], null).steps, []);      // no ctx.json: never kill by guesswork
  assert.deepEqual(endPlan(null, [LABELED], CFG).steps, []);
});

// ── message (m) / answer (a) ──────────────────────────────────────────────────────────────────────────

test("messageRequest: the POST, with this computer's host_id as the sender", () => {
  assert.deepEqual(messageRequest(row(), "keep going", CFG),
    { path: "/v1/sessions/laptop/claude/7f3a91c2/messages", body: { body: "keep going", from_host: "laptop" } });
  assert.deepEqual(messageRequest(row({ key: board.rows[0].key }), "x", { host_id: "devbox" }),
    { path: `/v1/sessions/${board.rows[0].key}/messages`, body: { body: "x", from_host: "devbox" } });
  assert.deepEqual(messageRequest(row(), "x", {}),
    { path: "/v1/sessions/laptop/claude/7f3a91c2/messages", body: { body: "x", from_host: null } });
});

test("messageRequest: an answer carries the hub's own target ref", () => {
  const asking = row({ reply_target: { ref: "1:plain", kind: "plain", text: "Token bucket or sliding window?" } });
  assert.deepEqual(messageRequest(asking, "sliding window", CFG, "a"), {
    path: "/v1/sessions/laptop/claude/7f3a91c2/messages",
    body: { body: "sliding window", from_host: "laptop", answers: "1:plain" },
  });
  assert.deepEqual(messageRequest(row({ reply_why: "nothing to answer: it isn't asking you anything" }),
    "sliding window", CFG, "a"), { refuse: "nothing to answer: it isn't asking you anything" });   // the hub's own words
  assert.deepEqual(messageRequest(row(), "sliding window", CFG, "a"), { refuse: "nothing to answer" });
});

test("messageRequest: the refusals are the hub's reasons, said before anything is posted", () => {
  assert.deepEqual(messageRequest(row(), "", CFG), { refuse: "nothing to send" });
  assert.deepEqual(messageRequest(row(), "   \n", CFG), { refuse: "nothing to send" });
  assert.deepEqual(messageRequest(row(), null, CFG), { refuse: "nothing to send" });
  assert.deepEqual(messageRequest(row({ can_message: "it's waiting on a permission dialog: answer it in the session" }),
    "hi", CFG), { refuse: "it's waiting on a permission dialog: answer it in the session" });
  // can_message is the stricter rule, so it beats a target that happens to be there
  assert.deepEqual(messageRequest(row({ can_message: "ended", reply_target: { ref: "1:plain" } }), "hi", CFG, "a"),
    { refuse: "ended" });
  assert.deepEqual(messageRequest(row({ can_message: null }), "hi", CFG),
    { path: "/v1/sessions/laptop/claude/7f3a91c2/messages", body: { body: "hi", from_host: "laptop" } });
});

// ── retire (i) ────────────────────────────────────────────────────────────────────────────────────────

test("retireRequest: the ledger row's id retires it", () => {
  assert.deepEqual(retireRequest(row(), { n: 1, id: 1, text: "Don't touch the billing endpoints" }),
    { path: "/v1/sessions/laptop/claude/7f3a91c2/ledger/1/retire", body: {} });
  assert.deepEqual(retireRequest(row({ key: board.rows[2].key }), { n: 3 }),    // no id: its number is the row
    { path: `/v1/sessions/${board.rows[2].key}/ledger/3/retire`, body: {} });
  assert.deepEqual(retireRequest(row(), {}), { refuse: "no such instruction" });
  assert.deepEqual(retireRequest(row(), null), { refuse: "no such instruction" });
});

// ── host filter (h) and the board's query ─────────────────────────────────────────────────────────────

test("hostCycle: this host → each other host → every host → back here", () => {
  const seen = [];                                            // one full turn of the key, as the header lists them
  let at = "laptop";
  for (let i = 0; i < 4; i++) { at = hostCycle(["laptop", "gpu-host", "devbox"], at, "laptop"); seen.push(at); }
  assert.deepEqual(seen, ["gpu-host", "devbox", null, "laptop"]);
  assert.equal(hostCycle(["laptop"], "laptop", "laptop"), null);                    // nothing else: this host ↔ all
  assert.equal(hostCycle(["laptop", "gpu-host"], null, "laptop"), "laptop");        // this host is listed: back here
  assert.equal(hostCycle(["gpu-host"], null, "laptop"), "gpu-host");                // this host has no news: start there
  assert.equal(hostCycle(["gpu-host", "devbox"], "devbox", "laptop"), null);        // the last other host → all
  assert.equal(hostCycle([], null, "laptop"), "laptop");
  assert.equal(hostCycle(null, null, null), null);
});

test("boardPath: the filter state as the hub's query", () => {
  // A host is a name; "" is every host; absent means this computer's own, which is where the filter starts.
  assert.equal(boardPath({}, CFG), "/v1/board?host=laptop&screen_host=laptop&ended=0&tz=%2B00%3A00");
  assert.equal(boardPath({ host: "laptop", ended: true, tz: "-05:30" }, CFG),
    "/v1/board?host=laptop&screen_host=laptop&ended=1&tz=-05%3A30");
  assert.equal(boardPath({ host: "" }, CFG), "/v1/board?host=&screen_host=laptop&ended=0&tz=%2B00%3A00");
  assert.equal(boardPath({ host: "gpu-host" }, CFG), "/v1/board?host=gpu-host&screen_host=laptop&ended=0&tz=%2B00%3A00");
  assert.equal(boardPath({}, {}), "/v1/board?host=&screen_host=&ended=0&tz=%2B00%3A00");
});

// ── keys.json's commands, and the selection they move ─────────────────────────────────────────────────

test("parseCommand: a mod's own namespaced command, its shorthand after a colon", () => {
  assert.deepEqual(parseCommand("ctx-board.select:3"), { command: "select", index: 3 });
  assert.deepEqual(parseCommand("ctx-board.select:0"), { command: "select", index: null });
  assert.deepEqual(parseCommand("ctx-board.select:10"), { command: "select", index: null });
  assert.deepEqual(parseCommand("ctx-board.select:x"), { command: "select", index: null });
  assert.deepEqual(parseCommand("ctx-board.open"), { command: "open", index: null });
  assert.deepEqual(parseCommand("ctx-board.start"), { command: "start", index: null });
  assert.deepEqual(parseCommand("ctx-board.clear"), { command: "clear", index: null });
  assert.deepEqual(parseCommand("focus"), { command: "focus", index: null });
  assert.deepEqual(parseCommand(""), { command: "", index: null });
  assert.deepEqual(parseCommand(null), { command: "", index: null });
});

test("moveSelection: digits jump, up/down walk, both over the rows as shown", () => {
  const keys = board.rows.map((r) => r.key);
  assert.equal(moveSelection(board.rows, null, 0, 3), keys[2]);
  assert.equal(moveSelection(board.rows, keys[0], 1), keys[1]);
  assert.equal(moveSelection(board.rows, keys[2], -1), keys[1]);
  assert.equal(moveSelection(board.rows, keys[0], -1), keys[0]);       // already first
  assert.equal(moveSelection(board.rows, keys[5], 1), keys[5]);        // already last
  assert.equal(moveSelection(board.rows, null, 1), keys[0]);           // nothing chosen yet
  assert.equal(moveSelection(board.rows, "gone/claude/0000", 1), keys[0]);
  assert.equal(moveSelection(board.rows, keys[1], 0, 9), null);        // no ninth row
  assert.equal(moveSelection([], keys[0], 1), null);
});
