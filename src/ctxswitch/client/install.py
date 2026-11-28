"""ctx install / uninstall / doctor: wire this host's Claude Code, pi and collector to ctxswitch, and check it.

  install    Claude hooks merged into settings.json (ctx-hook, async; other hooks untouched) plus two synchronous
             ones: SessionStart(resume) for the resume briefing and UserPromptSubmit for the misplaced-prompt check,
             the pi extension symlinked into pi's extensions dir (it finds ctx-hook through <state>/install.json),
             a config stub, the collector's systemd --user unit (re)started. Antigravity, when it has been run here,
             gets our one named hook in its GLOBAL hooks.json (~/.gemini/config/), which fires for every agy session
             on this host.
             Idempotent. The first install backs settings.json up; state is kept in <state>/install.json.
  uninstall  settings.json restored byte-for-byte when nothing else changed it since, else our hooks removed
             surgically; agy's named hook removed with the same care; the extension link and (with --collector)
             the unit removed.
  --deck     (opt-in) `ctx deck` as a systemd --user unit, restarted when it dies and at login. The deck itself
             waits for the device, so a machine that boots with the board unplugged comes up anyway and paints it
             when `usbipd attach` runs. doctor checks the unit, the udev rule that lets us read it, and says what a
             replug costs under WSL.
  --mcp      (opt-in, not part of the default) `ctx mcp` registered with Claude Code for this user, through
             `claude mcp add/remove -s user` (~/.claude.json is Claude's own and rewritten constantly, so no edits to it),
             the skill that says when to use it linked at ~/.claude/skills/ctxswitch, the same server in pi's and
             Antigravity's JSON server lists, and the skill linked at ~/.gemini/config/skills/ctxswitch.
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

VALIDATED = {"claude": "2.1.280", "pi": "1.2.0", "agy": "1.4.11"}   # versions whose hook/extension/store payloads were checked
MARK = "ctx-hook"                                           # identifies our hook entries in settings.json
AGY_ASK_TOOL = hook.AGY_ASK_TOOL                            # the one agy tool whose calls go through us
UNIT = "ctx-collector.service"
EXT_NAME = "ctxswitch.ts"
MCP_NAME = "ctxswitch"


# -- paths ------------------------------------------------------------------------------------------------------
def state_dir() -> Path:
    return Path.home() / ".local" / "state" / "ctxswitch"


def claude_settings() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"


def pi_agent_dir() -> Path:
    return Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent")


def pi_ext_dir() -> Path:
    return pi_agent_dir() / "extensions"


def pi_mcp_json() -> Path:
    """pi-mcp-adapter's server list (pi has no MCP of its own)."""
    return pi_agent_dir() / "mcp.json"


def agy_config_dir() -> Path:
    """Antigravity's config dir; its presence is how we know agy has been run here."""
    return Path(os.environ.get("AGY_CONFIG_DIR") or Path.home() / ".gemini" / "config")


def agy_hooks_json() -> Path:
    """agy's GLOBAL hook file (~/.gemini/config/hooks.json): it fires for every agy session on this host."""
    return agy_config_dir() / "hooks.json"


def agy_mcp_json() -> Path:
    """Antigravity's server list (it has MCP of its own; the file is created empty on first run)."""
    return agy_config_dir() / "mcp_config.json"


def agy_skill_link() -> Path:
    return agy_config_dir() / "skills" / MCP_NAME


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


def ctx_exe() -> Path:
    return scripts_dir() / "ctx"


def skill_source() -> Path:
    return Path(hook.__file__).parent / "skill"


def skill_link() -> Path:
    return claude_settings().parent / "skills" / MCP_NAME


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


def prompt_check_group(command: str) -> dict[str, Any]:
    """The misplaced-prompt check: synchronous (it blocks the prompt) with a short timeout, so a hung hub can
    never hold a prompt for long (it fails open on timeout)."""
    exe = command.rsplit(" ", 1)[0]
    return {"hooks": [{"type": "command", "command": f"{exe} prompt-check", "timeout": 3}]}


def merge_hooks(settings: dict[str, Any], command: str) -> dict[str, Any]:
    """settings with exactly one spooled ctx-hook group per event in hook.EVENTS, plus the two synchronous ones
    (the briefing on SessionStart, the prompt check on UserPromptSubmit); everything else as it was."""
    out = json.loads(json.dumps(settings))
    hooks = out.setdefault("hooks", {})
    for ev in hook.EVENTS:
        groups = [g for g in hooks.get(ev, []) if not _ours(g)]
        group = {"hooks": [{"type": "command", "command": command, "async": True, "timeout": 10}]}
        groups.append({"matcher": "|".join(hook.DIALOG_TOOLS)} | group if ev == "PreToolUse" else group)
        if ev == "SessionStart":
            groups.append(brief_group(command))
        elif ev == "UserPromptSubmit":
            groups.append(prompt_check_group(command))
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


