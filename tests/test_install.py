"""ctx install / uninstall against a throwaway HOME: merge, idempotence, byte-for-byte restore, surgical removal."""
import json
import os

import pytest

from ctxswitch import hook
from ctxswitch.client import install as ins

SAINT = {"matcher": "startup|resume", "hooks": [{"type": "command", "command": "/x/saint-pool --hook", "timeout": 5}]}
ORIGINAL = '{\n    "model": "opus",\n    "hooks": {"SessionStart": [' + json.dumps(SAINT) + ']}\n}'   # odd indent on purpose


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for v in ("CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR", "XDG_CONFIG_HOME", "CTXSWITCH_SPOOL", "CTXSWITCH_CONFIG",
              "HERDR_APP_HOME"):
        monkeypatch.delenv(v, raising=False)
    (tmp_path / ".claude").mkdir()
    monkeypatch.setattr(ins, "windows_claude_root", lambda: None)               # not this laptop's real Windows home
    monkeypatch.setattr(ins, "hc_app_exe", lambda which=None: None)             # nor necessarily herdr-app
    return tmp_path


def _settings(home):
    return json.loads((home / ".claude" / "settings.json").read_text())


def _run(fn, **kw):
    lines = []
    fn(ins.State(), dry=kw.pop("dry", False), log=lines.append, **kw)
    return lines


def test_merge_keeps_other_hooks_and_is_idempotent(home):
    p = home / ".claude" / "settings.json"
    p.write_text(ORIGINAL); os.chmod(p, 0o600)
    _run(ins.install_claude)
    s = _settings(home)
    assert s["model"] == "opus" and s["hooks"]["SessionStart"][0] == SAINT
    for ev in hook.EVENTS:
        ours = [g for g in s["hooks"][ev] if ins._ours(g)]
        assert len(ours) == (2 if ev == "SessionStart" else 1) and ours[0]["hooks"][0]["async"] is True
    (brief,) = [g for g in s["hooks"]["SessionStart"] if ins._ours(g) and "brief" in g["hooks"][0]["command"]]
    assert brief["matcher"] == "resume" and "async" not in brief["hooks"][0]                      # output is dropped if async
    assert brief["hooks"][0]["command"] == f"{ins.hook_exe()} brief claude"
    assert [g.get("matcher") for g in s["hooks"]["PreToolUse"] if ins._ours(g)] == ["AskUserQuestion|ExitPlanMode"]   # not every tool
    assert all("matcher" not in g for g in s["hooks"]["Stop"] if ins._ours(g))
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    before = p.read_bytes()
    assert "already" in _run(ins.install_claude)[0]
    assert p.read_bytes() == before
    backups = list((home / ".local/state/ctxswitch/backup").iterdir())
    assert len(backups) == 1 and backups[0].read_text() == ORIGINAL


def test_reinstall_with_a_moved_venv_repoints_without_duplicates(home, monkeypatch):
    (home / ".claude" / "settings.json").write_text(ORIGINAL)
    _run(ins.install_claude)
    monkeypatch.setattr(ins, "hook_command", lambda: "/new/venv/bin/ctx-hook claude")
    _run(ins.install_claude)
    s = _settings(home)
    cmds = [h["command"] for g in s["hooks"]["Stop"] for h in g["hooks"]]
    assert cmds == ["/new/venv/bin/ctx-hook claude"]
    _run(ins.uninstall_claude)                                     # still the pre-ctx original
    assert (home / ".claude" / "settings.json").read_text() == ORIGINAL


def test_uninstall_restores_byte_for_byte(home):
    p = home / ".claude" / "settings.json"
    p.write_text(ORIGINAL)
    _run(ins.install_claude)
    assert "byte-for-byte" in _run(ins.uninstall_claude)[0]
    assert p.read_text() == ORIGINAL
    assert "claude" not in ins.State().d


