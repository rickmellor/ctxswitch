"""ctx install / uninstall / doctor: wire this host's Claude Code, pi and collector to ctxswitch, and check it.

  install    Claude hooks merged into settings.json (ctx-hook, async; other hooks untouched) plus one synchronous
             SessionStart(resume) hook for the resume briefing, the pi extension symlinked into pi's extensions dir
             (it finds ctx-hook through <state>/install.json), a config stub, the collector's systemd --user unit (re)started.
             Idempotent. The first install backs settings.json up; state is kept in <state>/install.json.
  uninstall  settings.json restored byte-for-byte when nothing else changed it since, else our hooks removed
             surgically; the extension link and (with --collector) the unit removed.
  doctor     one line per check (✓ ok, ! warning, ✗ broken, with the fix); exit 1 on any ✗.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sysconfig
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ctxswitch import hook
from ctxswitch.common.config import Config, default_path, load

VALIDATED = {"claude": "2.1.280", "pi": "1.2.0"}           # versions whose hook/extension payloads were checked
MARK = "ctx-hook"                                           # identifies our hook entries in settings.json
UNIT = "ctx-collector.service"
EXT_NAME = "ctxswitch.ts"


# -- paths ------------------------------------------------------------------------------------------------------
def state_dir() -> Path:
    return Path.home() / ".local" / "state" / "ctxswitch"


def claude_settings() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"


def pi_ext_dir() -> Path:
    return Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent") / "extensions"


def unit_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / UNIT


def scripts_dir() -> Path:
    return Path(sysconfig.get_path("scripts"))


def ext_source() -> Path:
    return Path(hook.__file__).parent / "pi_ext" / EXT_NAME


def hook_exe() -> Path:
    return scripts_dir() / "ctx-hook"


def hook_command() -> str:
    return f"{hook_exe()} claude"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _own(p: Path) -> None:
    """Running as root for another user's HOME: hand what we create to that user."""
    try:
        st = Path.home().stat()
        if os.geteuid() == 0 and st.st_uid != 0:
            os.chown(p, st.st_uid, st.st_gid, follow_symlinks=False)
    except OSError:
        pass


def _mkdir(p: Path) -> None:
    missing = [d for d in (p, *p.parents) if not d.exists()]
    p.mkdir(parents=True, exist_ok=True)
    for d in missing:
        _own(d)


def _write(p: Path, data: bytes, mode: int = 0o644) -> None:
    """Atomic write keeping the existing file's mode and owner."""
    _mkdir(p.parent)
    st = p.stat() if p.exists() else None
    tmp = p.with_name(f".{p.name}.ctx-tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, st.st_mode & 0o7777 if st else mode)
    if st:
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except OSError:
            pass
    else:
        _own(tmp)
    os.replace(tmp, p)


class State:
    def __init__(self, path: Path | None = None):
        self.path = path or state_dir() / "install.json"
        self.d: dict[str, Any] = json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self) -> None:
        _write(self.path, (json.dumps(self.d, indent=2) + "\n").encode())


# -- Claude settings.json ---------------------------------------------------------------------------------------
def _ours(group: Any) -> bool:
    return isinstance(group, dict) and any(MARK in str(h.get("command", "")) for h in group.get("hooks", []) if isinstance(h, dict))


def brief_group(command: str) -> dict[str, Any]:
    """The resume briefing: synchronous (an async hook's output is dropped), resume only, short timeout."""
    exe = command.rsplit(" ", 1)[0]
    return {"matcher": "resume", "hooks": [{"type": "command", "command": f"{exe} brief claude", "timeout": 5}]}


def merge_hooks(settings: dict[str, Any], command: str) -> dict[str, Any]:
    """settings with exactly one ctx-hook group per event in hook.EVENTS (plus the briefing on SessionStart);
    everything else as it was."""
    out = json.loads(json.dumps(settings))
    hooks = out.setdefault("hooks", {})
    for ev in hook.EVENTS:
        groups = [g for g in hooks.get(ev, []) if not _ours(g)]
        groups.append({"hooks": [{"type": "command", "command": command, "async": True, "timeout": 10}]})
        if ev == "SessionStart":
            groups.append(brief_group(command))
        hooks[ev] = groups
    return out


def remove_hooks(settings: dict[str, Any], had_hooks: bool = True) -> dict[str, Any]:
    out = json.loads(json.dumps(settings))
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for ev in list(hooks):
        if isinstance(hooks[ev], list):
            hooks[ev] = [g for g in hooks[ev] if not _ours(g)]
            if not hooks[ev]:
                del hooks[ev]
    if not hooks and not had_hooks:
        del out["hooks"]
    return out