def profile_settings(cfg=None) -> list[tuple[str, Path]]:
    """(state key, settings.json) for the default Claude config and every [[claude_profiles]] config_dir, so each
    account gets the hooks. The default keeps the historical key "claude"; others are "claude:<settings path>"."""
    from ctxswitch.common.config import load
    cfg = cfg or load()
    out = [("claude", claude_settings())]
    for pr in cfg.claude_profiles:                          # every profile's dir, the default one's (~/.claude) included
        p = pr.home / "settings.json"
        if p not in [x[1] for x in out]:
            out.append((f"claude:{p}", p))
    return out


def install_claude(st: State, dry: bool, log, settings: Path | None = None, key: str = "claude") -> None:
    p = settings or claude_settings()
    raw = p.read_bytes() if p.exists() else None
    cur = json.loads(raw) if raw and raw.strip() else {}
    new = merge_hooks(cur, hook_command())
    if new == cur:
        log(f"claude: hooks already in {p}"); return
    if dry:
        log(f"claude: would add ctx-hook for {', '.join(hook.EVENTS)} to {p}"); return
    rec = st.d.get(key)
    if not rec or rec.get("settings") != str(p):            # first install: keep the original
        backup = None
        if raw is not None:
            backup = state_dir() / "backup" / f"settings.json.{time.strftime('%Y%m%d-%H%M%S')}"
            _mkdir(backup.parent); backup.write_bytes(raw); _own(backup)
        rec = {"settings": str(p), "backup": str(backup) if backup else None, "had_hooks": "hooks" in cur}
    data = _dump(new)
    _write(p, data, mode=0o600)
    rec["written_sha"] = _sha(data)
    st.d[key] = rec; st.save()
    log(f"claude: hooks added to {p}" + (f" (backup {rec['backup']})" if rec.get("backup") else ""))


def uninstall_claude(st: State, dry: bool, log, settings: Path | None = None, key: str = "claude") -> None:
    rec = st.d.get(key) or {}
    p = Path(rec.get("settings") or settings or claude_settings())
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
        st.d.pop(key, None); st.save()


# -- MCP server registration -----------------------------------------------------------------------------------
def _claude_mcp(*args: str, run=subprocess.run) -> subprocess.CompletedProcess | None:
    if not shutil.which("claude"):
        return None
    try:
        return run(["claude", "mcp", *args], capture_output=True, text=True, timeout=60, cwd=str(Path.home()))
    except (OSError, subprocess.TimeoutExpired):
        return None


def mcp_registered(run=subprocess.run) -> tuple[bool | None, str]:
    """(None: no claude here | False: not registered or another command | True: points at this install, output)."""
    r = _claude_mcp("get", MCP_NAME, run=run)
    if r is None:
        return None, ""
    return (r.returncode == 0 and str(ctx_exe()) in r.stdout), r.stdout + r.stderr


def install_mcp(st: State, dry: bool, log, run=subprocess.run) -> None:
    ok, out = mcp_registered(run)
    if ok is None:
        log("mcp: claude not on PATH here, nothing registered"); return
    if ok:
        log(f"mcp: {MCP_NAME} already registered -> {ctx_exe()} mcp"); return
    there = "No MCP server named" not in out
    if dry:
        log(f"mcp: would {'re-point' if there else 'register'} {MCP_NAME} -> {ctx_exe()} mcp (claude mcp add -s user)"); return
    if there:                                               # another venv's ctx: replace it
        _claude_mcp("remove", "-s", "user", MCP_NAME, run=run)
    r = _claude_mcp("add", "-s", "user", MCP_NAME, "--", str(ctx_exe()), "mcp", run=run)
    if r is None or r.returncode != 0:
        log(f"mcp: claude mcp add failed: {(r.stderr or r.stdout).strip() if r else 'claude did not run'}"); return
    st.d["mcp"] = {"name": MCP_NAME, "command": f"{ctx_exe()} mcp"}; st.save()
    log(f"mcp: {MCP_NAME} registered -> {ctx_exe()} mcp (new Claude sessions get it)")


def _install_skill(link: Path, label: str, dry: bool, log) -> None:
    """The skill that says when to use the tools, linked like the pi extension (a real directory there is left alone)."""
    src = skill_source()
    if link.is_symlink() and link.resolve() == src.resolve():
        log(f"{label}: already linked at {link}"); return
    if link.exists() and not link.is_symlink():
        log(f"{label}: {link} exists and is not our link; left alone"); return
    if dry:
        log(f"{label}: would link {link} -> {src}"); return
    _mkdir(link.parent)
    if link.is_symlink():
        link.unlink()
    link.symlink_to(src); _own(link)
    log(f"{label}: linked {link} -> {src}")


def _uninstall_skill(link: Path, label: str, dry: bool, log) -> None:
    if not link.is_symlink() or link.resolve() != skill_source().resolve():
        log(f"{label}: no link of ours at {link}"); return
    if dry:
        log(f"{label}: would remove {link}"); return
    link.unlink(); log(f"{label}: removed {link}")


def install_skill(st: State, dry: bool, log) -> None:
    _install_skill(skill_link(), "skill", dry, log)