def test_uninstall_after_other_edits_is_surgical(home):
    p = home / ".claude" / "settings.json"
    p.write_text(ORIGINAL)
    _run(ins.install_claude)
    s = _settings(home); s["theme"] = "dark"; p.write_text(json.dumps(s))          # Claude changed something since
    assert "other changes kept" in _run(ins.uninstall_claude)[0]
    s = _settings(home)
    assert s["theme"] == "dark" and s["hooks"] == {"SessionStart": [SAINT]}


def test_no_settings_before_install(home):
    _run(ins.install_claude)
    _run(ins.uninstall_claude)
    assert not (home / ".claude" / "settings.json").exists()
    (home / ".claude" / "settings.json").write_text('{"a": 1}')
    _run(ins.install_claude)
    s = _settings(home); s["b"] = 2; (home / ".claude" / "settings.json").write_text(json.dumps(s))
    _run(ins.uninstall_claude)
    assert _settings(home) == {"a": 1, "b": 2}                                     # no empty "hooks" left behind


def test_dry_run_changes_nothing(home):
    p = home / ".claude" / "settings.json"
    p.write_text(ORIGINAL)
    assert "would add" in _run(ins.install_claude, dry=True)[0]
    assert p.read_text() == ORIGINAL and not (home / ".local").exists()


def test_pi_link_and_unlink(home):
    st = ins.State()
    lines = []
    ins.install_pi(st, False, False, lines.append)
    link = home / ".pi" / "agent" / "extensions" / "ctxswitch.ts"
    assert link.is_symlink() and link.resolve() == ins.ext_source().resolve()
    assert ins.State().d["pi"]["hook"] == str(ins.hook_exe())                          # the extension reads this
    ins.install_pi(st, False, False, lines.append)
    assert "already" in lines[-1]
    ins.uninstall_pi(st, False, lines.append)
    assert not link.exists() and not link.is_symlink()
    link.write_text("// someone else's")                                             # not ours: left alone
    ins.install_pi(st, False, False, lines.append)
    assert "left alone" in lines[-1] and link.read_text() == "// someone else's"


def test_collector_unit_points_at_this_venv(home, monkeypatch):
    calls = []
    monkeypatch.setattr(ins, "_systemctl", lambda *a: calls.append(a) or None)
    lines = []
    ins.install_collector(False, True, lines.append)
    unit = (home / ".config/systemd/user/ctx-collector.service").read_text()
    assert f"ExecStart={ins.scripts_dir() / 'ctx-collector'}\n" in unit
    assert (home / ".local/state/ctxswitch/spool").is_dir()
    assert calls == [("daemon-reload",)] and "run: systemctl" in lines[-1]            # no systemd: says what to run


def test_config_stub(home, monkeypatch):
    import tomllib
    lines = []
    ins.write_config("http://hub.local:7433", "t0k", False, lines.append)
    cfg = tomllib.loads((home / ".config/ctxswitch/config.toml").read_text())
    assert cfg["hub"] == {"url": "http://hub.local:7433", "token": "t0k"} and cfg["collector"]["host_id"]
    assert oct((home / ".config/ctxswitch/config.toml").stat().st_mode & 0o777) == "0o600"
    ins.write_config(None, None, False, lines.append)
    assert "unchanged" in lines[-1]


def test_doctor_flags_missing_wiring(home, monkeypatch):
    monkeypatch.setattr(ins, "_systemctl", lambda *a: None)
    monkeypatch.setattr(ins.shutil, "which", lambda c: None)
    from ctxswitch.common.config import Config
    cfg = Config(); cfg.hub.url = "http://127.0.0.1:9"                               # nothing listens
    checks = {c.name: c for c in ins.doctor(cfg)}
    assert checks["hub"].status == "✗" and checks["config"].status == "!"
    assert checks["claude"].status == "!" and checks["collector"].status == "!"
    (home / ".claude" / "settings.json").write_text(ORIGINAL)
    _run(ins.install_claude)
    assert ins._check_claude().status == "✓"
    s = _settings(home); s["hooks"]["Stop"] = []; (home / ".claude" / "settings.json").write_text(json.dumps(s))
    c = ins._check_claude()
    assert c.status == "✗" and "Stop" in c.detail
    s["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": ins.hook_command()}]}]
    s["hooks"]["SessionStart"] = [g for g in s["hooks"]["SessionStart"] if "brief" not in str(g)]
    (home / ".claude" / "settings.json").write_text(json.dumps(s))
    c = ins._check_claude()
    assert c.status == "✗" and c.detail == "missing for SessionStart (briefing)"