def _dump(settings: dict[str, Any]) -> bytes:
    return (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode()


def install_claude(st: State, dry: bool, log) -> None:
    p = claude_settings()
    raw = p.read_bytes() if p.exists() else None
    cur = json.loads(raw) if raw and raw.strip() else {}
    new = merge_hooks(cur, hook_command())
    if new == cur:
        log(f"claude: hooks already in {p}"); return
    if dry:
        log(f"claude: would add ctx-hook for {', '.join(hook.EVENTS)} to {p}"); return
    rec = st.d.get("claude")
    if not rec or rec.get("settings") != str(p):            # first install: keep the original
        backup = None
        if raw is not None:
            backup = state_dir() / "backup" / f"settings.json.{time.strftime('%Y%m%d-%H%M%S')}"
            _mkdir(backup.parent); backup.write_bytes(raw); _own(backup)
        rec = {"settings": str(p), "backup": str(backup) if backup else None, "had_hooks": "hooks" in cur}
    data = _dump(new)
    _write(p, data, mode=0o600)
    rec["written_sha"] = _sha(data)
    st.d["claude"] = rec; st.save()
    log(f"claude: hooks added to {p}" + (f" (backup {rec['backup']})" if rec.get("backup") else ""))


def uninstall_claude(st: State, dry: bool, log) -> None:
    rec = st.d.get("claude") or {}
    p = Path(rec.get("settings") or claude_settings())
    if not p.exists():
        log(f"claude: {p} missing, nothing to do"); return
    raw = p.read_bytes()
    if rec.get("written_sha") == _sha(raw):                 # untouched since we wrote it: put the original back
        if dry:
            log(f"claude: would restore {p} from {rec.get('backup') or '(no original: delete)'}"); return
        if rec.get("backup") and Path(rec["backup"]).exists():
            _write(p, Path(rec["backup"]).read_bytes())
            log(f"claude: restored {p} byte-for-byte from {rec['backup']}")
        elif not rec.get("backup"):
            p.unlink(); log(f"claude: removed {p} (it didn't exist before install)")
        else:
            log(f"claude: backup {rec['backup']} is gone; removing our hooks instead")
            _write(p, _dump(remove_hooks(json.loads(raw), rec.get("had_hooks", True))))
    else:
        cur = json.loads(raw) if raw.strip() else {}
        new = remove_hooks(cur, rec.get("had_hooks", True))
        if new == cur:
            log(f"claude: no ctx-hook entries in {p}")
        elif dry:
            log(f"claude: would remove ctx-hook entries from {p} (it changed since install, so no restore)")
        else:
            _write(p, _dump(new)); log(f"claude: removed ctx-hook entries from {p} (other changes kept)")
    if not dry:
        st.d.pop("claude", None); st.save()


# -- pi extension -----------------------------------------------------------------------------------------------
def install_pi(st: State, dry: bool, force: bool, log) -> None:
    link, src = pi_ext_dir() / EXT_NAME, ext_source()
    if link.is_symlink() and link.resolve() == src.resolve():
        if not dry and (st.d.get("pi") or {}).get("hook") != str(hook_exe()):
            st.d["pi"] = {"link": str(link), "hook": str(hook_exe())}; st.save()
        log(f"pi: extension already linked at {link}"); return
    if (link.exists() or link.is_symlink()) and not link.is_symlink() and not force:
        log(f"pi: {link} exists and is not our link; left alone (--force replaces it)"); return
    if dry:
        log(f"pi: would link {link} -> {src}"); return
    _mkdir(link.parent)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(src); _own(link)
    st.d["pi"] = {"link": str(link), "hook": str(hook_exe())}; st.save()
    log(f"pi: linked {link} -> {src} (takes effect in new pi sessions)")


def uninstall_pi(st: State, dry: bool, log) -> None:
    link = Path((st.d.get("pi") or {}).get("link") or pi_ext_dir() / EXT_NAME)
    if link.is_symlink():
        if dry:
            log(f"pi: would remove {link}"); return
        link.unlink(); log(f"pi: removed {link}")
    else:
        log(f"pi: no extension link at {link}")
    if not dry:
        st.d.pop("pi", None); st.save()


# -- collector --------------------------------------------------------------------------------------------------
UNIT_TEMPLATE = """\
# written by `ctx install`; `ctx uninstall --collector` removes it
[Unit]
Description=ctxswitch collector (transcript watcher -> hub)
After=network-online.target

[Service]
Type=simple
ExecStart={exe}
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
StandardOutput=append:%h/.local/state/ctxswitch/collector.log
StandardError=append:%h/.local/state/ctxswitch/collector.log

[Install]
WantedBy=default.target
"""


def _systemctl(*args: str) -> subprocess.CompletedProcess | None:
    if not shutil.which("systemctl"):
        return None
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None


def write_config(hub: str | None, token: str | None, dry: bool, log) -> None:
    import tomllib
    import tomli_w
    p = default_path()
    raw = tomllib.loads(p.read_text()) if p.exists() else {}
    new = json.loads(json.dumps(raw))
    new.setdefault("collector", {}).setdefault("host_id", socket.gethostname().split(".")[0])
    if hub:
        new.setdefault("hub", {})["url"] = hub
    if token:
        new.setdefault("hub", {})["token"] = token
    if "claude_win_roots" not in new["collector"] and (win := windows_claude_root()):
        new["collector"]["claude_win_roots"] = [str(win)]      # WSL: also read Windows-native Claude (read-only)
    if new == raw:
        log(f"config: {p} unchanged"); return
    if dry:
        log(f"config: would write {p}"); return
    _write(p, tomli_w.dumps(new).encode(), mode=0o600)       # holds the hub token
    log(f"config: wrote {p}" + ("" if new.get("hub", {}).get("url") else " (set [hub] url and token)"))


def windows_claude_root() -> Path | None:
    """<windows home>/.claude/projects when this is WSL and Windows-native Claude has been used."""
    from ctxswitch.common.wsl import windows_home
    home = windows_home()
    p = home / ".claude" / "projects" if home else None
    return p if p and p.is_dir() else None


def install_collector(dry: bool, start: bool, log) -> None:
    for d in (state_dir(), hook.spool_dir()):
        if not dry:
            _mkdir(d)
    p, body = unit_path(), UNIT_TEMPLATE.format(exe=scripts_dir() / "ctx-collector")
    changed = not p.exists() or p.read_text() != body
    if dry:
        log(f"collector: would {'write' if changed else 'keep'} {p}" + (" and restart it" if start else "")); return
    if changed:
        _write(p, body.encode()); log(f"collector: wrote {p}")
    if not start:
        return
    cmds = [("daemon-reload",), ("enable", UNIT), ("restart", UNIT)]
    for c in cmds:
        r = _systemctl(*c)
        if r is None or r.returncode != 0:
            log(f"collector: `systemctl --user {' '.join(c)}` failed ({(r.stderr.strip() if r else 'no systemd --user')}); "
                f"run: systemctl --user daemon-reload && systemctl --user enable --now {UNIT}")
            return
    log("collector: enabled and restarted")
    lg = subprocess.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger", "--value"],
                        capture_output=True, text=True) if shutil.which("loginctl") else None
    if lg and lg.stdout.strip() == "no":
        log("collector: linger is off; `loginctl enable-linger $USER` keeps it running after logout")


