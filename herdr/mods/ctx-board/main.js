// The ctx board as a herdr-app mod: the hub's view model, painted. Render-only for now —
// every string comes from /v1/board and /v1/board/{key}, and the only thing you can do to a row
// is select it. act() is where open/start/message land later.

import { View, div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { appHome, readTextFile, theme, nextCommand } from "herdr";
import { rowParts, headerLines, detailSections, detailTitle, toneColor, hasBold, tzOffset } from "./view.js";

const POLL_MS = 2000;

export default class CtxBoard extends View {
  init(_props, cx) {
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
    this.handle = cx.focus_handle();
    // mod.json says focusable: pane.go can step onto this copy; it takes the keyboard on "focus".
    cx.spawn(async (cx) => {
      for (;;) {
        if ((await nextCommand()) === "focus") this.handle.focus();
        cx.notify();
      }
    });
    cx.timer.every(POLL_MS, (cx) => this.poll(cx));
    this.poll(cx);
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
    const r = await fetch(`${String(this.cfg.hub_url).replace(/\/+$/, "")}${path}`, {
      headers: { Authorization: `Bearer ${this.cfg.token ?? ""}` },
    });
    if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
    return r.json();
  }

  // A fetch every 2 s, but a redraw only when something actually changed — plus one every 30 s
  // so the ages on the rows don't go stale.
  async poll(cx) {
    try {
      const cfg = await this.config();
      if (cfg) {
        const host = encodeURIComponent(cfg.host_id ?? "");
        const tz = encodeURIComponent(tzOffset());
        const health = await this.get("/v1/health");
        if (health.change !== this.change) {
          this.change = health.change;
          const board = await this.get(`/v1/board?host=${host}&screen_host=${host}&tz=${tz}`);
          this.rows = board.rows ?? [];
          this.header = board.header ?? null;
          if (this.selected) this.detail = await this.get(`/v1/board/${encodeURIComponent(this.selected)}?tz=${tz}`);
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

  // Every action goes through here. Only "select" does anything yet; open / start / message /
  // answer will join it, and the row already carries what they need (reply_target, can_message,
  // host_id, harness, tmux_at) — so the hub stays the one deciding, and this stays the one routing.
  act(name, p, cx) {
    if (name !== "select") {
      console.log(`ctx-board: action "${name}" on ${p.key}`);
      return;
    }
    this.select(p, cx);
  }

  select(p, cx) {
    const key = p.key;
    this.selected = key;
    this.detail = null;
    cx.spawn(async (cx) => {
      try {
        this.detail = await this.get(`/v1/board/${encodeURIComponent(key)}?tz=${encodeURIComponent(tzOffset())}`);
      } catch (e) {
        this.error = String(e?.message ?? e);
      }
      cx.notify();
    });
  }

  // Digits pick a top-level row (the same selection a click makes); anything else is left alone.
  onKey(e, cx) {
    const ch = e?.key_char ?? e?.key ?? "";
    const p = this.parts?.[Number(ch) - 1];
    if (/^[1-9]$/.test(ch) && p) this.act("select", p, cx);
  }

  text(t, part) {
    return div().text_color(t[toneColor(part.tone)] ?? t.muted).child(part.text ?? "");
  }

  headerView(t) {
    const lines = headerLines(this.header, { host: this.cfg?.host_id });
    return v_flex().flex_none().px(10).pt(8).pb(6).gap(2).text_size(11.5)
      .child(this.text(t, lines[0]))
      .child(this.text(t, lines[1]));
  }

  rowView(t, p, depth) {
    const on = this.selected === p.key;
    const row = h_flex().id(`ctx-row-${p.key}`).pl(8 + depth * 14).pr(8).h(24).flex_none().gap(6).items_center()
      .text_size(12.5).rounded(6).hover((s) => s.bg(t.hover))
      .when(on, (el) => el.bg(t.selected))
      .on_click((_e, cx) => this.act("select", p, cx))
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
        .children(s.lines.map((l) => this.text(t, l)))));
  }

  render(_cx) {
    const t = theme();
    const root = v_flex().id("ctx-root").size_full().min_h(0).bg(t.chrome).border_r(1).border_color(t.border)
      .track_focus(this.handle).on_key_down((e, cx) => this.onKey(e, cx))
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