def uninstall_skill(st: State, dry: bool, log) -> None:
    _uninstall_skill(skill_link(), "skill", dry, log)


def install_agy_skill(st: State, dry: bool, log) -> None:
    if not agy_config_dir().is_dir():
        log("agy skill: ~/.gemini/config not found; skipped"); return
    _install_skill(agy_skill_link(), "agy skill", dry, log)


def uninstall_agy_skill(st: State, dry: bool, log) -> None:
    _uninstall_skill(agy_skill_link(), "agy skill", dry, log)


def uninstall_mcp(st: State, dry: bool, log, run=subprocess.run) -> None:
    ok, out = mcp_registered(run)
    if ok is None or "No MCP server named" in out:
        log(f"mcp: {MCP_NAME} not registered")
        if not dry and st.d.pop("mcp", None):
            st.save()
        return
    if dry:
        log(f"mcp: would remove {MCP_NAME} (claude mcp remove -s user)"); return
    r = _claude_mcp("remove", "-s", "user", MCP_NAME, run=run)
    log(f"mcp: {MCP_NAME} removed" if r is not None and r.returncode == 0 else
        f"mcp: claude mcp remove failed: {(r.stderr or r.stdout).strip() if r else 'claude did not run'}")
    st.d.pop("mcp", None); st.save()


def mcp_entry() -> dict[str, Any]:
    return {"command": str(ctx_exe()), "args": ["mcp"]}


pi_mcp_entry = mcp_entry                                      # kept for callers that know it by pi's name


def _pi_adapter() -> bool:
    """pi-mcp-adapter is installed: listed in pi's packages, or its server list exists."""
    try:
        pkgs = json.loads((pi_agent_dir() / "settings.json").read_text()).get("packages") or []
    except (OSError, ValueError, AttributeError):
        pkgs = []
    return pi_mcp_json().exists() or any("pi-mcp-adapter" in str(x) for x in pkgs)


def _read_mcp_json(p: Path) -> tuple[dict[str, Any] | None, bytes | None]:
    """(the parsed file, its bytes) for a JSON `mcpServers` file; ({}, None) when absent, ({}, bytes) when it's
    empty (Antigravity creates a 0-byte mcp_config.json before it's ever written), (None, bytes) when it isn't
    a JSON object with an mcpServers object."""
    if not p.exists():
        return {}, None
    raw = p.read_bytes()
    if raw.strip() == b"":
        return {}, raw
    try:
        d = json.loads(raw)
    except ValueError:
        return None, raw
    return (d, raw) if isinstance(d, dict) and isinstance(d.get("mcpServers", {}), dict) else (None, raw)


def _add_mcp_entry(st: State, p: Path, label: str, key: str, prefix: str, note: str, dry: bool, log) -> None:
    """Our server in a harness's JSON server list, the rest kept as it was. The file holds other servers' keys: it's
    never printed, keeps its mode (0600 when new), and the original is backed up 0600."""
    d, raw = _read_mcp_json(p)
    if d is None:
        log(f"{label}: {p} isn't a JSON object with an mcpServers object; left alone"); return
    if (d.get("mcpServers") or {}).get(MCP_NAME) == mcp_entry():
        log(f"{label}: {MCP_NAME} already in {p}"); return
    if dry:
        log(f"{label}: would add {MCP_NAME} -> {ctx_exe()} mcp to {p}"); return
    backup = None
    if raw is not None:
        backup = state_dir() / "backup" / f"{prefix}.{time.strftime('%Y%m%d-%H%M%S')}"
        _write(backup, raw, 0o600)
    d.setdefault("mcpServers", {})[MCP_NAME] = mcp_entry()
    _write(p, (json.dumps(d, indent=2) + "\n").encode(), 0o600)
    st.d[key] = {"file": str(p), "backup": str(backup) if backup else None}; st.save()
    log(f"{label}: {MCP_NAME} added to {p}{note}" + (f"; backup {backup}" if backup else ""))


def _drop_mcp_entry(st: State, p: Path, label: str, key: str, dry: bool, log) -> None:
    d, _ = _read_mcp_json(p)
    if not d or MCP_NAME not in (d.get("mcpServers") or {}):
        log(f"{label}: {MCP_NAME} not in {p}")
        if not dry and st.d.pop(key, None):
            st.save()
        return
    if dry:
        log(f"{label}: would remove {MCP_NAME} from {p}"); return
    del d["mcpServers"][MCP_NAME]
    _write(p, (json.dumps(d, indent=2) + "\n").encode())
    st.d.pop(key, None); st.save()
    log(f"{label}: {MCP_NAME} removed from {p}")


def install_pi_mcp(st: State, dry: bool, log) -> None:
    if not _pi_adapter():
        log("pi mcp: pi-mcp-adapter not installed here; pi agents don't get the ctxswitch tools"); return
    _add_mcp_entry(st, pi_mcp_json(), "pi mcp", "pi_mcp", "pi-mcp.json", " (new pi sessions get it)", dry, log)


def uninstall_pi_mcp(st: State, dry: bool, log) -> None:
    _drop_mcp_entry(st, pi_mcp_json(), "pi mcp", "pi_mcp", dry, log)