def uninstall_collector(dry: bool, log) -> None:
    p = unit_path()
    if not p.exists():
        log("collector: no unit installed"); return
    if dry:
        log(f"collector: would stop, disable and remove {p}"); return
    _systemctl("disable", "--now", UNIT)
    p.unlink(); _systemctl("daemon-reload")
    log(f"collector: stopped and removed {p} (queue and config kept)")


# -- doctor -----------------------------------------------------------------------------------------------------
@dataclass
class Check:
    status: str                    # "✓" | "!" | "✗"
    name: str
    detail: str
    fix: str = ""


def _version(cmd: str) -> str | None:
    try:
        r = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"\d+\.\d+\.\d+", r.stdout + r.stderr)
    return m.group(0) if m else None


def _age(t: float | None) -> str:
    if t is None:
        return "never"
    s = max(0, time.time() - t)
    return f"{s:.0f}s ago" if s < 120 else f"{s / 60:.0f}m ago" if s < 7200 else f"{s / 3600:.0f}h ago"


def doctor(cfg: Config | None = None) -> list[Check]:
    import httpx
    cfg = cfg or load()
    out: list[Check] = []
    cp = default_path()
    out.append(Check("✓", "config", str(cp)) if cp.exists() else
               Check("!", "config", f"{cp} missing; using defaults (hub {cfg.hub.url})", "ctx install --hub URL --token T"))
    health = None
    try:
        h = httpx.get(cfg.hub.url.rstrip("/") + "/v1/health", timeout=5,
                      headers={"Authorization": f"Bearer {cfg.hub.token}"} if cfg.hub.token else {})
        health = h.json()
    except (httpx.HTTPError, ValueError) as e:
        out.append(Check("✗", "hub", f"{cfg.hub.url} unreachable ({e.__class__.__name__})", "check [hub] url and that the hub is up"))
    if health is not None:
        if health.get("auth") is False:
            out.append(Check("✗", "hub", f"{cfg.hub.url} rejects this token", "set [hub] token to the hub's CTXSWITCH_HUB_TOKEN"))
        else:
            out.append(Check("✓", "hub", f"{cfg.hub.url} ({health.get('hosts')} hosts)"))
            try:
                hosts = {x["host_id"]: x["last_seen"] for x in httpx.get(
                    cfg.hub.url.rstrip("/") + "/v1/hosts", timeout=5,
                    headers={"Authorization": f"Bearer {cfg.hub.token}"} if cfg.hub.token else {}).json()}
                seen = hosts.get(cfg.host_id)
                if seen is None:
                    out.append(Check("✗", "this host", f"hub has never heard from {cfg.host_id!r}", "start the collector"))
                elif time.time() - seen > 150:
                    out.append(Check("✗", "this host", f"{cfg.host_id} last seen {_age(seen)}", "systemctl --user status ctx-collector"))
                else:
                    out.append(Check("✓", "this host", f"{cfg.host_id} last seen {_age(seen)}"))
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                out.append(Check("!", "this host", "couldn't read /v1/hosts"))
            rs = health.get("recap")
            if rs is None:
                out.append(Check("!", "recaps", "off on the hub (no seat, or --no-recap)"))
            elif rs.get("error_at") and (rs.get("last_ok") or 0) < rs["error_at"]:
                out.append(Check("!", "recaps", f"{rs.get('seat')}: {rs.get('last_error')} ({_age(rs['error_at'])})",
                                 "check the seat; the board works without recaps"))
            else:
                out.append(Check("✓", "recaps", f"{rs.get('seat')} last ok {_age(rs.get('last_ok'))}"))
            ls = health.get("ledger")
            if rs is not None and ls is None:
                out.append(Check("!", "ledger", "off on the hub ([hub] ledger = false)"))
            elif ls and ls.get("error_at") and (ls.get("last_ok") or 0) < ls["error_at"]:
                out.append(Check("!", "ledger", f"{ls.get('last_error')} ({_age(ls['error_at'])})", "check the seat; recaps and asks still work"))
            elif ls:
                out.append(Check("✓", "ledger", f"{ls.get('extracted', 0)} exchanges read, {ls.get('backlog', 0)} sessions behind"))
    r = _systemctl("is-active", UNIT)
    if r is None:
        out.append(Check("!", "collector", "no systemd --user here", "run ctx-collector some other way"))
    elif r.stdout.strip() == "active":
        out.append(Check("✓", "collector", f"{UNIT} active"))
    else:
        out.append(Check("✗", "collector", f"{UNIT} {r.stdout.strip() or 'not installed'}", "ctx install --collector"))
    sp = hook.spool_dir()
    old = [p for p in sp.glob("*.json") if time.time() - p.stat().st_mtime > 60] if sp.is_dir() else []
    bad = list((sp / "bad").glob("*.json")) if (sp / "bad").is_dir() else []
    if old:
        out.append(Check("✗", "spool", f"{len(old)} events waiting >60s in {sp}", "the collector isn't draining: check its log"))
    else:
        out.append(Check("!" if bad else "✓", "spool", f"{sp}" + (f" ({len(bad)} unreadable in bad/)" if bad else "")))
    for harness, check in (("claude", _check_claude), ("pi", _check_pi)):
        if not shutil.which(harness):
            out.append(Check("!", harness, "not installed on this host"))
            continue
        v = _version(harness)
        out.append(Check("✓" if v == VALIDATED[harness] else "!", f"{harness} version", f"{v or '?'}" +
                         ("" if v == VALIDATED[harness] else f" (payloads validated on {VALIDATED[harness]})")))
        out.append(check())
    for root in cfg.collector.claude_win_roots:
        out.append(_check_claude_win(Path(os.path.expanduser(root))))
    out.append(Check("✓", "tmux", shutil.which("tmux")) if shutil.which("tmux") else
               Check("!", "tmux", "not installed: `ctx tmux` (the manager) won't work"))
    return out


