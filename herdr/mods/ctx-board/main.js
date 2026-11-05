// The ctx board as a herdr-app mod: the hub's view model, painted, and the keys that act on it.
// The mod holds no rules: what to show comes from GET /v1/board, what to run comes from GET /v1/launch, and
// actions.js turns those answers into {command, args} steps. Everything here is plumbing — fetch, run, paint.

import { View, div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import {
  appHome, readTextFile, theme, nextCommand, run, hc, spaces, terminals, panes, openOverlay, closeOverlay, notify, props,
} from "herdr";
import { rowParts, headerLines, detailSections, detailTitle, toneColor, hasBold, tzOffset } from "./view.js";
import {
  openPlan, startPlan, endPlan, messageRequest, sendCheck, retireRequest, terminalFor, paneFor, newestInSpace, parseCommand,
  moveSelection, boardPath, launchPath, hostChoices, harnessChoices, hostCycle, spaceArgs,
} from "./actions.js";

const POLL_MS = 2000;
const PICKER = { width: 360, height: 280 };
const DOUBLE_MS = 500;                                              // two clicks on one row this close apart = open

export default class CtxBoard extends View {
  init(_props, cx) {
    const p = props() ?? {};
    this.overlay = p.overlay ?? null;                               // set when this copy is a popup
    this.action = p.action ?? null;                                 // that popup's pending work, as plain data
    this.chain = p.action?.kind === "start" ? { hosts: p.action.hosts ?? [], me: p.action.me ?? "", host: null, harness: null, profile: null } : null;
    this.cfg = null;
    this.cfgError = null;
    this.rows = [];
    this.header = null;
    this.change = -1;
    this.selected = null;
    this.detail = null;
    this.error = null;
    this.size = null;
    this.dirty = true;
    this.notifiedAt = 0;
    this.filter = { host: null, ended: false };                     // null: this computer's host, as ctx.json names it
    this.popup = null;                                              // a popup this copy opened, so keys go to it
    this.clickedAt = 0;
    this.input = null;
    if (p.action?.kind === "send") this.makeInput("");               // state is made here or in a handler, never in render
    this.handle = cx.focus_handle();
    // mod.json says focusable: pane.go can step onto this copy; it takes the keyboard on "focus".
    cx.spawn(async (cx) => {
      for (;;) {
        const c = await nextCommand();
        if (c === "focus") { this.popup = null; this.handle.focus(); }   // also what we get when our popup closes
        else this.command(c, cx);
        cx.notify();
      }
    });
    if (!this.overlay) {                                            // a popup polls nothing; the board does
      cx.timer.every(POLL_MS, (cx) => this.poll(cx));
      this.poll(cx);
    }
  }

  // ctx.json: {hub_url, token, host_id, machines}. The token only ever goes into a header.
  async config() {
    if (this.cfg || this.cfgError) return this.cfg;
    try {
      const text = await readTextFile(`${appHome()}/ctx.json`);
      if (!text) throw new Error("no ctx.json");
      const cfg = JSON.parse(text);
      if (!cfg.hub_url) throw new Error("ctx.json has no hub_url");
      this.cfg = cfg;
    } catch (e) {
      this.cfgError = String(e?.message ?? e);
    }
    return this.cfg;
  }

  async get(path) {
    const r = await fetch(this.url(path), { headers: this.headers() });
    return this.answer(r, path);
  }

  async send(path, body) {
    const r = await fetch(this.url(path), {
      method: "POST", headers: { ...this.headers(), "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}),
    });
    return this.answer(r, path);
  }

  url(path) {
    return `${String(this.cfg.hub_url).replace(/\/+$/, "")}${path}`;
  }

  headers() {
    return { Authorization: `Bearer ${this.cfg.token ?? ""}` };
  }

  // A hub answer is either the JSON or {"error": …}; the status only says which corner we are in.
  async answer(r, path) {
    const json = await r.json().catch(() => null);
    if (!r.ok) throw new Error(json?.error ?? `${path}: HTTP ${r.status}`);
    return json;
  }

  // A fetch every 2 s, but a redraw only when something actually changed — plus one every 30 s
  // so the ages on the rows don't go stale.
  async poll(cx) {
    try {
      const cfg = await this.config();
      if (cfg) {
        const health = await this.get("/v1/health");
        if (health.change !== this.change) {
          this.change = health.change;
          const board = await this.get(boardPath({ ...this.filter, tz: tzOffset() }, cfg));
          this.rows = board.rows ?? [];
          this.header = board.header ?? null;
          if (this.selected) this.detail = await this.get(this.detailPath(this.selected));
          this.dirty = true;
        }
        if (this.error) { this.error = null; this.dirty = true; }
      }
    } catch (e) {
      const msg = String(e?.message ?? e);
      if (msg !== this.error) { this.error = msg; this.dirty = true; }
    }
    const now = Date.now();
    if (this.dirty || now - (this.notifiedAt ?? 0) > 30000) {
      this.dirty = false;
      this.notifiedAt = now;
      cx.notify();
    }
  }

  detailPath(key) {
    return `/v1/board/${String(key ?? "")}?tz=${encodeURIComponent(tzOffset())}`;
  }

  select(key, cx) {
    this.selected = key;
    this.detail = null;
    cx.spawn(async (cx) => {
      try {
        if (await this.config()) this.detail = await this.get(this.detailPath(key));
      } catch (e) {
        this.error = String(e?.message ?? e);
      }
      cx.notify();
    });
  }

  row(key) {
    const walk = (rows) => {
      for (const r of rows ?? []) {
        if (r.key === key) return r;
        const kid = walk(r.children);
        if (kid) return kid;
      }
      return null;
    };
    return walk(this.rows);
  }

  // A key command for this mod ("<mod>.<command>", from keys.json), or a click that names the same thing.
  command(c, cx) {
    const { command, index } = parseCommand(c);
    if (this.overlay) {                                             // a popup copy: digits choose, Esc closes
      if (command === "select" && index) this.pick(this.overlayChoices()[index - 1]?.value, cx);
      else if (command === "clear") closeOverlay(this.overlay);
      else if (command === "open" && this.overlayIsText()) this.submitText(cx);
      return;
    }
    if (this.popup && command !== "clear") return;                  // our own popup is up: its copy has the keys
    switch (command) {
      case "focus": this.handle.focus(); break;
      case "open": this.open(this.selected, cx); break;
      case "start": this.start(cx); break;
      case "message": this.compose("m", this.selected, cx); break;
      case "answer": this.compose("a", this.selected, cx); break;
      case "retire": this.retire(this.selected, cx); break;
      case "end": this.confirmEnd(this.selected, cx); break;
      case "host": this.cycleHost(cx); break;
      case "ended": this.filter.ended = !this.filter.ended; this.refresh(cx); break;
      case "clear":                                                // Esc: a popup first, else back to the plain view
        if (this.popup) { closeOverlay(this.popup); this.popup = null; break; }
        this.filter = { host: null, ended: false }; this.selected = null; this.detail = null;
        this.refresh(cx); break;
      case "prev": this.move(-1, cx); break;
      case "next": this.move(1, cx); break;
      case "select": if (index) this.select(moveSelection(this.rows, this.selected, 0, index), cx); break;
      default: break;
    }
  }

  refresh(cx) {
    this.change = -1;                                               // fetch this state now, not on the next tick
    this.poll(cx);
  }

  move(delta, cx) {
    const key = moveSelection(this.rows, this.selected, delta);
    if (key && key !== this.selected) this.select(key, cx);
  }

  // this host → each other host → every host → back here. "" is the hub's word for every host.
  cycleHost(cx) {
    const hosts = this.header?.hosts ?? [];
    const next = hostCycle(hosts.length ? hosts : [this.cfg?.host_id ?? ""], this.filter.host, this.cfg?.host_id);
    this.filter.host = next === null ? "" : next;
    this.refresh(cx);
  }

  // Every action goes through here: actions.js decides, this carries it out.
  async act(plan, cx, recipe = null) {
    if (!plan) return;
    const made = {};
    let before = null;
    for (const step of plan.steps ?? []) {
      if (step.fetch) {
        const json = await this.get(step.fetch.path);
        if (step.fetch.into) made[step.fetch.into] = json;
        continue;
      }
      if (step.label_new) { await this.labelNew(made.recipe ?? recipe, before, cx); continue; }
      if (step.command === "hc") { await hc(step.args ?? []); continue; }
      const args = step.from
        ? { ...spaceArgs(made[step.from], this.cfg), ...(step.machine === undefined ? {} : { machine: step.machine }) }
        : step.args ?? {};
      if (step.command === "space.new") before = new Set(spaces().map((s) => s?.id ?? ""));
      run(step.command, args);
    }
    for (const extra of plan.extra ?? []) {
      if (extra.fetch) await this.send(extra.fetch.path, extra.fetch.body).catch(() => null);
    }
    if (plan.notify) notify(plan.notify);
    cx?.notify();
  }

  // The terminal space.new just made gets the session's key, so the next board read finds it by label.
  // `run()` answers null, so what it made is whichever space wasn't there a moment ago — and core takes a
  // new terminal on a beat after the command returns, so this waits for it rather than guessing.
  labelNew(recipe, spacesBefore, cx) {
    const key = recipe?.key;
    if (!key) return Promise.resolve();
    if (!cx) { notify({ title: "ctx", body: `started: label ${key} once the terminal shows`, level: "warning" }); return Promise.resolve(); }
    return new Promise((done) => {
      let tries = 0;
      const look = () => {
        const fresh = (spaces() ?? []).find((s) => !spacesBefore?.has(s?.id ?? ""));
        const t = fresh ? newestInSpace(terminals(), fresh.id) : null;
        if (t?.address) {
          hc(["label", String(t.address), `ctx.key=${key}`]).catch(() => null).then(() => done());
          return;
        }
        if (++tries >= 12) {
          notify({ title: "ctx", body: `started: couldn't label the terminal for ${key}`, level: "warning" });
          done();
          return;
        }
        cx.timer.after(150, look);
      };
      look();
    });
  }

  open(key, cx) {
    const row = this.row(key);
    if (!row) return;
    cx.spawn(async (cx) => {
      try {
        if (!await this.config()) return;
        const t = terminalFor(row, terminals(), this.cfg);
        const pane = t ? paneFor(panes(), String(t.address ?? "")) : null;
        await this.act(openPlan(row, terminals(), this.cfg, pane), cx);
      } catch (e) {
        this.toast(e);
      }
    });
  }

  // `n` / `s`: host, then what to run there, then a folder. The choices are the hosts' own published words.
  start(cx) {
    cx.spawn(async (cx) => {
      try {
        const cfg = await this.config();
        if (!cfg) return;
        const hosts = (await this.get("/v1/hosts")) ?? [];
        this.openPopup({ kind: "start", hosts, me: cfg.host_id ?? "" }, cx);
      } catch (e) {
        this.toast(e);
      }
      cx.notify();
    });
  }

  // `m` / `a`: the hub's own reason not to comes first, then one line to send.
  compose(mode, key, cx) {
    const row = this.row(key);
    if (!row) return;
    const ask = sendCheck(row, mode);
    if (ask.refuse) {
      notify({ title: mode === "a" ? "answer" : "message", body: ask.refuse, level: "warning" });
      return;
    }
    const hint = mode === "a" ? row.reply_target?.text ?? "" : row.title ?? "";
    this.openPopup({ kind: "send", mode, row, title: `${mode === "a" ? "answer" : "message"} · ${hint}` }, cx);
  }

  // `i`: the session's standing instructions, oldest first; a click retires that one.
  retire(key, cx) {
    const row = this.row(key);
    if (!row) return;
    if (this.detail?.key === key) { this.retireFrom(row, this.detail.instructions ?? [], cx); return; }
    cx.spawn(async (cx) => {                                        // the card isn't in yet: ask for it, then list
      try {
        if (await this.config()) this.detail = await this.get(this.detailPath(key));
        this.retireFrom(row, this.detail?.instructions ?? [], cx);
      } catch (e) {
        this.toast(e);
      }
      cx.notify();
    });
  }

  retireFrom(row, instructions, cx) {
    if (!instructions.length) {
      notify({ title: "ctx", body: "no standing instructions", level: "info" });
      return;
    }
    this.openPopup({ kind: "retire", row, instructions, title: "retire which instruction?" }, cx);
  }

  // `k`: a yes/no over the row, then terminal.kill — or where it actually runs.
  confirmEnd(key, cx) {
    const row = this.row(key);
    if (!row) return;
    this.openPopup({ kind: "end", row, title: `end ${row.title || row.key} on ${row.host_id || "?"}?` }, cx);
  }

  // A popup is this same mod with plain props: whichever copy shows one carries out its own action.
  openPopup(action, cx) {
    const id = openOverlay({ mod: "ctx-board", kind: "popup", ...PICKER, props: { action } });
    if (!this.overlay) this.popup = id;                             // only the board copy tracks its own popup
    cx?.notify();
  }

  // The start picker steps inside one popup — hosts, then what to run, then a folder — because a popup that
  // opens another hands the keyboard to whichever copy shows it, and two copies answering the same key is
  // never right. The steps are this copy's own state; props only carry where it began.
  overlayChoices() {
    const a = this.action ?? {};
    const c = this.chain ?? {};
    if (a.kind === "start") {
      if (!c.host) return hostChoices(c.hosts, c.me).map((h) => ({
        label: h === c.me ? `${h} (this computer)` : h, value: { host: h },
      }));
      const info = (c.hosts ?? []).find((h) => h?.host_id === c.host)?.info ?? {};
      return harnessChoices(info).map((x) => ({ label: x.label, value: x }));
    }
    if (a.kind === "retire") {
      return (a.instructions ?? []).map((r) => ({ label: `${r.n} • ${r.text ?? ""}`, value: { instruction: r } }));
    }
    if (a.kind === "end") return [{ label: "yes — kill its terminal", value: { yes: true } }, { label: "no", value: {} }];
    return [];
  }

  // The picker's last step, and the message/answer field, are one text input.
  overlayIsText() {
    const a = this.action ?? {};
    return a.kind === "send" || (a.kind === "start" && Boolean(this.chain?.harness));
  }

  overlayTitle() {
    const a = this.action ?? {};
    const c = this.chain ?? {};
    if (a.kind === "start") {
      if (!c.host) return "start on which host?";
      if (!c.harness) return `start what on ${c.host}?`;
      return `folder for ${c.harness}${c.profile ? ` · ${c.profile}` : ""} on ${c.host}?`;
    }
    return String(a.title ?? "ctx");
  }

  pick(value, cx) {
    const a = this.action ?? {};
    if (!value) return;                                             // a digit past the end of the list
    if (a.kind === "start") {
      if (!this.chain) return;
      if (!this.chain.host) this.chain.host = value.host;
      else { this.chain.harness = value.harness; this.chain.profile = value.profile ?? null; this.makeInput("~"); }
      cx?.notify();
      return;
    }
    closeOverlay(this.overlay);
    if (a.kind === "retire") this.doRetire(a.row, value.instruction);
    else if (a.kind === "end" && value.yes) this.doEnd(a.row, cx);
  }

  // The text popup's button or Enter: whatever was typed goes through the same decision functions.
  submitText(cx) {
    const a = this.action ?? {};
    const c = this.chain ?? {};
    const text = String(this.input?.value?.() ?? "").trim();
    if (a.kind === "start" && !c.harness) return;                   // nothing chosen yet: nothing to start
    closeOverlay(this.overlay);
    if (a.kind === "send") this.doSend(a.row, text, a.mode ?? "m");
    else this.doLaunch({ host: c.host, harness: c.harness, profile: c.profile }, text || "~", cx);
  }

  async doSend(row, text, mode) {
    try {
      const req = messageRequest(row, text, await this.config(), mode);
      if (req.refuse) {
        notify({ title: mode === "a" ? "answer" : "message", body: req.refuse, level: "warning" });
        return;
      }
      await this.send(req.path, req.body);
      notify({ title: "ctx", body: "sent", level: "success" });
    } catch (e) {
      this.toast(e);
    }
  }

  async doRetire(row, instruction) {
    try {
      const req = retireRequest(row, instruction);
      if (req.refuse) { notify({ title: "ctx", body: req.refuse, level: "warning" }); return; }
      await this.send(req.path, req.body);
      notify({ title: "ctx", body: "retired", level: "success" });
    } catch (e) {
      this.toast(e);
    }
  }

  async doEnd(row, cx) {
    try {
      await this.act(endPlan(row, terminals(), await this.config()), cx);
    } catch (e) {
      this.toast(e);
    }
  }

  async doLaunch(choice, cwd, cx) {
    try {
      const cfg = await this.config();
      const recipe = await this.get(launchPath(choice, cwd));
      await this.act(startPlan(recipe, cfg), cx, recipe);
    } catch (e) {
      this.toast(e);
    }
  }

  toast(e) {
    notify({ title: "ctx", body: String(e?.message ?? e), level: "error" });
  }

  // Rows: one click selects, a second on the same row opens it. Keys come through keys.json.
  clickRow(key, cx) {
    const now = Date.now();
    const again = key === this.selected && now - this.clickedAt < DOUBLE_MS;
    this.clickedAt = now;
    if (again) { this.clickedAt = 0; this.open(key, cx); } else this.select(key, cx);
  }

  text2(t, part) {
    return div().text_color(t[toneColor(part.tone)] ?? t.muted).child(part.text ?? "");
  }

  headerView(t) {
    const all = this.filter.host === "";
    const lines = headerLines(this.header, { host: all ? "all hosts" : (this.filter.host ?? this.cfg?.host_id ?? "") });
    const flag = this.filter.ended ? "+ ended" : "";
    return v_flex().flex_none().px(10).pt(8).pb(6).gap(2).text_size(11.5)
      .child(this.text2(t, lines[0]))
      .child(this.text2(t, lines[1]))
      .when(flag, (el) => el.child(div().text_size(10).text_color(t.accent).child(flag)));
  }

  rowView(t, p, depth) {
    const on = this.selected === p.key;
    const row = h_flex().id(`ctx-row-${p.key}`).pl(8 + depth * 14).pr(8).h(24).flex_none().gap(6).items_center()
      .text_size(12.5).rounded(6).hover((s) => s.bg(t.hover))
      .when(on, (el) => el.bg(t.selected))
      .on_click((_e, cx) => this.clickRow(p.key, cx))
      .when(p.number !== null, (el) => el.child(div().w(14).flex_none().text_color(t.dim).child(String(p.number))))
      .child(div().w(12).flex_none().text_color(t[p.tone] ?? t.dim).child(p.glyph))
      .when(p.prefix, (el) => el.child(div().flex_none().text_color(t.dim).child(p.prefix)))
      .when(p.label, (el) => el.child(div().flex_none().text_size(10.5).text_color(t.muted).child(p.label)))
      .when(p.pool, (el) => el.child(div().flex_none().when(hasBold(p.pool.tone), (x) => x.font_semibold())
        .text_color(t[toneColor(p.pool.tone)] ?? t.dim).child(p.pool.text)))
      .child(div().flex_1().min_w(0).truncate().text_color(on ? t.foreground : t.muted).child(p.title))
      .when(p.age, (el) => el.child(div().flex_none().text_size(10.5).text_color(t.dim).child(p.age)))
      .when(p.askAge, (el) => el.child(div().flex_none().text_size(10.5).text_color(t.blocked).child(p.askAge)))
      .when(p.unseen, (el) => el.child(div().flex_none().text_size(10.5).text_color(t.accent).child(p.unseen)))
      .when(p.cost, (el) => el.child(div().flex_none().text_size(10.5).text_color(t.dim).child(p.cost)));
    const out = [row];
    if (p.waiting) {
      out.push(div().pl(8 + depth * 14 + 20).pr(8).text_size(11).text_color(t.blocked).flex_none().truncate()
        .child(`└ ${p.waiting}`));
    }
    for (const kid of p.children) out.push(...this.rowView(t, kid, depth + 1));
    return out;
  }

  listView(t) {
    this.parts = this.rows.map((r, i) => rowParts(r, { number: i + 1 }));
    const rows = this.parts.flatMap((p) => this.rowView(t, p, 0));
    return v_flex().id("ctx-list").flex_1().min_h(0).px(6).gap(1).overflow_y_scrollbar().children(rows);
  }

  detailView(t) {
    const max = this.size ? Math.max(120, Math.floor(this.size.height * 0.45)) : 260;
    const head = detailTitle(this.detail);
    const sections = detailSections(this.detail);
    return v_flex().id("ctx-detail").w_full().flex_none().max_h(max).min_h(0).mt(4).px(10).pb(8)
      .border_t(1).border_color(t.border).text_size(11.5).overflow_y_scrollbar()
      .child(h_flex().flex_none().gap(6).pt(6).pb(4).items_center()
        .child(div().flex_none().text_color(t[head.tone] ?? t.dim).child(head.glyph))
        .child(div().flex_1().min_w(0).truncate().font_semibold().child(head.title)))
      .children(sections.map((s) => v_flex().flex_none().gap(1).pb(5)
        .child(div().text_size(10).font_semibold().text_color(t.dim).child(s.title))
        .children(s.lines.map((l) => this.text2(t, l)))));
  }

  // The popup this mod draws as an overlay: a titled list of choices, or one text field.
  popupView(t) {
    const a = this.action ?? {};
    if (this.overlayIsText()) {
      return this.textOverlay(t, { title: this.overlayTitle() });
    }
    const list = this.overlayChoices();
    const c = this.chain ?? {};
    const empty = a.kind === "start" && c.host ? `${c.host} says it has nothing to start` : "nothing to choose here";
    return v_flex().id("ctx-pick").w_full().p(12).gap(4).bg(t.popover).text_size(12.5)
      .child(h_flex().pb(4).items_center()
        .child(div().flex_1().min_w(0).truncate().font_semibold().child(this.overlayTitle()))
        .child(div().text_size(10.5).text_color(t.dim).child("Esc")))
      .child(v_flex().id("ctx-pick-list").max_h(240).gap(1).overflow_y_scrollbar()
        .children(list.length
          ? list.map((ch, i) => h_flex().id(`ctx-pick-${i}`).h(28).px(8).rounded(6).gap(8).items_center()
            .hover((s) => s.bg(t.hover))
            .on_click((_e, cx) => this.pick(ch.value, cx))
            .child(div().w(14).flex_none().text_size(10.5).text_color(t.dim).child(i < 9 ? String(i + 1) : ""))
            .child(div().flex_1().min_w(0).truncate().child(ch.label ?? "")))
          : [div().p(8).text_size(11.5).text_color(t.dim).child(empty)]));
  }

  // The field holds its own text; it is read at Send. gpui-kit refuses state made during render, so the popup copy
  // makes it in init (a message) or in the pick that reaches the folder step (a start).
  makeInput(value) {
    this.input = InputState.new({ value: String(value ?? "") });
    this.input.on("submit", (_e, cx) => this.submitText(cx));
    this.input.focus();
  }

  textOverlay(t, a) {
    if (!this.input) return div().p(14).text_size(12).text_color(t.dim).child("…");
    return v_flex().id("ctx-ask").w_full().min_h(0).p(14).gap(8).bg(t.popover).text_size(12.5)
      .child(h_flex().items_center()
        .child(div().flex_1().min_w(0).truncate().font_semibold().child(String(a.title ?? "ctx")))
        .child(div().flex_none().text_size(10.5).text_color(t.dim).child("Enter sends · Esc closes")))
      .child(Input.new(this.input).h(34).px(10).rounded(8).border_1().border_color(t.accent).bg(t.background)
        .text_size(12.5).text_color(t.foreground))
      .child(h_flex().gap(8).justify_end().pt(4)
        .child(h_flex().id("ctx-ask-cancel").h(28).px(12).rounded(7).items_center().border_1().border_color(t.input)
          .bg(t.raised).text_color(t.foreground).on_click(() => closeOverlay(this.overlay)).child("Cancel"))
        .child(h_flex().id("ctx-ask-go").h(28).px(12).rounded(7).items_center().border_1().border_color(t.solid)
          .bg(t.solid).text_color(t.solid_foreground).on_click((_e, cx) => this.submitText(cx)).child("Send")));
  }

  render(_cx) {
    const t = theme();
    if (this.overlay) return this.popupView(t);
    const root = v_flex().id("ctx-root").size_full().min_h(0).bg(t.chrome).border_r(1).border_color(t.border)
      .track_focus(this.handle)
      .on_size_change((size, cx) => { this.size = size; cx.notify(); });
    if (this.cfgError || this.error) {
      return root.child(v_flex().flex_1().min_h(0).p(10).gap(6).text_size(12)
        .child(div().text_color(t.dim).child("ctx board"))
        .child(div().truncate().text_color(t.blocked)
          .child(`hub unreachable: ${this.cfgError ?? this.error}`)));
    }
    if (!this.cfg) return root.child(div().p(10).text_size(12).text_color(t.dim).child("loading…"));
    return root.child(this.headerView(t)).child(this.listView(t)).when(this.detail, (el) => el.child(this.detailView(t)));
  }
}