def install_agy_mcp(st: State, dry: bool, log) -> None:
    if not agy_config_dir().is_dir():
        log("agy mcp: ~/.gemini/config not found; Antigravity agents don't get the ctxswitch tools"); return
    _add_mcp_entry(st, agy_mcp_json(), "agy mcp", "agy_mcp", "agy-mcp.json", " (new Antigravity sessions get it)", dry, log)


def uninstall_agy_mcp(st: State, dry: bool, log) -> None:
    _drop_mcp_entry(st, agy_mcp_json(), "agy mcp", "agy_mcp", dry, log)


# -- Antigravity hooks ------------------------------------------------------------------------------------------
AGY_HOOK_NAME = "ctxswitch"                                 # our one entry in agy's global hooks.json
AGY_FLAT = ("SessionStart", "PreInvocation", "Stop")        # these fire for every session/turn, matcher would do nothing
AGY_MATCHED = ("PreToolUse", "PostToolUse")                 # only these go through a matcher, and only for one tool
AGY_EVENTS = (*AGY_FLAT, *AGY_MATCHED)


def agy_hook_groups(command: str) -> dict[str, list[dict[str, Any]]]:
    """Our handlers per event: a flat list where agy fires it for everything, ``{"matcher": …, "hooks": […]}`` for
    the tool events, where ``"*"`` would put every tool call through us. Synchronous with a short timeout: agy's
    hooks block its loop and run for EVERY session on this host."""
    h = {"type": "command", "timeout": 5}
    return {ev: ([h | {"command": agy_event_command(command, ev)}] if ev in AGY_FLAT          # flat: the handlers
                 else [{"matcher": AGY_ASK_TOOL, "hooks": [h | {"command": agy_event_command(command, ev)}]}])
            for ev in AGY_EVENTS}


def agy_event_command(command: str, ev: str) -> str:
    """The shell line for one event. PreToolUse gets a fallback in the shell itself: agy DENIES a tool when the hook
    prints nothing, so even a ctx-hook that can't start (a broken venv) must leave {"decision":"ask"} behind."""
    if ev != "PreToolUse":
        return f"{command} {ev}"
    return (f"out=$({command} {ev}); if [ -n \"$out\" ]; then printf '%s\\n' \"$out\"; "
            f"else echo '{{\"decision\":\"ask\"}}'; fi")


def agy_hook_command() -> str:
    return f"{hook_exe()} agy"


def _read_json_obj(p: Path) -> tuple[dict[str, Any] | None, bytes | None]:
    """(the parsed file, its bytes) for a JSON object file; ({}, None) when absent, ({}, bytes) when it's empty (agy
    creates a 0-byte file before it's ever written), (None, bytes) when it isn't a JSON object."""
    if not p.exists():
        return {}, None
    raw = p.read_bytes()
    if raw.strip() == b"":
        return {}, raw
    try:
        d = json.loads(raw)
    except ValueError:
        return None, raw
    return (d, raw) if isinstance(d, dict) else (None, raw)


def _hook_cmd(x: Any, event: str) -> bool:
    """Whether one of agy's handlers for ``event`` is one of ours (its own trailing event name included, so a moved
    venv's path still reads as ours and gets re-pointed instead of left beside ours)."""
    return isinstance(x, dict) and f"{MARK} agy {event}" in str(x.get("command", ""))


def _agy_ours(x: Any, event: str) -> bool:
    """Whether one of agy's groups for ``event`` is ours: a handler of ours in it, or (a flat list) is it."""
    if isinstance(x, dict) and isinstance(x.get("hooks"), list):          # a matcher group
        return any(_hook_cmd(h, event) for h in x["hooks"] if isinstance(h, dict))
    return _hook_cmd(x, event)                                             # a flat handler


def _agy_groups(entry: Any, event: str) -> list[Any]:
    """The event's groups (flat handlers or matcher groups, as they're written) with ours dropped, the rest untouched.
    Anything that is neither (agy couldn't run it for these events) is dropped."""
    v = entry.get(event) if isinstance(entry, dict) else None
    if isinstance(v, dict):                                 # a single group written on its own
        v = [v]
    elif not isinstance(v, list):
        return []
    return [g for g in v if not _agy_ours(g, event)]