class FakeClaudeMcp:
    """`claude mcp get/add/remove` over a dict, like the real CLI's output and exit codes."""
    def __init__(self, servers=None):
        self.servers, self.calls = dict(servers or {}), []

    def __call__(self, argv, **kw):
        assert argv[:2] == ["claude", "mcp"] and kw.get("timeout")
        self.calls.append(argv[2:])
        verb, rest = argv[2], argv[3:]
        r = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if verb == "get":
            if rest[0] in self.servers:
                r.stdout = f"{rest[0]}:\n  Scope: User config\n  Type: stdio\n  Command: {self.servers[rest[0]]}\n"
            else:
                r.returncode, r.stdout = 1, f'No MCP server named "{rest[0]}". Configured servers: {", ".join(self.servers)}'
        elif verb == "add":
            assert rest[:2] == ["-s", "user"] and rest[3] == "--"
            if rest[2] in self.servers:
                r.returncode, r.stderr = 1, f"MCP server {rest[2]} already exists in user config"
            else:
                self.servers[rest[2]] = " ".join(rest[4:])
        elif verb == "remove":
            assert rest[:2] == ["-s", "user"]
            if self.servers.pop(rest[2], None) is None:
                r.returncode = 1
        return r


def test_mcp_registration_is_opt_in_idempotent_and_repoints(home, monkeypatch):
    monkeypatch.setattr(ins.shutil, "which", lambda c: "/usr/bin/" + c)
    fake = FakeClaudeMcp({"other": "x"})
    ours = f"{ins.ctx_exe()} mcp"
    assert _run(ins.install_mcp, dry=True, run=fake)[0].startswith("mcp: would register")
    assert fake.servers == {"other": "x"}
    _run(ins.install_mcp, run=fake)
    assert fake.servers == {"other": "x", "ctxswitch": ours} and ins.State().d["mcp"]["command"] == ours
    n = len(fake.calls)
    assert "already registered" in _run(ins.install_mcp, run=fake)[0] and fake.calls[n:] == [["get", "ctxswitch"]]
    fake.servers["ctxswitch"] = "/old/venv/bin/ctx mcp"                # a moved venv: replaced, not "already exists"
    _run(ins.install_mcp, run=fake)
    assert fake.servers["ctxswitch"] == ours
    _run(ins.uninstall_mcp, run=fake)
    assert fake.servers == {"other": "x"} and "mcp" not in ins.State().d
    assert _run(ins.uninstall_mcp, run=fake) == ["mcp: ctxswitch not registered"]
    monkeypatch.setattr(ins.shutil, "which", lambda c: None)
    assert "not on PATH" in _run(ins.install_mcp, run=fake)[0]


def test_install_without_flags_leaves_mcp_alone(home, monkeypatch):
    seen = []
    for name in ("write_config", "install_collector", "install_claude", "install_pi", "install_mcp", "install_skill", "install_pi_mcp"):
        monkeypatch.setattr(ins, name, lambda *a, _n=name, **k: seen.append(_n))
    args = dict(claude=False, pi=False, collector=False, mcp=False, dry_run=False, hub=None, token=None, no_start=True, force=False)
    ins.cmd_install(type("A", (), args)())
    assert seen == ["write_config", "install_collector", "install_claude", "install_pi"]
    seen.clear()
    ins.cmd_install(type("A", (), args | {"mcp": True})())
    assert seen == ["install_mcp", "install_skill", "install_pi_mcp"]   # --mcp alone: only that