def _check_claude() -> Check:
    p = claude_settings()
    try:
        hooks = json.loads(p.read_text()).get("hooks", {}) if p.exists() else {}
    except ValueError:
        return Check("✗", "claude hooks", f"{p} is not valid JSON")
    cmds = {ev: [h.get("command", "") for g in hooks.get(ev, []) if _ours(g) for h in g["hooks"]] for ev in hook.EVENTS}
    missing = [ev for ev, c in cmds.items() if not c]
    if not any(" brief claude" in c for c in cmds.get("SessionStart", [])):
        missing.append("SessionStart (briefing)")
    if missing:
        return Check("✗", "claude hooks", f"missing for {', '.join(missing)}", "ctx install --claude")
    exe = next(iter(cmds.values()))[0].split()[0]
    if not os.access(exe, os.X_OK):
        return Check("✗", "claude hooks", f"{exe} not executable", "ctx install --claude (re-points the hooks)")
    return Check("✓", "claude hooks", f"{len(hook.EVENTS)} events -> {exe}")


def _check_claude_win(root: Path) -> Check:
    if not root.is_dir():
        return Check("✗", "claude (windows)", f"{root} missing", "fix [collector] claude_win_roots")
    from ctxswitch.common.wsl import windows_pids
    n = sum(1 for _ in root.glob("*/*.jsonl"))
    if windows_pids() is None:
        return Check("!", "claude (windows)", f"{n} transcripts; tasklist.exe unavailable, so no liveness", "enable WSL interop")
    return Check("✓", "claude (windows)", f"{n} transcripts in {root} (read-only, polled)")