def _agy_entry(entry: Any, want: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """What our named hook's entry should hold: our handlers for every event, every group of anyone else's kept in
    front, and any other key the entry already carried."""
    out = dict(entry) if isinstance(entry, dict) else {}
    for ev in AGY_EVENTS:
        out[ev] = _agy_groups(entry, ev) + want[ev]
    return out


def install_agy_hooks(st: State, dry: bool, log, path: Path | None = None) -> None:
    """Our one named hook in agy's GLOBAL hooks.json, which fires for every agy session on this host. Every other
    named hook is kept as it was; a file that isn't a JSON object is left alone."""
    p = path or agy_hooks_json()
    if not agy_config_dir().is_dir():
        log("agy hooks: ~/.gemini/config not found; skipped"); return
    d, raw = _read_json_obj(p)
    if d is None:                                           # agy's format: each named hook is a top-level key
        log(f"agy hooks: {p} isn't a JSON object; left alone"); return
    entry = d.get(AGY_HOOK_NAME)
    want = _agy_entry(entry, agy_hook_groups(agy_hook_command()))
    if entry == want:
        log(f"agy hooks: hooks already in {p}"); return
    if dry:
        log(f"agy hooks: would add {AGY_HOOK_NAME} for {', '.join(AGY_EVENTS)} to {p}"); return
    backup = None
    if raw is not None:
        backup = state_dir() / "backup" / f"agy-hooks.json.{time.strftime('%Y%m%d-%H%M%S')}"
        _write(backup, raw, 0o600)
    d[AGY_HOOK_NAME] = want
    data = _dump(d)
    _write(p, data, mode=0o600)
    st.d["agy_hooks"] = {"file": str(p), "backup": str(backup) if backup else None, "sha": _sha(data)}
    st.save()
    log(f"agy hooks: {AGY_HOOK_NAME} added to {p} (every agy session on this host)" + (f"; backup {backup}" if backup else ""))


def _agy_drop(p: Path, raw: bytes) -> bool:
    """Take our named hook out of the file as it stands (every other named hook, group and key as it was).
    False when it holds nothing of ours or isn't a JSON object."""
    try:
        d = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return False
    if not isinstance(d, dict):
        return False
    if AGY_HOOK_NAME not in d:
        return False
    del d[AGY_HOOK_NAME]
    _write(p, _dump(d))
    return True


def uninstall_agy_hooks(st: State, dry: bool, log, path: Path | None = None) -> None:
    """Remove only our named hook; agy's own and every other one stay, and so does everything else in the file.
    Nothing changed it since we wrote it: the original comes back byte-for-byte."""
    rec = st.d.get("agy_hooks") or {}
    p = path or Path(rec.get("file") or agy_hooks_json())
    if not p.exists():
        log(f"agy hooks: {p} missing, nothing to do"); return
    raw = p.read_bytes()
    if rec and rec.get("sha") == _sha(raw):                  # untouched since we wrote it
        if dry:
            log(f"agy hooks: would {'restore' if rec.get('backup') else 'remove'} {p}"); return
        if rec.get("backup") and Path(rec["backup"]).exists():
            _write(p, Path(rec["backup"]).read_bytes())
            log(f"agy hooks: restored {p} byte-for-byte from {rec['backup']}")
        elif not rec.get("backup"):
            p.unlink(); log(f"agy hooks: removed {p} (hooks.json didn't exist before install)")
        else:
            log(f"agy hooks: backup {rec['backup']} is gone; removing our hook instead")
            _agy_drop(p, raw)
        st.d.pop("agy_hooks", None); st.save()
        return
    d, _ = _read_json_obj(p)
    if d is None:
        log(f"agy hooks: {p} isn't a JSON object; left alone"); return
    if dry:
        log(f"agy hooks: would remove {AGY_HOOK_NAME} from {p}"); return
    if _agy_drop(p, raw):
        log(f"agy hooks: {AGY_HOOK_NAME} removed from {p} (other hooks kept)")
    else:
        log(f"agy hooks: {AGY_HOOK_NAME} not in {p}")
    st.d.pop("agy_hooks", None); st.save()


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


# -- the Stream Deck ---------------------------------------------------------------------------------------------
#
# `ctx deck` is the one part of this that runs against hardware someone unplugs, so it is the one part that needs to
# come back by itself: `Restart=always` covers a crash, and the deck's own device supervisor covers the unplugs —
# it waits for a device rather than exiting without one, and picks it up when `usbipd attach` finally runs.
DECK_UNIT = "ctx-deck.service"
DECK_UNIT_TEMPLATE = """\
# written by `ctx install --deck`; `ctx uninstall --deck` removes it
[Unit]
Description=ctxswitch Stream Deck
After=ctx-collector.service

[Service]
Type=simple
ExecStart={exe} deck
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
StandardOutput=append:%h/.local/state/ctxswitch/deck.log
StandardError=append:%h/.local/state/ctxswitch/deck.log

[Install]
WantedBy=default.target
"""
DECK_INSTALL_CMD = "uv pip install -e '.[deck]'"               # said out loud rather than guessed at, when it's missing
UDEV_RULE = Path("/etc/udev/rules.d/70-streamdeck.rules")
UDEV_RULE_BODY = (
    'SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", MODE="0666"\n'
    'KERNEL=="hidraw*", ATTRS{idVendor}=="0fd9", MODE="0666"\n')


def deck_unit_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / DECK_UNIT


def udev_rules_path() -> Path:
    """Where the rules live. Overridable for the same reason every other path here is: a test gets its own HOME."""
    return Path(os.environ.get("CTXSWITCH_UDEV_RULES") or UDEV_RULE)


def deck_extra_missing() -> str | None:
    """The name of the first import `ctx deck` needs that this environment doesn't have, or None.

    Found rather than imported: `find_spec` says whether the package is installed without running it, which for
    streamdeck means opening libusb and looking at the USB bus. The check is here because the unit is worth writing
    only to a machine that can run it — a service that exits 2 on every restart is worse than no service."""
    import importlib.util
    for name in ("StreamDeck", "PIL"):
        try:
            if importlib.util.find_spec(name) is None:
                return name
        except (ImportError, ValueError):                       # a parent package that can't be parsed is missing too
            return name
    return None


def install_deck(dry: bool, start: bool, log) -> None:
    missing = deck_extra_missing()
    if missing:
        log(f"deck: the Stream Deck library isn't installed ({missing} missing); run: {DECK_INSTALL_CMD}"); return
    if not dry:
        _mkdir(state_dir())                                     # deck.log is appended to in here
    p, body = deck_unit_path(), DECK_UNIT_TEMPLATE.format(exe=ctx_exe())
    changed = not p.exists() or p.read_text() != body
    if dry:
        log(f"deck: would {'write' if changed else 'keep'} {p}" + (" and start it" if start else "")); return
    if changed:
        _write(p, body.encode()); log(f"deck: wrote {p}")
    if not start:
        return
    for c in (("daemon-reload",), ("enable", DECK_UNIT), ("restart", DECK_UNIT)):
        r = _systemctl(*c)
        if r is None or r.returncode != 0:
            log(f"deck: `systemctl --user {' '.join(c)}` failed ({(r.stderr.strip() if r else 'no systemd --user')}); "
                f"run: systemctl --user daemon-reload && systemctl --user enable --now {DECK_UNIT}")
            return
    log("deck: enabled and restarted")


def uninstall_deck(dry: bool, log) -> None:
    p = deck_unit_path()
    if not p.exists():
        log("deck: no unit installed"); return
    if dry:
        log(f"deck: would stop, disable and remove {p}"); return
    _systemctl("disable", "--now", DECK_UNIT)
    p.unlink(); _systemctl("daemon-reload")
    log(f"deck: stopped and removed {p} (its log and settings kept)")


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
    if r.returncode != 0:                                  # a crash (pi on Node 20) prints Node's own version
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
    out.extend(_check_deck_unit())                            # nothing at all when the deck unit isn't installed
    sp = hook.spool_dir()
    old = [p for p in sp.glob("*.json") if time.time() - p.stat().st_mtime > 60] if sp.is_dir() else []
    bad = list((sp / "bad").glob("*.json")) if (sp / "bad").is_dir() else []
    if old:
        out.append(Check("✗", "spool", f"{len(old)} events waiting >60s in {sp}", "the collector isn't draining: check its log"))
    else:
        out.append(Check("!" if bad else "✓", "spool", f"{sp}" + (f" ({len(bad)} unreadable in bad/)" if bad else "")))
    for harness, check in (("claude", _check_claude), ("pi", _check_pi), ("agy", lambda: _check_agy(cfg))):
        if not shutil.which(harness):
            out.append(Check("!", harness, "not installed on this host"))
            continue
        v = _version(harness)
        out.append(Check("✓" if v == VALIDATED[harness] else "!", f"{harness} version", f"{v or '?'}" +
                         ("" if v == VALIDATED[harness] else f" (payloads validated on {VALIDATED[harness]})")))
        if harness == "claude":
            for key, sp_ in profile_settings(cfg):
                name = next((pr.name for pr in cfg.claude_profiles if pr.config_dir and pr.home / "settings.json" == sp_), None)
                out.append(_check_claude(sp_, f"claude hooks [{name}]" if name else "claude hooks"))
        else:
            out.append(check())
    if agy_config_dir().is_dir():                           # agy has run here: its hooks are ours to check
        out.append(_check_agy_hooks())
    for root in cfg.collector.claude_win_roots:
        out.append(_check_claude_win(Path(os.path.expanduser(root))))
    if State().d.get("mcp"):                                # opt-in: only checked once installed
        ok, _ = mcp_registered()
        out.append(Check("✓", "mcp", f"{MCP_NAME} -> {ctx_exe()} mcp") if ok else
                   Check("!", "mcp", f"{MCP_NAME} not registered for this install" if ok is False else "claude not on PATH",
                         "ctx install --mcp"))
    if State().d.get("pi_mcp"):
        d, _ = _read_mcp_json(pi_mcp_json())
        ok = bool(d) and (d.get("mcpServers") or {}).get(MCP_NAME) == mcp_entry()
        out.append(Check("✓", "pi mcp", f"{MCP_NAME} in {pi_mcp_json()}") if ok else
                   Check("!", "pi mcp", f"{MCP_NAME} missing from {pi_mcp_json()} or another command", "ctx install --mcp"))
    if State().d.get("agy_mcp"):
        p = agy_mcp_json()
        d, _ = _read_mcp_json(p)
        ok = bool(d) and (d.get("mcpServers") or {}).get(MCP_NAME) == mcp_entry()
        out.append(Check("✓", "agy mcp", f"{MCP_NAME} in {p}") if ok else
                   Check("!", "agy mcp", f"{MCP_NAME} missing from {p} or another command", "ctx install --mcp"))
    out.append(Check("✓", "tmux", shutil.which("tmux")) if shutil.which("tmux") else
               Check("!", "tmux", "not installed: `ctx tmux` (the manager) won't work"))
    return out


def _check_claude(p: Path | None = None, label: str = "claude hooks") -> Check:
    p = p or claude_settings()
    try:
        hooks = json.loads(p.read_text()).get("hooks", {}) if p.exists() else {}
    except ValueError:
        return Check("✗", label, f"{p} is not valid JSON")
    cmds = {ev: [h.get("command", "") for g in hooks.get(ev, []) if _ours(g) for h in g["hooks"]] for ev in hook.EVENTS}
    missing = [ev for ev, c in cmds.items() if not c]
    if not any(" brief claude" in c for c in cmds.get("SessionStart", [])):
        missing.append("SessionStart (briefing)")
    if not any(" prompt-check" in c for c in cmds.get("UserPromptSubmit", [])):
        missing.append("UserPromptSubmit (prompt check)")
    if missing:
        return Check("✗", label, f"missing for {', '.join(missing)}", "ctx install --claude")
    exe = next(iter(cmds.values()))[0].split()[0]
    if not os.access(exe, os.X_OK):
        return Check("✗", label, f"{exe} not executable", "ctx install --claude (re-points the hooks)")
    return Check("✓", label, f"{len(hook.EVENTS)} events -> {exe}")


def _check_agy(cfg: Config) -> Check:
    """Antigravity conversations are read straight from its SQLite store; say where and how many."""
    roots = [Path(os.path.expanduser(r)) for r in cfg.collector.agy_roots]
    found = [(r, len(list(r.glob("*.db")))) for r in roots if r.is_dir()]
    if not found:
        return Check("!", "agy store", f"no conversations yet in {', '.join(map(str, roots))}")
    return Check("✓", "agy store", ", ".join(f"{n} conversation{'s' if n != 1 else ''} in {r}" for r, n in found))


def _check_agy_hooks() -> Check:
    """Our named hook in agy's global hooks.json: ! when it's missing or another install's (never ✗ — agy works
    without us, and a file we can't read is agy's own to fix)."""
    p = agy_hooks_json()
    if not agy_config_dir().is_dir():
        return Check("!", "agy hooks", f"{agy_config_dir()} missing: agy has never run here", "ctx install")
    d, _ = _read_json_obj(p)
    if d is None:
        return Check("!", "agy hooks", f"{p} is not valid JSON; left alone", "fix it, then ctx install")
    command = agy_hook_command()
    entry = d.get(AGY_HOOK_NAME)
    want = _agy_entry(entry, agy_hook_groups(command))
    if not isinstance(entry, dict) or entry != want:        # absent, another venv's, or an event was edited away
        miss = list(AGY_EVENTS) if not isinstance(entry, dict) else [ev for ev in AGY_EVENTS if entry.get(ev) != want[ev]]
        return Check("!", "agy hooks", f"{p}: {AGY_HOOK_NAME} missing for {', '.join(miss)}", "ctx install")
    exe = command.split()[0]
    if not os.access(exe, os.X_OK):
        return Check("!", "agy hooks", f"{exe} not executable", "ctx install (re-points the hooks)")
    return Check("✓", "agy hooks", f"{len(AGY_EVENTS)} events -> {command} … (fires for every agy session here)")


def _check_claude_win(root: Path) -> Check:
    if not root.is_dir():
        return Check("✗", "claude (windows)", f"{root} missing", "fix [collector] claude_win_roots")
    from ctxswitch.common.wsl import windows_pids
    n = sum(1 for _ in root.glob("*/*.jsonl"))
    if windows_pids() is None:
        return Check("!", "claude (windows)", f"{n} transcripts; tasklist.exe unavailable, so no liveness", "enable WSL interop")
    return Check("✓", "claude (windows)", f"{n} transcripts in {root} (read-only, polled)")


def _check_deck_unit() -> list[Check]:
    """The deck's service, and the two things that can stop it reaching the hardware: a udev rule that lets this
    user read the board at all, and — on WSL, where the board is on Windows' USB stack until someone hands it over
    — the attach that a replug costs. Only asked when the unit is installed: a host with no deck to run has nothing
    here to be wrong, and `ctx doctor` must not fail on account of hardware it was never given."""
    p = deck_unit_path()
    if not p.exists():
        return []
    r = _systemctl("is-active", DECK_UNIT)
    out = [Check("!", "deck", "no systemd --user here", "run ctx deck some other way") if r is None else
           Check("✓", "deck", f"{DECK_UNIT} active") if r.stdout.strip() == "active" else
           Check("✗", "deck", f"{DECK_UNIT} {r.stdout.strip() or 'not installed'}",
                 f"check {state_dir() / 'deck.log'}; ctx install --deck")]
    rules = udev_rules_path()
    want = [l for l in UDEV_RULE_BODY.splitlines() if "idVendor" in l]
    fix = (f"write these two lines to {rules}:\n" + "\n".join(f"  {l}" for l in want)
           + "\n  then: sudo udevadm control --reload-rules && sudo udevadm trigger")
    if not rules.exists():
        out.append(Check("✗", "deck udev", f"{rules} missing: this user can't read the board", fix))
    else:
        try:
            body = rules.read_text()
        except OSError as e:
            out.append(Check("!", "deck udev", f"{rules} can't be read ({e.strerror or e})"))
        else:
            miss = [l for l in want if l not in body]
            out.append(Check("✓", "deck udev", str(rules)) if not miss else
                       Check("✗", "deck udev", f"{rules} is missing {'; '.join(miss)}", fix))
    from ctxswitch.common.wsl import is_wsl
    if is_wsl():
        out.append(Check("!", "deck (wsl)", "after a replug the board is still on the Windows side until "
                                           "`usbipd attach --wsl --busid <id>` is run from Windows "
                                           "(no --auto-attach: it locks up; the service waits and picks it up itself)"))
    return out


def _check_pi() -> Check:
    link = pi_ext_dir() / EXT_NAME
    if not link.exists():
        return Check("✗", "pi extension", f"{link} missing" + (" (dangling link)" if link.is_symlink() else ""), "ctx install --pi")
    if link.is_symlink() and link.resolve() != ext_source().resolve():
        return Check("!", "pi extension", f"{link} -> {link.resolve()} (not this install)", "ctx install --pi")
    return Check("✓", "pi extension", str(link))


# -- CLI --------------------------------------------------------------------------------------------------------
def _parts(a) -> tuple[bool, bool, bool]:
    """Which of the three wirings to do: the ones named, or all three when nothing was named at all. `--deck` and
    `--mcp` name nothing of their own but must still count as having said something — otherwise `ctx uninstall
    --deck` would read as "no flags" and take the hooks back too."""
    picked = (a.claude, a.pi, a.collector)
    return picked if any(picked) or a.mcp or getattr(a, "deck", False) else (True, True, True)


def cmd_install(a) -> int:
    st, log = State(), print
    claude, pi, coll = _parts(a)
    if coll:
        write_config(a.hub, a.token, a.dry_run, log)
        install_collector(a.dry_run, not a.no_start, log)
    if getattr(a, "deck", False):                           # only when asked: most hosts have no deck on them
        install_deck(a.dry_run, not a.no_start, log)
    if claude:
        for key, p in profile_settings():
            install_claude(st, a.dry_run, log, p, key)
    if pi:
        install_pi(st, a.dry_run, a.force, log)
    if claude or pi:                                        # the harness wiring, --mcp alone doesn't touch it
        install_agy_hooks(st, a.dry_run, log)
    if a.mcp:
        install_mcp(st, a.dry_run, log)
        install_skill(st, a.dry_run, log)
        install_pi_mcp(st, a.dry_run, log)
        install_agy_mcp(st, a.dry_run, log)
        install_agy_skill(st, a.dry_run, log)
    if not a.dry_run:
        log("next: `ctx doctor`")
    return 0


def cmd_uninstall(a) -> int:
    st, log = State(), print
    claude, pi, _ = _parts(a)
    if claude:
        keys = {k for k in st.d if k == "claude" or k.startswith("claude:")}
        for key, p in profile_settings():
            keys.discard(key); uninstall_claude(st, a.dry_run, log, p, key)
        for key in sorted(keys):                                # profiles since dropped from the config
            uninstall_claude(st, a.dry_run, log, None, key)
    if pi:
        uninstall_pi(st, a.dry_run, log)
    if claude or pi:                                        # the harness wiring, not `--mcp` on its own
        uninstall_agy_hooks(st, a.dry_run, log)
    if a.collector:                                         # only when asked: it also serves transcripts
        uninstall_collector(a.dry_run, log)
    if getattr(a, "deck", False):                           # only when asked, same as the unit it serves
        uninstall_deck(a.dry_run, log)
    if a.mcp:
        uninstall_mcp(st, a.dry_run, log)
        uninstall_skill(st, a.dry_run, log)
        uninstall_pi_mcp(st, a.dry_run, log)
        uninstall_agy_mcp(st, a.dry_run, log)
        uninstall_agy_skill(st, a.dry_run, log)
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
        p.add_argument("--deck", action="store_true",
                       help="the Stream Deck service: `ctx deck` restarted by systemd when it dies, and after a "
                            "replug (uninstall: only removed when given)")
        p.add_argument("--mcp", action="store_true",
                       help="`ctx mcp` registered with Claude Code, pi and Antigravity (only when given)")
        p.add_argument("--dry-run", action="store_true")
        if name == "install":
            p.add_argument("--hub", help="hub url for the config"); p.add_argument("--token", help="hub token for the config")
            p.add_argument("--no-start", action="store_true", help="write the unit but don't (re)start it")
            p.add_argument("--force", action="store_true", help="replace a non-link pi extension file")
    sub.add_parser("doctor", help="check this host's wiring").set_defaults(fn=cmd_doctor)