def test_doctor_checks_mcp_only_once_installed(home, monkeypatch):
    monkeypatch.setattr(ins, "_systemctl", lambda *a: None)
    monkeypatch.setattr(ins.shutil, "which", lambda c: None)
    from ctxswitch.common.config import Config
    cfg = Config(); cfg.hub.url = "http://127.0.0.1:9"
    answer = [(True, "")]
    monkeypatch.setattr(ins, "mcp_registered", lambda: answer[0])
    assert "mcp" not in {c.name for c in ins.doctor(cfg)}
    st = ins.State(); st.d["mcp"] = {"name": "ctxswitch"}; st.save()
    for ok, status, detail in ((True, "✓", "ctxswitch -> "), (False, "!", "ctxswitch not registered"), (None, "!", "claude not on PATH")):
        answer[0] = (ok, "")
        (c,) = [c for c in ins.doctor(cfg) if c.name == "mcp"]
        assert c.status == status and c.detail.startswith(detail)


def test_a_failed_mcp_add_records_nothing(home, monkeypatch):
    monkeypatch.setattr(ins.shutil, "which", lambda c: "/usr/bin/" + c)
    fake = FakeClaudeMcp()
    real = fake.__call__
    def refuse(argv, **kw):
        r = real(argv, **kw)
        if argv[2] == "add":
            r.returncode, r.stderr = 1, "Invalid scope"
        return r
    (line,) = _run(ins.install_mcp, run=refuse)
    assert line == "mcp: claude mcp add failed: Invalid scope" and "mcp" not in ins.State().d


def test_skill_is_linked_and_only_our_link_removed(home):
    link = ins.skill_link()
    assert link == home / ".claude" / "skills" / "ctxswitch" and (ins.skill_source() / "SKILL.md").is_file()
    assert _run(ins.install_skill, dry=True)[0].startswith("skill: would link") and not link.is_symlink()
    _run(ins.install_skill)
    assert link.resolve() == ins.skill_source().resolve()
    assert "already linked" in _run(ins.install_skill)[0]
    _run(ins.uninstall_skill)
    assert not link.is_symlink()
    link.mkdir(parents=True)                                          # the user's own skill by that name
    assert "not our link" in _run(ins.install_skill)[0]
    assert "no link of ours" in _run(ins.uninstall_skill)[0] and link.is_dir()
    link.rmdir(); link.symlink_to(home)                               # someone else's link
    assert "no link of ours" in _run(ins.uninstall_skill)[0] and link.is_symlink()


def test_pi_mcp_entry_is_added_beside_other_servers_and_removed_alone(home, monkeypatch):
    agent = home / ".pi" / "agent"
    assert "not installed" in _run(ins.install_pi_mcp)[0] and not (agent / "mcp.json").exists()   # no adapter: nothing
    agent.mkdir(parents=True)
    (agent / "settings.json").write_text(json.dumps({"packages": ["npm:pi-mcp-adapter"]}))
    _run(ins.install_pi_mcp)                                          # adapter but no file yet: created, 0600
    p = agent / "mcp.json"
    assert json.loads(p.read_text()) == {"mcpServers": {"ctxswitch": ins.pi_mcp_entry()}} and oct(p.stat().st_mode & 0o777) == "0o600"
    other = {"mcpServers": {"brave": {"command": "npx", "env": {"KEY": "s3cret"}}}, "settings": {"x": 1}}
    p.write_text(json.dumps(other)); os.chmod(p, 0o640)
    lines = _run(ins.install_pi_mcp)
    assert not any("s3cret" in ln for ln in lines)                    # other servers' keys are never printed
    d = json.loads(p.read_text())
    assert d["mcpServers"]["brave"] == other["mcpServers"]["brave"] and d["settings"] == {"x": 1}
    assert d["mcpServers"]["ctxswitch"] == {"command": str(ins.ctx_exe()), "args": ["mcp"]}
    assert oct(p.stat().st_mode & 0o777) == "0o640"                   # its mode kept
    backup = __import__("pathlib").Path(ins.State().d["pi_mcp"]["backup"])
    assert json.loads(backup.read_text()) == other and oct(backup.stat().st_mode & 0o777) == "0o600"
    before = p.read_bytes()
    assert "already" in _run(ins.install_pi_mcp)[0] and p.read_bytes() == before
    _run(ins.uninstall_pi_mcp)
    assert json.loads(p.read_text()) == other and "pi_mcp" not in ins.State().d
    assert "not in" in _run(ins.uninstall_pi_mcp)[0]
    p.write_text(json.dumps({"mcpServers": {"ctxswitch": {"command": "/old/venv/bin/ctx", "args": ["mcp"]}}}))
    assert "added" in _run(ins.install_pi_mcp)[0]                    # another venv's ctx: re-pointed
    assert json.loads(p.read_text())["mcpServers"]["ctxswitch"] == ins.pi_mcp_entry()
    for bad in ("[1, 2]", '{"mcpServers": []}', "{not json"):
        p.write_text(bad)
        assert "left alone" in _run(ins.install_pi_mcp)[0] and p.read_text() == bad