def _check_pi() -> Check:
    link = pi_ext_dir() / EXT_NAME
    if not link.exists():
        return Check("✗", "pi extension", f"{link} missing" + (" (dangling link)" if link.is_symlink() else ""), "ctx install --pi")
    if link.is_symlink() and link.resolve() != ext_source().resolve():
        return Check("!", "pi extension", f"{link} -> {link.resolve()} (not this install)", "ctx install --pi")
    return Check("✓", "pi extension", str(link))


# -- CLI --------------------------------------------------------------------------------------------------------
def _parts(a) -> tuple[bool, bool, bool]:
    picked = (a.claude, a.pi, a.collector)
    return picked if any(picked) else (True, True, True)


def cmd_install(a) -> int:
    st, log = State(), print
    claude, pi, coll = _parts(a)
    if coll:
        write_config(a.hub, a.token, a.dry_run, log)
        install_collector(a.dry_run, not a.no_start, log)
    if claude:
        install_claude(st, a.dry_run, log)
    if pi:
        install_pi(st, a.dry_run, a.force, log)
    if not a.dry_run:
        log("next: `ctx doctor`")
    return 0


def cmd_uninstall(a) -> int:
    st, log = State(), print
    claude, pi, _ = _parts(a)
    if claude:
        uninstall_claude(st, a.dry_run, log)
    if pi:
        uninstall_pi(st, a.dry_run, log)
    if a.collector:                                         # only when asked: it also serves transcripts
        uninstall_collector(a.dry_run, log)
    return 0


def cmd_doctor(a) -> int:
    checks = doctor()
    w = max(len(c.name) for c in checks)
    for c in checks:
        print(f"{c.status} {c.name:<{w}}  {c.detail}" + (f"\n  {'':<{w}}  → {c.fix}" if c.fix and c.status != "✓" else ""))
    return 1 if any(c.status == "✗" for c in checks) else 0


def add_parsers(sub) -> None:
    for name, fn in (("install", cmd_install), ("uninstall", cmd_uninstall)):
        p = sub.add_parser(name, help=f"{name} Claude hooks, the pi extension and the collector unit")
        p.set_defaults(fn=fn)
        p.add_argument("--claude", action="store_true"); p.add_argument("--pi", action="store_true")
        p.add_argument("--collector", action="store_true", help="(uninstall: only removed when given)")
        p.add_argument("--dry-run", action="store_true")
        if name == "install":
            p.add_argument("--hub", help="hub url for the config"); p.add_argument("--token", help="hub token for the config")
            p.add_argument("--no-start", action="store_true", help="write the unit but don't (re)start it")
            p.add_argument("--force", action="store_true", help="replace a non-link pi extension file")
    sub.add_parser("doctor", help="check this host's wiring").set_defaults(fn=cmd_doctor)
