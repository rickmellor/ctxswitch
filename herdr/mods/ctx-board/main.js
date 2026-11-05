// The ctx board as a herdr-app mod: the hub's view model, painted, and the keys that act on it.
// The mod holds no rules: what to show comes from GET /v1/board, what to run comes from GET /v1/launch, and
// actions.js turns those answers into {command, args} steps. Everything here is plumbing — fetch, run, paint.

import { View, div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import {
  appHome, readTextFile, theme, nextCommand, run, hc, terminals, panes, openOverlay, closeOverlay,
  notify, props,
} from "herdr";
import { rowParts, headerLines, detailSections, detailTitle, toneColor, hasBold, tzOffset } from "./view.js";
import {
  openPlan, startPlan, endPlan, messageRequest, retireRequest, terminalFor, paneFor, newestInSpace, parseCommand,
  moveSelection, boardPath, launchPath, hostChoices, harnessChoices, spaceArgs,
} from "./actions.js";

const POLL_MS = 2000;
const PICKER = { width: 360, height: 280 };

export default class CtxBoard extends View {
  init(_props, cx) {
    const p = props() ?? {};
    this.picker = p.picker ?? null;                       // set when this copy is the launch picker's overlay
    this.overlay = p.overlay ?? null;
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
    this.filter = { host: null, ended: false };           // null: this computer's host, as ctx.json names it
    this.hosts = [];
    this.text = null;                                     // the compose overlay's draft
    this.list = [];                                       // the list/menu overlay's choices
    this.busy = false;
    this.handle = cx.focus_handle();
    // mod.json says focusable: pane.go can step onto this copy; it takes the keyboard on "focus".
    cx.spawn(async (cx) => {
      for (;;) {
        const c = await nextCommand();
        if (c === "focus") this.handle.focus();
        else if (!this.picker) this.command(c, cx);
        cx.notify();
      }
    });
    if (this.picker) {
      this.list = this.picker.list ?? [];
    } else {
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
    return `/v1/board/${encodeURIComponent(String(key ?? ""))}?tz=${encodeURIComponent(tzOffset())}`;
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
    switch (command) {
      case "focus": this.handle.focus(); break;
      case "open": this.open(this.selected, cx); break;
      case "start": this.start(cx); break;
      case "message": this.compose("m", this.selected, cx); break;
      case "answer": this.compose("a", this.selected, cx); break;
      case "retire": this.retire(this.selected, cx); break;
      case "end": this.end(this.selected, cx); break;
      case "host": this.cycleHost(cx); break;
      case "ended": this.filter.ended = !this.filter.ended; this.change = -1; this.poll(cx); break;
      case "clear": this.filter = { host: null, ended: false }; this.selected = null; this.detail = null;
        this.change = -1; this.poll(cx); break;
      case "prev": this.move(-1, cx); break;
      case "next": this.move(1, cx); break;
      case "select": if (index) this.select(this.rows[index - 1]?.key ?? null, cx); break;
      default: break;
    }
  }

  move(delta, cx) {
    const key = moveSelection(this.rows, this.selected, delta);
    if (key && key !== this.selected) this.select(key, cx);
  }

  // this host → all → each other host, over the hosts the header names.
  cycleHost(cx) {
    const hosts = this.header?.hosts ?? [];
    const next = (hosts.length ? hostChoices(hosts, this.cfg?.host_id) : [this.cfg?.host_id ?? null]);
    const at = next.findIndex((h) => (h ?? null) === (this.filter.host ?? null));
    this.filter.host = at === next.length - 1 ? null : next[at + 1] ?? null;
    this.change = -1;
    this.poll(cx);
  }

  // Every action goes through here: actions.js decides, this carries it out.
  async act(plan, cx) {
    if (!plan) return;
    const made = {};
    for (const step of plan.steps ?? []) {
      if (step.fetch) {
        const json = await this.get(step.fetch.path);
        if (step.fetch.into) made[step.fetch.into] = json;
        continue;
      }
      if (step.label_new) {
        await this.labelNew(made, plan.recipe ?? made.recipe);
        continue;
      }
      if (step.command === "hc") {
        await hc(step.args ?? []);
        continue;
      }
      const args = step.from ? { ...spaceArgs(made[step.from], this.cfg), ...(step.machine === undefined
        ? {} : { machine: step.machine }) } : step.args ?? {};
      const out = run(step.command, args);
      if (step.as && out && typeof out === "object") made[step.as] = out;
    }
    for (const extra of plan.extra ?? []) {
      if (extra.fetch) await this.send(extra.fetch.path, extra.fetch.body).catch(() => null);
    }
    if (plan.notify) notify(plan.notify);
    cx.notify();
  }

  // The terminal space.new just made gets the session's key, so the next board read finds it by label.
  async labelNew(made, recipe) {
    const key = recipe?.key;
    if (!key) return;
    const space = made?.made?.space;
    for (let i = 0; i < 10; i++) {
      const t = newestInSpace(terminals(), space);
      if (t?.address) {
        await hc(["label", String(t.address), `ctx.key=${key}`]);
        return;
      }
      await new Promise((done) => setTimeout(done, 150));   // core has not taken it on yet
    }
    notify({ title: "ctx", body: `started: couldn't label the terminal for ${key}`, level: "warning" });
  }

  open(key, cx) {
    const row = this.row(key);
    if (!row || !this.cfg) return;
    cx.spawn(async (cx) => {
      try {
        await this.act(openPlan(row, terminals(), this.cfg), cx);
      } catch (e) {
        this.error = String(e?.message ?? e);
        cx.notify();
      }
    });
  }

  // `n` / `s`: host, then what to run there, then a folder. The choices are the hosts' own published words.
  start(cx) {
    cx.spawn(async (cx) => {
      try {
        if (!this.cfg) return;
        const hosts = await this.get("/v1/hosts");
        this.hosts = hosts ?? [];
        const list = hostChoices(this.hosts, this.cfg.host_id).map((host) => ({
          label: host === this.cfg.host_id ? `${host} (this computer)` : host,
          value: { step: "harness", host },
        }));
        this.pick("start on which host?", list);
      } catch (e) {
        this.error = String(e?.message ?? e);
        cx.notify();
      }
    });
  }

  pick(title, list) {
    openOverlay({
      mod: "ctx-board", kind: "popup", ...PICKER,
      props: { picker: { title, list, picked: this.picked } },
    });
  }

  // The picker is this same mod with props: each answer either opens the next step or acts.
  picked(next, cx) {
    const p = next ?? {};
    if (p.step === "harness") {
      const info = this.hosts.find((h) => h?.host_id === p.host)?.info ?? {};
      const list = harnessChoices(info).map((c) => ({ label: c.label, value: { step: "folder", host: p.host, ...c } }));
      if (!list.length) {
        notify({ title: "ctx", body: `${p.host} says it has nothing to start`, level: "warning" });
        return;
      }
      this.pick(`start what on ${p.host}?`, list);
      cx?.notify();
      return;
    }
    if (p.step === "folder") {
      this.ask(`folder for ${p.harness}${p.profile ? ` · ${p.profile}` : ""} on ${p.host}?`, "~",
        (cwd, cx2) => this.launch({ host: p.host, harness: p.harness, profile: p.profile }, cwd, cx2));
      cx?.notify();
      return;
    }
    if (p.launch) { this.launch(p.launch.host, p.launch.cwd, cx); return; }
    if (p.retire) { this.retireRow(p.retire.key, p.retire.instruction, cx); return; }
    if (p.end) { this.endNow(p.end, cx); return; }
    if (p.send) { this.post(p.send.path, p.send.body, cx); return; }
    closeOverlay(this.overlay);
  }

  async launch(choice, cwd, cx) {
    closeOverlay(this.overlay);
    try {
      const recipe = await this.get(launchPath(choice, cwd));
      await this.act(startPlan(recipe, this.cfg), cx);
    } catch (e) {
      this.error = String(e?.message ?? e);
      notify({ title: "ctx", body: String(e?.message ?? e), level: "error" });
      cx?.notify();
    }
  }

  // `m` / `a`: one line to send. The refusal is the hub's own reason, said before anything is posted.
  compose(mode, key, cx) {
    const row = this.row(key);
    if (!row) return;
    const ask = messageRequest(row, " ", this.cfg, mode);
    if (ask.refuse) {
      notify({ title: mode === "a" ? "answer" : "message", body: ask.refuse, level: "warning" });
      return;
    }
    const hint = mode === "a" ? row.reply_target?.text ?? "" : row.title ?? "";
    this.ask(mode === "a" ? `answer · ${hint}` : `message · ${row.title ?? ""}`, "", (body, cx2) => {
      const req = messageRequest(row, body, this.cfg, mode);
      if (req.refuse) { notify({ title: "ctx", body: req.refuse, level: "warning" }); return; }
      this.post(req.path, req.body, cx2);
    });
  }

  post(path, body, cx) {
    closeOverlay(this.overlay);
    cx.spawn(async (cx) => {
      try {
        await this.send(path, body);
        notify({ title: "ctx", body: "sent", level: "success" });
      } catch (e) {
        notify({ title: "ctx", body: String(e?.message ?? e), level: "error" });
      }
      if (this.selected) this.select(this.selected, cx);
      else this.poll(cx);
    });
  }

  // `i`: the session's standing instructions, oldest first; a click retires that one.
  retire(key, cx) {
    if (!key) return;
    cx.spawn(async (cx) => {
      const instructions = this.detail?.instructions ?? [];
      if (!instructions.length) {
        notify({ title: "ctx", body: "no standing instructions", level: "info" });
        return;
      }
      this.pick("retire which instruction?", instructions.map((r) => ({
        label: `${r.n} • ${r.text ?? ""}`,
        value: { retire: { key, instruction: r } },
      })));
      cx.notify();
    });
  }

  retireRow(key, instruction, cx) {
    closeOverlay(this.overlay);
    const req = retireRequest(this.row(key) ?? { key }, instruction);
    if (req.refuse) { notify({ title: "ctx", body: req.refuse, level: "warning" }); return; }
    this.post(req.path, req.body, cx);
  }

  // `k`: a yes/no over the row, then terminal.kill — or where it actually runs.
  end(key, cx) {
    const row = this.row(key);
    if (!row) return;
    this.pick(`end ${row.title || row.key} on ${row.host_id || "?"}?  (y = yes, n = Esc)`,
      [{ label: "yes — kill its terminal", value: { end: key } }, { label: "no", value: {} }]);
    cx?.notify();
  }

  endNow(key, cx) {
    closeOverlay(this.overlay);
    const row = this.row(key);
    if (!row) return;
    const plan = endPlan(row, terminals(), this.cfg);
    for (const step of plan.steps ?? []) run(step.command, step.args ?? {});
    if (plan.notify) notify(plan.notify);
    cx?.notify();
  }

  // One text field over the popup: Enter sends whatever the callback makes of it, Esc closes.
  ask(title, value, onText) {
    this.text = { title, value: value ?? "", onText };
    openOverlay({
      mod: "ctx-board", kind: "popup", ...PICKER,
      props: { picker: { text: this.text }, ask: true },
    });
  }

  // A button on the text overlay: the same value under another name, so Enter and Send agree.
  submit(cx) {
    const t = this.text;
    closeOverlay(this.overlay);
    if (t?.value?.trim()) t.onText(t.value, cx);
    else closeOverlay(this.overlay);
  }

  // Rows: a click selects, Enter (or a double click) opens. Digits and up/down come through keys.json.
  onKey(e, cx) {
    const ch = e?.key_char ?? e?.key ?? "";
    if (e?.key === "enter") { this.open(this.selected, cx); return; }
    if (e?.click_count === 2) { this.open(this.selected, cx); return; }
    const p = this.parts?.[Number(ch) - 1];
    if (/^[1-9]$/.test(ch) && p) this.select(p.key, cx);
  }

  text2(t, part) {
    return div().text_color(t[toneColor(part.tone)] ?? t.muted).child(part.text ?? "");
  }

  headerView(t) {
    const lines = headerLines(this.header, { host: this.filter.host ?? this.cfg?.host_id });
    const flag = `${this.filter.host === null ? "all hosts" : ""}${this.filter.ended ? " · + ended" : ""}`;
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
      .on_click((_e, cx) => this.select(p.key, cx))
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
  pickerOverlay(t) {
    const p = props()?.picker ?? {};
    if (p.text) return this.textOverlay(t, p.text);
    return v_flex().id("ctx-pick").w_full().h_full().min_h(0).p(12).gap(4).bg(t.popover).text_size(12.5)
      .child(h_flex().pb(4).items_center()
        .child(div().flex_1().min_w(0).truncate().font_semibold().child(String(p.title ?? "ctx")))
        .child(div().text_size(10.5).text_color(t.dim).child("Esc")))
      .child(v_flex().id("ctx-pick-list").flex_1().min_h(0).gap(1).overflow_y_scrollbar()
        .children((p.list ?? []).map((c, i) => h_flex().id(`ctx-pick-${i}`).h(28).px(8).rounded(6).gap(8).items_center()
          .hover((s) => s.bg(t.hover))
          .on_click((_e, cx) => this.picked(c.value, cx))
          .child(div().w(14).flex_none().text_size(10.5).text_color(t.dim).child(i < 9 ? String(i + 1) : ""))
          .child(div().flex_1().min_w(0).truncate().child(c.label ?? "")))));
  }

  textOverlay(t, draft) {
    if (this.input === undefined) this.input = null;
    if (!this.input || this.inputDraft !== draft) {
      this.inputDraft = draft;
      this.input = InputState.new({ value: draft.value ?? "" });
      this.input.on("change", () => { draft.value = this.input.value(); });
      this.input.on("submit", () => this.submit(this.cxRef));
      this.input.focus();
    }
    return v_flex().id("ctx-ask").w_full().min_h(0).p(14).gap(8).bg(t.popover).text_size(12.5)
      .child(h_flex().items_center()
        .child(div().flex_1().min_w(0).truncate().font_semibold().child(String(draft.title ?? "ctx")))
        .child(div().text_size(10.5).text_color(t.dim).child("Esc")))
      .child(Input.new(this.input).h(34).px(10).rounded(8).border_1().border_color(t.accent).bg(t.background)
        .text_size(12.5).text_color(t.foreground))
      .child(h_flex().gap(8).justify_end().pt(4)
        .child(h_flex().id("ctx-ask-cancel").h(28).px(12).rounded(7).items_center().border_1().border_color(t.input)
          .bg(t.raised).text_color(t.foreground).on_click(() => closeOverlay(this.overlay)).child("Cancel"))
        .child(h_flex().id("ctx-ask-go").h(28).px(12).rounded(7).items_center().border_1().border_color(t.solid)
          .bg(t.solid).text_color(t.solid_foreground).on_click((_e, cx) => this.submit(cx)).child("Send")));
  }

  render(_cx) {
    const t = theme();
    if (this.picker) return this.pickerOverlay(t);
    const root = v_flex().id("ctx-root").size_full().min_h(0).bg(t.chrome).border_r(1).border_color(t.border)
      .track_focus(this.handle).on_key_down((e, cx) => { this.cxRef = cx; this.onKey(e, cx); })
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