def test_doctor_checks_pi_mcp_once_installed(home, monkeypatch):
    monkeypatch.setattr(ins, "_systemctl", lambda *a: None)
    monkeypatch.setattr(ins.shutil, "which", lambda c: None)
    from ctxswitch.common.config import Config
    cfg = Config(); cfg.hub.url = "http://127.0.0.1:9"
    assert "pi mcp" not in {c.name for c in ins.doctor(cfg)}
    (home / ".pi" / "agent").mkdir(parents=True); (home / ".pi/agent/mcp.json").write_text("{}")
    _run(ins.install_pi_mcp)
    assert [c.status for c in ins.doctor(cfg) if c.name == "pi mcp"] == ["✓"]
    (home / ".pi/agent/mcp.json").write_text(json.dumps({"mcpServers": {"ctxswitch": {"command": "/old/ctx", "args": ["mcp"]}}}))
    assert [c.status for c in ins.doctor(cfg) if c.name == "pi mcp"] == ["!"]


# -- herdr app: the ctx-board mod, ctx.json and keys.json -------------------------------------------------------
MOD_FILES = ("main.js", "view.js", "mod.json", "README.md")


def _herdr_src(home, monkeypatch, keys_example=None):
    """A fake repo mod at <tmp>/repo/herdr/mods/ctx-board (the locator pointed at it, no herdr-app on this host)."""
    sdir = home / "repo" / "herdr" / "mods" / "ctx-board"
    (sdir / "test" / "fixtures").mkdir(parents=True)
    for name in MOD_FILES:
        (sdir / name).write_text(f"// {name}\n")
    (sdir / "package.json").write_text('{ "type": "module" }\n')
    (sdir / "test" / "view.test.mjs").write_text("// a test\n")
    (sdir / "test" / "fixtures" / "board.json").write_text("{}\n")
    if keys_example:
        (sdir / "keys.example.json").write_text(json.dumps({"mod:ctx-board": keys_example, "core": {"quit": "ctrl+q"}}))
    monkeypatch.setattr(ins, "herdr_mod_src", lambda: sdir)
    return sdir


def _herdr_cfg():
    from ctxswitch.common.config import Config
    cfg = Config()
    cfg.hub.url, cfg.hub.token = "http://laptop:7433/v1", "t0k"
    cfg.collector.host_id = "laptop"
    cfg.ssh = {"devbox": "me@devbox.lan", "gpu-host": "gpu-host", "blocked": ""}
    return cfg


def _herdr_install(cfg, home, **kw):
    lines = []
    ins.install_herdr(cfg, ins.herdr_home(), kw.pop("dry", False), lines.append, **kw)
    return lines


def test_herdr_home_defaults_to_env_or_xdg(home, monkeypatch):
    assert ins.herdr_home() == home / ".config" / "herdr-app"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    assert ins.herdr_home() == home / "xdg" / "herdr-app"
    monkeypatch.setenv("HERDR_APP_HOME", str(home / "elsewhere"))
    assert ins.herdr_home() == home / "elsewhere"
    assert ins.herdr_home(str(home / "given")) == home / "given"          # --herdr-home wins


