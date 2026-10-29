// ctxswitch pi extension: live status + "waiting on you" for the ctx collector. Installed by `ctx install`
// as a symlink in ~/.pi/agent/extensions/.
//
// Registry: <runtime>/ctxswitch/pi-<pid>.json = {pid, procStart, sessionId, sessionFile, cwd, status, statusSince, socket},
//   status "idle" | "busy", rewritten on agent_start / agent_settled, removed on session_shutdown. The collector
//   polls it (a crashed pi leaves a file whose pid is gone or reused: procStart tells them apart).
// Spool: <spool>/<time_ns>-<pid>.json, one per event, the same record ctx-hook writes for Claude:
//   {harness:"pi", session_id, event, ts, data}. Only blocking UI prompts and session start/end are spooled;
//   everything else is in the transcript.
// Briefing: on a resumed session (/resume, or pi started into a session that has entries), `ctx-hook brief pi <id>` (path from <state>/install.json, written by
//   `ctx install`) asks the hub what you need back in your head; shown with ui.notify. Async, 4 s cap.
// Messages: <runtime>/ctxswitch/pi-<pid>.sock takes the collector's newline-delimited JSON line
//   {"type":"user","message":{"content":..},"msg_id":..,"session_id":..} (the Claude inbox format), hands the text
//   to pi.sendUserMessage (a follow-up while pi is busy, a new prompt when idle) and answers {"ok":true} or
//   {"ok":false,"error":..}. A line for another session id is refused.
// Nothing here may throw into pi: every write is best-effort.
import { execFile } from "node:child_process";
import { createServer, type Server } from "node:net";
import { chmodSync, mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync, existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const STATE = join(homedir(), ".local", "state", "ctxswitch");

function runtimeDir(): string {
	if (process.env.CTXSWITCH_RUNTIME) return process.env.CTXSWITCH_RUNTIME;
	if (process.env.XDG_RUNTIME_DIR) return process.env.XDG_RUNTIME_DIR;
	const run = `/run/user/${process.getuid?.() ?? 1000}`;
	return existsSync(run) ? run : join(STATE, "run");
}

const REGISTRY = join(runtimeDir(), "ctxswitch");
const SPOOL = process.env.CTXSWITCH_SPOOL || join(STATE, "spool");
const FILE = join(REGISTRY, `pi-${process.pid}.json`);
const SOCK = join(REGISTRY, `pi-${process.pid}.sock`);
const MAX_LINE = 256 * 1024;

function procStart(): string | undefined {
	try {
		const stat = readFileSync("/proc/self/stat", "utf8");
		return stat.slice(stat.lastIndexOf(")") + 1).trim().split(/\s+/)[19];
	} catch {
		return undefined; // no /proc (macOS): the collector falls back to "pid exists"
	}
}

function hookExe(): string {
	try {
		const hook = JSON.parse(readFileSync(join(STATE, "install.json"), "utf8"))?.pi?.hook;
		if (typeof hook === "string" && hook) return hook;
	} catch { /* not installed by ctx install */ }
	return "ctx-hook";
}

function atomicWrite(dir: string, name: string, body: unknown): void {
	mkdirSync(dir, { recursive: true });
	const tmp = join(dir, `.${name}`);
	writeFileSync(tmp, JSON.stringify(body));
	renameSync(tmp, join(dir, name));
}

export default function (pi: ExtensionAPI) {
	const start = procStart();
	let session: { id: string; file?: string; cwd: string } | undefined;
	let status: "idle" | "busy" = "idle";
	let since = new Date().toISOString();
	let seq = 0;
	let server: Server | undefined;
	let sockPath: string | undefined;

	const register = () => {
		if (!session) return;
		try {
			atomicWrite(REGISTRY, `pi-${process.pid}.json`, {
				pid: process.pid, procStart: start, sessionId: session.id, sessionFile: session.file,
				cwd: session.cwd, status, statusSince: since, socket: sockPath,
			});
		} catch { /* best-effort */ }
	};
	const spool = (event: string, data: Record<string, unknown> = {}) => {
		if (!session) return;
		try {
			// ns since the epoch, like ctx-hook's names, so the collector's name sort is time order
			const ns = BigInt(Date.now()) * 1_000_000n + BigInt(seq++ % 1_000_000);
			atomicWrite(SPOOL, `${ns}-${process.pid}.json`, {
				harness: "pi", session_id: session.id, event, ts: new Date().toISOString(), data,
			});
		} catch { /* best-effort */ }
	};
	const handle = (raw: string): { ok: boolean; error?: string } => {
		let m: any;
		try { m = JSON.parse(raw); } catch { return { ok: false, error: "not json" }; }
		if (m?.type !== "user" || typeof m?.message?.content !== "string" || !m.message.content.trim()) return { ok: false, error: "not a user message" };
		if (!session) return { ok: false, error: "no session" };
		if (m.session_id && m.session_id !== session.id) return { ok: false, error: "wrong session" };
		pi.sendUserMessage(m.message.content, { deliverAs: "followUp" });
		return { ok: true };
	};
	const listen = () => {
		if (server) return;
		try {
			mkdirSync(REGISTRY, { recursive: true, mode: 0o700 });
			try { unlinkSync(SOCK); } catch { /* none left over */ }
			server = createServer((conn) => {
				let buf = "";
				conn.setEncoding("utf8");
				conn.setTimeout(30_000, () => conn.destroy());
				conn.on("error", () => { /* peer went away */ });
				conn.on("data", (chunk: string) => {
					buf += chunk;
					if (buf.length > MAX_LINE) { conn.end(JSON.stringify({ ok: false, error: "too long" }) + "\n"); return; }
					const nl = buf.indexOf("\n");
					if (nl < 0) return;
					let reply;
					try { reply = handle(buf.slice(0, nl)); } catch (e) { reply = { ok: false, error: String(e) }; }
					conn.end(JSON.stringify(reply) + "\n");
				});
			});
			server.on("error", () => { server = undefined; sockPath = undefined; register(); });
			server.listen(SOCK, () => {
				try { chmodSync(SOCK, 0o600); } catch { /* best-effort */ }
				sockPath = SOCK; register();
			});
		} catch { server = undefined; }
	};
	const unlisten = () => {
		try { server?.close(); } catch { /* closing */ }
		server = undefined; sockPath = undefined;
		try { unlinkSync(SOCK); } catch { /* already gone */ }
	};
	const setStatus = (s: "idle" | "busy") => {
		if (s === status) return;
		status = s; since = new Date().toISOString(); register();
	};

	pi.on("session_start", async (ev, ctx: ExtensionContext) => {
		session = { id: ctx.sessionManager.getSessionId(), file: ctx.sessionManager.getSessionFile(), cwd: ctx.cwd };
		status = ctx.isIdle() ? "idle" : "busy"; since = new Date().toISOString();
		register();
		listen();
		spool("session_start", { reason: ev.reason, cwd: ctx.cwd, session_file: session.file });
		// resume = /resume inside pi; `pi -c` / `pi --session X` start with "startup" into an existing session
		const resumed = ev.reason === "resume" || (ev.reason === "startup" && ctx.sessionManager.getLeafEntry() !== undefined);
		if (resumed) {
			try {
				execFile(hookExe(), ["brief", "pi", session.id], { timeout: 4000 }, (err, out) => {
					const text = String(out ?? "").trim();
					if (!err && text) try { ctx.ui.notify(text, "info"); } catch { /* no UI */ }
				});
			} catch { /* best-effort */ }
		}
	});
	pi.on("agent_start", async () => setStatus("busy"));
	pi.on("agent_settled", async () => setStatus("idle"));
	pi.on("ui_prompt_start", async (ev) => spool("ui_prompt_start", { kind: ev.kind, title: ev.title }));
	pi.on("ui_prompt_end", async (ev) => spool("ui_prompt_end", { kind: ev.kind, title: ev.title }));
	pi.on("session_shutdown", async (ev) => {
		spool("session_shutdown", { reason: ev.reason });
		try { unlinkSync(FILE); } catch { /* already gone */ }
		unlisten();
		session = undefined;
	});
}
