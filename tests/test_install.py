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
    for v in ("CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR", "XDG_CONFIG_HOME", "CTXSWITCH_SPOOL", "CTXSWITCH_CONFIG"):
        monkeypatch.delenv(v, raising=False)
    (tmp_path / ".claude").mkdir()
    monkeypatch.setattr(ins, "windows_claude_root", lambda: None)               # not this laptop's real Windows home
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
    for name in ("write_config", "install_collector", "install_claude", "install_pi", "install_mcp", "install_skill"):
        monkeypatch.setattr(ins, name, lambda *a, _n=name, **k: seen.append(_n))
    args = dict(claude=False, pi=False, collector=False, mcp=False, dry_run=False, hub=None, token=None, no_start=True, force=False)
    ins.cmd_install(type("A", (), args)())
    assert seen == ["write_config", "install_collector", "install_claude", "install_pi"]
    seen.clear()
    ins.cmd_install(type("A", (), args | {"mcp": True})())
    assert seen == ["install_mcp", "install_skill"]                   # --mcp alone: only that


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