def test_install_herdr_copies_the_mod_and_writes_ctx_json(home, monkeypatch):
    sdir = _herdr_src(home, monkeypatch)
    cfg = _herdr_cfg()
    lines = _herdr_install(cfg, home)
    dest = home / ".config/herdr-app/mods/ctx-board"
    assert f"herdr: mod {ins.MOD_NAME} copied ({len(MOD_FILES)} files) -> {dest}" == lines[0]
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()) == sorted(MOD_FILES)
    assert not (dest / "package.json").exists() and not (dest / "test").exists()   # npm/test scaffolding stays in the repo
    p = ins.herdr_ctx_json()
    assert json.loads(p.read_text()) == {"hub_url": cfg.hub.url, "token": "t0k", "host_id": "laptop",
                                         "machines": {"devbox": "devbox", "gpu-host": "gpu-host", "laptop": None}}
    assert oct(p.stat().st_mode & 0o777) == "0o600"                     # it holds the hub token
    assert "herdr: hc machine add --name devbox --via 'ssh me@devbox.lan'" in lines   # [ssh] hosts become machines
    assert "herdr: hc machine add --name gpu-host --via 'ssh gpu-host'" in lines
    assert not any("blocked" in ln for ln in lines)                     # "" = no route, so no machine
    assert any("`hc server start`" in ln for ln in lines)               # the core starts from a clean login shell
    assert any("herdr-app not found" in ln for ln in lines)             # nothing could check the mod
    before = p.read_bytes()
    lines = _herdr_install(cfg, home)
    assert lines[0].endswith(f"unchanged ({len(MOD_FILES)} files in {dest})") and "already points at" in lines[1]
    assert p.read_bytes() == before


def test_install_herdr_replaces_only_files_that_differ(home, monkeypatch):
    sdir = _herdr_src(home, monkeypatch)
    _herdr_install(_herdr_cfg(), home)
    dest = ins.herdr_mod_dir()
    (dest / "main.js").write_text("// edited by hand\n")
    kept = (dest / "view.js").read_bytes()
    lines = _herdr_install(_herdr_cfg(), home)
    assert lines[0] == f"herdr: mod {ins.MOD_NAME} copied 1 of {len(MOD_FILES)} files -> {dest}"
    assert (dest / "main.js").read_text() == "// main.js\n" and (dest / "view.js").read_bytes() == kept


def test_install_herdr_keeps_an_edited_machines_map(home, monkeypatch):
    sdir = _herdr_src(home, monkeypatch)
    cfg = _herdr_cfg()
    _herdr_install(cfg, home)
    p = ins.herdr_ctx_json()
    d = json.loads(p.read_text()); d["machines"] = {"laptop": "mine", "extra": "elsewhere"}; p.write_text(json.dumps(d))
    lines = _herdr_install(cfg, home)                                   # same hub, same host id: not rewritten at all
    assert "already points at" in lines[1] and json.loads(p.read_text())["machines"] == d["machines"]
    cfg.hub.url = "http://other:7433/v1"                                # a hub moved: refresh, keeping their map
    lines = _herdr_install(cfg, home)
    assert "updated" in lines[1]
    d = json.loads(p.read_text())
    assert d["hub_url"] == "http://other:7433/v1" and d["token"] == "t0k"
    assert d["machines"] == {"devbox": "devbox", "gpu-host": "gpu-host", "laptop": "mine", "extra": "elsewhere"}
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    for bad in ("[1, 2]", "{not json"):
        p.write_text(bad)
        assert "left alone" in _herdr_install(cfg, home)[1] and p.read_text() == bad


