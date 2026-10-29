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