def test_install_herdr_merges_keys_json_once_and_keeps_other_sections(home, monkeypatch):
    ours = {"board.select": "ctrl+alt+b", "board.refresh": "ctrl+alt+r"}
    sdir = _herdr_src(home, monkeypatch, keys_example=ours)
    cfg = _herdr_cfg()
    p = ins.herdr_keys_json()
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"core": {"quit": "ctrl+q"}, "app": {"refresh": "ctrl+alt+r"}}))   # one held elsewhere
    lines = _herdr_install(cfg, home)
    d = json.loads(p.read_text())
    assert d["mod:ctx-board"] == {"board.select": "ctrl+alt+b"}         # the bound-elsewhere one isn't duplicated
    assert d["core"] == {"quit": "ctrl+q"} and d["app"] == {"refresh": "ctrl+alt+r"}   # the operator's, as they were
    assert "added 1 binding" in lines[2] and "1 held by another mod" in lines[2]
    assert p.with_name("keys.json.bak").read_text() == json.dumps({"core": {"quit": "ctrl+q"}, "app": {"refresh": "ctrl+alt+r"}})
    before = p.read_bytes()
    assert "unchanged" in _herdr_install(cfg, home)[2] and p.read_bytes() == before    # idempotent, no second backup
    (sdir / "keys.example.json").write_text(json.dumps({"mod:ctx-board": ours | {"board.open": "ctrl+alt+o"}}))
    assert "added 1 binding" in _herdr_install(cfg, home)[2]
    assert json.loads(p.read_text())["mod:ctx-board"] == {"board.select": "ctrl+alt+b", "board.open": "ctrl+alt+o"}
    assert p.with_name("keys.json.bak").read_text() == json.dumps({"core": {"quit": "ctrl+q"}, "app": {"refresh": "ctrl+alt+r"}})
    p.with_name("keys.json.bak").unlink()
    p.unlink()
    assert "created" in _herdr_install(cfg, home)[2]                    # none yet: created with our section
    assert json.loads(p.read_text())["mod:ctx-board"] == ours | {"board.open": "ctrl+alt+o"}   # nothing holds any of them now
    assert not p.with_name("keys.json.bak").exists()                    # nothing to back up of a file that wasn't there


def test_install_herdr_no_keys_example_and_no_mod_source(home, monkeypatch):
    sdir = _herdr_src(home, monkeypatch)
    cfg = _herdr_cfg()
    lines = _herdr_install(cfg, home)
    assert not ins.herdr_keys_json().exists() and "left uncreated" in lines[2]      # no keys.example.json: no file made
    import shutil as sh; sh.rmtree(sdir)                                            # an installed wheel has no mod
    lines = _herdr_install(cfg, home)
    assert lines[0].startswith("herdr: the mod isn't in this install") and str(sdir) in lines[0]
    assert "copy herdr/mods/ctx-board into" in lines[0]                             # and how to do it by hand
    assert not any("herdr-app check" in ln for ln in lines)                         # nothing to check, nothing checked
    assert ins.herdr_ctx_json().exists()                                            # ctx.json still written all the same


def test_install_herdr_checks_the_mod_with_the_app(home, monkeypatch):
    _herdr_src(home, monkeypatch)
    cfg = _herdr_cfg()
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw.get("timeout")))
        return type("R", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()
    monkeypatch.setattr(ins, "hc_app_exe", lambda which=None: "/usr/local/bin/herdr-app")
    lines = _herdr_install(cfg, home, run=run)
    assert calls == [(["/usr/local/bin/herdr-app", "check", str(ins.herdr_mod_dir())], 60)]
    assert f"herdr-app check passed for {ins.herdr_mod_dir()}" in lines[-1]

    def fail(argv, **kw):
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "ctx-board main.js: syntax error\n"})()
    lines = _herdr_install(cfg, home, run=fail)
    assert lines[-1] == f"herdr: herdr-app check failed: {ins.MOD_NAME} main.js: syntax error"
    assert "check passed" not in _herdr_install(cfg, home, dry=True, run=fail)[-1]      # dry run checks nothing


def test_install_herdr_is_opt_in(home, monkeypatch, capsys):
    _herdr_src(home, monkeypatch)
    seen = []
    for name in ("write_config", "install_collector", "install_claude", "install_pi", "install_herdr"):
        monkeypatch.setattr(ins, name, lambda *a, _n=name, **k: seen.append(_n))
    args = dict(claude=False, pi=False, collector=False, mcp=False, herdr=False, herdr_home=None, dry_run=False,
                hub=None, token=None, no_start=True, force=False)
    ins.cmd_install(type("A", (), args)())
    assert seen == ["write_config", "install_collector", "install_claude", "install_pi"]     # --herdr isn't part of it
    seen.clear()
    ins.cmd_install(type("A", (), args | {"herdr": True})())
    assert seen == ["install_herdr"]                                          # --herdr alone: only that
    ins.cmd_uninstall(type("A", (), args | {"herdr": True})())
    assert "the app's to manage" in capsys.readouterr().out                   # nothing of ours is removed


def test_install_herdr_dry_run_writes_nothing(home, monkeypatch):
    _herdr_src(home, monkeypatch, keys_example={"board.select": "ctrl+alt+b"})
    lines = _herdr_install(_herdr_cfg(), home, dry=True)
    assert any("would copy" in ln for ln in lines) and any("would write" in ln for ln in lines)
    assert any("would create" in ln for ln in lines)
    assert not ins.herdr_mod_dir().exists() and not ins.herdr_ctx_json().exists() and not ins.herdr_keys_json().exists()


def test_doctor_herdr_line(home, monkeypatch):
    from ctxswitch.common.config import Config
    monkeypatch.setattr(ins, "_systemctl", lambda *a: None)
    monkeypatch.setattr(ins.shutil, "which", lambda c: None)
    monkeypatch.setattr(ins, "hc_bin", lambda which=None: None)
    monkeypatch.setattr(ins, "hc_app_exe", lambda which=None: None)
    cfg = Config(); cfg.hub.url = "http://127.0.0.1:9"
    c = next(c for c in ins.doctor(cfg) if c.name == "herdr")
    assert c.status == "!" and c.detail == "hc missing, app missing, mod missing, ctx.json missing"
    assert c.fix == "ctx install --herdr"
    monkeypatch.setattr(ins, "hc_bin", lambda which=None: "/usr/local/bin/hc")
    monkeypatch.setattr(ins, "hc_core_ok", lambda which=None, run=None: True)
    monkeypatch.setattr(ins, "hc_app_exe", lambda which=None: "/usr/local/bin/herdr-app")
    _herdr_src(home, monkeypatch)
    _herdr_install(cfg, home)
    c = next(c for c in ins.doctor(cfg) if c.name == "herdr")
    assert c.status == "✓" and c.detail == f"core ok, app ok, mod ok, ctx.json -> {cfg.hub.url}"
    (ins.herdr_mod_dir() / "main.js").write_text("// edited by hand\n")
    assert "mod stale" in next(c for c in ins.doctor(cfg) if c.name == "herdr").detail
    ins.herdr_ctx_json().unlink()
    c = next(c for c in ins.doctor(cfg) if c.name == "herdr")
    assert c.status == "!" and c.detail.endswith("ctx.json missing")


def test_hc_core_ok_talks_to_the_socket_never_the_cli(home, tmp_path, monkeypatch):
    """`hc server status` would start a core with this process's environment; the check only ever opens the socket."""
    import json
    import socket
    import threading
    monkeypatch.setattr(ins.subprocess, "run", lambda *a, **k: pytest.fail("hc_core_ok ran a subprocess"))
    sock = tmp_path / "core.sock"
    assert not ins.hc_core_ok(sock=str(sock))                       # no socket file: no core, and nothing started
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); srv.bind(str(sock)); srv.listen(1)

    def answer():
        c, _ = srv.accept()
        with c, c.makefile("rb") as f:
            req = json.loads(f.readline())
            c.sendall((json.dumps({"jsonrpc": "2.0", "id": req.get("id"), "result": {"machines": []}}) + "\n").encode())
    t = threading.Thread(target=answer, daemon=True); t.start()
    assert ins.hc_core_ok(sock=str(sock))
    t.join(3); srv.close()
    assert not ins.hc_core_ok(sock=str(tmp_path / "dead.sock"))
