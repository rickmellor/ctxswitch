"""ctx install / uninstall against a throwaway HOME: merge, idempotence, byte-for-byte restore, surgical removal."""
import json
import os
from pathlib import Path

import pytest

from ctxswitch import hook
from ctxswitch.client import install as ins

SAINT = {"matcher": "startup|resume", "hooks": [{"type": "command", "command": "/x/saint-pool --hook", "timeout": 5}]}
ORIGINAL = '{\n    "model": "opus",\n    "hooks": {"SessionStart": [' + json.dumps(SAINT) + ']}\n}'   # odd indent on purpose


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for v in ("CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR", "AGY_CONFIG_DIR", "XDG_CONFIG_HOME",
              "CTXSWITCH_SPOOL", "CTXSWITCH_CONFIG", "CTXSWITCH_UDEV_RULES"):
        monkeypatch.delenv(v, raising=False)
    # the deck's checks read a system file; here, as everywhere else in this file, the system is this tmp home
    monkeypatch.setenv("CTXSWITCH_UDEV_RULES", str(tmp_path / "etc/udev/rules.d/70-streamdeck.rules"))
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
        assert len(ours) == (2 if ev in ("SessionStart", "UserPromptSubmit") else 1) \
            and ours[0]["hooks"][0]["async"] is True                      # +1 where a sync group rides along
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
    for name in ("write_config", "install_collector", "install_claude", "install_pi", "install_agy_hooks",
                 "install_mcp", "install_skill", "install_pi_mcp", "install_agy_mcp", "install_agy_skill"):
        monkeypatch.setattr(ins, name, lambda *a, _n=name, **k: seen.append(_n))
    args = dict(claude=False, pi=False, collector=False, mcp=False, dry_run=False, hub=None, token=None, no_start=True, force=False)
    ins.cmd_install(type("A", (), args)())
    assert seen == ["write_config", "install_collector", "install_claude", "install_pi", "install_agy_hooks"]
    seen.clear()
    ins.cmd_install(type("A", (), args | {"mcp": True})())
    assert seen == ["install_mcp", "install_skill", "install_pi_mcp", "install_agy_mcp", "install_agy_skill"]   # --mcp alone: only that


def _deck_installed(log=None):
    """The unit written, without any (re)starting: what `ctx install --deck --no-start` leaves behind, and all the
    doctor's checks need."""
    ins.install_deck(False, False, log or (lambda m: None))


def _deck(home) -> Path:
    """The deck unit where the throwaway HOME keeps its units, whether or not anything has written it."""
    return home / ".config/systemd/user/ctx-deck.service"


def test_deck_unit_points_at_this_venv_and_waits_for_the_board(home, monkeypatch):
    """The service exists so that nobody has to start `ctx deck` by hand and nothing has to notice an unplug: it
    restarts on its own, and the deck waits for a device rather than exiting without one."""
    calls = []
    monkeypatch.setattr(ins, "_systemctl", lambda *a: calls.append(a) or None)
    lines = []
    ins.install_deck(False, True, lines.append)
    unit = _deck(home).read_text()
    assert f"ExecStart={ins.ctx_exe()} deck\n" in unit, unit
    assert "Restart=always" in unit and "RestartSec=5" in unit
    assert "append:%h/.local/state/ctxswitch/deck.log" in unit, "the unit doesn't say where its output goes"
    assert "After=ctx-collector.service" in unit and "WantedBy=default.target" in unit
    assert "Description=ctxswitch Stream Deck" in unit
    assert "written by `ctx install --deck`" in unit
    # no systemd here, which is what the sandbox and a plain terminal both look like: says what to run instead
    assert calls == [("daemon-reload",)] and "run: systemctl" in lines[-1], lines


def test_deck_unit_is_written_once_and_removed_whole(home, monkeypatch):
    calls = []
    monkeypatch.setattr(ins, "_systemctl", lambda *a: calls.append(a) or None)
    said: list[str] = []
    ins.install_deck(True, True, said.append)
    assert "would write" in said[-1] and not _deck(home).exists(), said
    ins.install_deck(False, True, said.append)
    assert _deck(home).is_file() and any("wrote" in l for l in said), said
    written, body = _deck(home).stat().st_mtime_ns, _deck(home).read_text()
    ins.install_deck(False, True, said.append)                 # the same unit again: written once, not rewritten
    assert (_deck(home).stat().st_mtime_ns, _deck(home).read_text()) == (written, body), said
    assert sum(1 for l in said if "wrote" in l) == 1, said
    ins.uninstall_deck(True, said.append)
    assert _deck(home).is_file() and "would stop" in said[-1], said
    ins.uninstall_deck(False, said.append)
    assert not _deck(home).exists() and "stopped and removed" in said[-1], said
    assert ("disable", "--now", ins.DECK_UNIT) in calls and ("daemon-reload",) in calls, calls
    said.clear()
    ins.uninstall_deck(False, said.append)                      # already gone: says so, changes nothing
    assert "no unit installed" in said[-1], said


def test_deck_extra_missing_writes_no_unit(home, monkeypatch):
    """A unit for a deck that can't import its library is a service that exits 2 on every restart, so the unit waits
    and the command that would fix it is what gets printed."""
    monkeypatch.setattr(ins, "deck_extra_missing", lambda: "StreamDeck")
    lines: list[str] = []
    ins.install_deck(False, True, lines.append)
    assert not _deck(home).exists(), "the unit was written without the extra installed"
    assert "uv pip install -e '.[deck]'" in " ".join(lines), lines
    assert "StreamDeck" in lines[0], lines


def test_install_and_uninstall_deck_are_opt_in(home, monkeypatch):
    """`--deck` names one wiring and nothing else: no hooks touched, no collector unit, and the same said back."""
    calls = []
    monkeypatch.setattr(ins, "_systemctl", lambda *a: calls.append(a) or None)
    args = dict(claude=False, pi=False, collector=False, mcp=False, deck=True, dry_run=False, force=False)
    ins.cmd_install(type("A", (), dict(no_start=False, hub=None, token=None, **args))())
    assert _deck(home).is_file() and not (home / ".claude" / "settings.json").exists()
    assert not (home / ".config/systemd/user/ctx-collector.service").exists()
    ins.cmd_uninstall(type("A", (), dict(**args))())
    assert not _deck(home).exists()
    ins.cmd_install(type("A", (), dict(no_start=False, hub=None, token=None, **dict(args, deck=False)))())
    assert not _deck(home).exists(), "a plain `ctx install` installed a deck service nobody asked for"
    seen = []
    for name in ("write_config", "install_collector", "install_deck", "install_claude", "install_pi",
                 "install_agy_hooks", "uninstall_deck", "uninstall_claude", "uninstall_pi", "uninstall_agy_hooks"):
        monkeypatch.setattr(ins, name, lambda *a, _n=name, **k: seen.append(_n))
    ins.cmd_install(type("A", (), dict(no_start=False, hub=None, token=None, **args))())
    assert seen == ["install_deck"], f"`--deck` reached past the deck service: {seen}"
    seen.clear()
    ins.cmd_uninstall(type("A", (), dict(**args))())
    assert seen == ["uninstall_deck"], f"`ctx uninstall --deck` took the hooks back too: {seen}"


def test_doctor_checks_the_deck_only_once_its_unit_is_installed(home, monkeypatch):
    monkeypatch.setattr(ins, "_systemctl", lambda *a: None)
    monkeypatch.setattr(ins.shutil, "which", lambda c: None)
    monkeypatch.setattr(ins, "deck_extra_missing", lambda: None)
    from ctxswitch.common.config import Config
    cfg = Config(); cfg.hub.url = "http://127.0.0.1:9"
    assert not [c for c in ins.doctor(cfg) if c.name.startswith("deck")], \
        "doctor checks a deck on a host that was never asked to run one"
    _deck_installed()
    (c,) = [c for c in ins.doctor(cfg) if c.name == "deck"]
    assert c.status == "!", c                                             # no systemd --user here: a warning, not a ✗
    rules = ins.udev_rules_path()
    assert rules == home / "etc/udev/rules.d/70-streamdeck.rules"           # the tmp home, not this machine's /etc
    bad = [c for c in ins.doctor(cfg) if c.name == "deck udev"]
    assert len(bad) == 1 and bad[0].status == "✗", bad
    assert 'SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", MODE="0666"' in bad[0].fix, bad[0].fix
    assert 'KERNEL=="hidraw*", ATTRS{idVendor}=="0fd9", MODE="0666"' in bad[0].fix, bad[0].fix
    assert "udevadm control --reload-rules && sudo udevadm trigger" in bad[0].fix, bad[0].fix
    rules.parent.mkdir(parents=True, exist_ok=True)
    rules.write_text(ins.UDEV_RULE_BODY + "# and anyone else's\n")
    assert [c.status for c in ins.doctor(cfg) if c.name == "deck udev"] == ["✓"]
    rules.write_text('KERNEL=="hidraw*", ATTRS{idVendor}=="0fd9", MODE="0666"\n')   # one of the two lines
    (c,) = [c for c in ins.doctor(cfg) if c.name == "deck udev"]
    assert c.status == "✗" and "SUBSYSTEM" in c.detail, c


def test_doctor_says_what_a_replug_costs_under_wsl(home, monkeypatch):
    monkeypatch.setattr(ins, "_systemctl", lambda *a: None)
    monkeypatch.setattr(ins.shutil, "which", lambda c: None)
    monkeypatch.setattr("ctxswitch.common.wsl.is_wsl", lambda: True)
    from ctxswitch.common.config import Config
    cfg = Config(); cfg.hub.url = "http://127.0.0.1:9"
    assert not [c for c in ins.doctor(cfg) if c.name.startswith("deck")]
    _deck_installed()
    (c,) = [c for c in ins.doctor(cfg) if c.name == "deck (wsl)"]
    assert c.status == "!" and "usbipd attach --wsl --busid <id>" in c.detail, c
    assert "--auto-attach" in c.detail and "picks it up" in c.detail, c


def test_doctor_says_when_the_deck_service_is_inactive(home, monkeypatch):
    class Out:
        def __init__(self, out): self.stdout, self.stderr = out, ""
    monkeypatch.setattr(ins, "_systemctl", lambda *a: Out("active" if a[0] == "is-active" else ""))
    monkeypatch.setattr(ins.shutil, "which", lambda c: None)
    monkeypatch.setattr(ins, "deck_extra_missing", lambda: None)
    from ctxswitch.common.config import Config
    cfg = Config(); cfg.hub.url = "http://127.0.0.1:9"
    _deck_installed()
    assert [c.status for c in ins.doctor(cfg) if c.name == "deck"] == ["✓"]
    monkeypatch.setattr(ins, "_systemctl", lambda *a: Out("failed"))
    (c,) = [c for c in ins.doctor(cfg) if c.name == "deck"]
    assert c.status == "✗" and "ctx-deck.service failed" in c.detail and "deck.log" in c.fix, c


def test_uninstall_without_flags_takes_the_agy_hooks_back_too(home, monkeypatch):
    seen = []
    for name in ("uninstall_claude", "uninstall_pi", "uninstall_collector", "uninstall_agy_hooks", "uninstall_mcp"):
        monkeypatch.setattr(ins, name, lambda *a, _n=name, **k: seen.append(_n))
    ins.cmd_uninstall(type("A", (), dict(claude=True, pi=True, collector=False, mcp=False, dry_run=False, force=False))())
    assert seen == ["uninstall_claude", "uninstall_pi", "uninstall_agy_hooks"]          # the unit only when asked


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


def test_agy_mcp_entry_is_added_beside_other_servers_and_removed_alone(home):
    cfg_dir = home / ".gemini" / "config"
    assert ins.agy_mcp_json() == cfg_dir / "mcp_config.json" and ins.agy_skill_link() == cfg_dir / "skills" / "ctxswitch"
    assert "not found" in _run(ins.install_agy_mcp)[0] and not cfg_dir.exists()      # agy never run here: nothing
    cfg_dir.mkdir(parents=True)
    p = ins.agy_mcp_json()
    p.write_bytes(b""); os.chmod(p, 0o644)                                            # agy's own 0-byte default file
    _run(ins.install_agy_mcp)
    assert json.loads(p.read_text()) == {"mcpServers": {"ctxswitch": ins.mcp_entry()}}
    assert oct(p.stat().st_mode & 0o777) == "0o644"                                   # the file's mode kept
    backup = Path(ins.State().d["agy_mcp"]["backup"])
    assert ins.State().d["agy_mcp"]["file"] == str(p) and backup.read_bytes() == b"" and \
        oct(backup.stat().st_mode & 0o777) == "0o600"
    p.unlink(); _run(ins.install_agy_mcp)
    assert oct(p.stat().st_mode & 0o777) == "0o600"                                   # written fresh: not world-readable
    other = {"mcpServers": {"github": {"command": "npx", "env": {"TOKEN": "s3cret"}}}, "settings": {"x": 1}}
    p.write_text(json.dumps(other)); os.chmod(p, 0o640)
    lines = _run(ins.install_agy_mcp)
    assert not any("s3cret" in ln for ln in lines)                                    # other servers' keys are never printed
    d = json.loads(p.read_text())
    assert d["mcpServers"]["github"] == other["mcpServers"]["github"] and d["settings"] == {"x": 1}
    assert d["mcpServers"]["ctxswitch"] == {"command": str(ins.ctx_exe()), "args": ["mcp"]}
    assert oct(p.stat().st_mode & 0o777) == "0o640"                                   # its mode kept
    backup = Path(ins.State().d["agy_mcp"]["backup"])
    assert json.loads(backup.read_text()) == other and oct(backup.stat().st_mode & 0o777) == "0o600"
    before = p.read_bytes()
    assert "already" in _run(ins.install_agy_mcp)[0] and p.read_bytes() == before
    _run(ins.uninstall_agy_mcp)
    assert json.loads(p.read_text()) == other and "agy_mcp" not in ins.State().d
    assert "not in" in _run(ins.uninstall_agy_mcp)[0]
    p.write_text(json.dumps({"mcpServers": {"ctxswitch": {"command": "/old/venv/bin/ctx", "args": ["mcp"]}}}))
    assert "added" in _run(ins.install_agy_mcp)[0]                                    # another venv's ctx: re-pointed
    assert json.loads(p.read_text())["mcpServers"]["ctxswitch"] == ins.mcp_entry()
    for bad in ("[1, 2]", '{"mcpServers": []}', "{not json"):
        p.write_text(bad)
        assert "left alone" in _run(ins.install_agy_mcp)[0] and p.read_text() == bad


def test_doctor_checks_agy_mcp_once_installed(home, monkeypatch):
    monkeypatch.setattr(ins, "_systemctl", lambda *a: None)
    monkeypatch.setattr(ins.shutil, "which", lambda c: None)
    from ctxswitch.common.config import Config
    cfg = Config(); cfg.hub.url = "http://127.0.0.1:9"
    assert "agy mcp" not in {c.name for c in ins.doctor(cfg)}
    (home / ".gemini" / "config").mkdir(parents=True)
    _run(ins.install_agy_mcp)
    assert [c.status for c in ins.doctor(cfg) if c.name == "agy mcp"] == ["✓"]
    ins.agy_mcp_json().write_text(json.dumps({"mcpServers": {"ctxswitch": {"command": "/old/ctx", "args": ["mcp"]}}}))
    (c,) = [c for c in ins.doctor(cfg) if c.name == "agy mcp"]
    assert c.status == "!" and c.fix == "ctx install --mcp"


def test_agy_skill_is_linked_and_only_our_link_removed(home):
    link = ins.agy_skill_link()
    assert "not found" in _run(ins.install_agy_skill)[0] and not link.exists()
    (home / ".gemini" / "config").mkdir(parents=True)
    assert _run(ins.install_agy_skill, dry=True)[0].startswith("agy skill: would link") and not link.is_symlink()
    _run(ins.install_agy_skill)
    assert link.resolve() == ins.skill_source().resolve()
    assert "already linked" in _run(ins.install_agy_skill)[0]
    _run(ins.uninstall_agy_skill)
    assert not link.is_symlink()
    link.mkdir(parents=True)                                                          # the user's own skill by that name
    assert "not our link" in _run(ins.install_agy_skill)[0]
    assert "no link of ours" in _run(ins.uninstall_agy_skill)[0] and link.is_dir()


# -- Antigravity's global hooks.json ----------------------------------------------------------------------------
# agy's format (measured): each NAMED hook is a top-level key; flat events hold handlers, tool events matcher groups.
# saint-pool's real entry is a flat SessionStart; "other-plugin" stands in for anyone else's hook.
SAINT_HOOKS = {"saint-pool": {"SessionStart": [{"type": "command", "command": "/x/saint-pool --hook", "timeout": 5}]}}
OTHER = {"PostToolUse": [{"matcher": "run_command", "hooks": [{"type": "command", "command": "/x/lint.sh"}]}]}
THEIRS = {**SAINT_HOOKS, "other-plugin": OTHER}


def _gemini(home, body=None):
    """~/.gemini/config with a hooks.json holding ``body`` (absent when None)."""
    (home / ".gemini" / "config").mkdir(parents=True, exist_ok=True)
    p = ins.agy_hooks_json()
    if body is not None:
        p.write_text(json.dumps(body))
    return p


def _entry(home):
    return json.loads(ins.agy_hooks_json().read_text())[ins.AGY_HOOK_NAME]


def _cmds(entry, ev):
    return [h["command"] for g in entry[ev] for h in (g["hooks"] if isinstance(g, dict) and "hooks" in g else [g])]


def test_agy_hooks_need_agy_to_have_run_here(home):
    assert ins.agy_hooks_json() == home / ".gemini" / "config" / "hooks.json"
    assert "not found" in _run(ins.install_agy_hooks)[0] and not (home / ".gemini").exists()
    assert "missing, nothing to do" in _run(ins.uninstall_agy_hooks)[0]


def test_agy_hooks_add_one_named_hook_beside_everyone_else_s(home):
    p = _gemini(home, THEIRS)
    _run(ins.install_agy_hooks)
    d = json.loads(p.read_text())
    assert sorted(d) == ["ctxswitch", "other-plugin", "saint-pool"]                          # a top-level named hook
    assert d["saint-pool"] == SAINT_HOOKS["saint-pool"] and d["other-plugin"] == OTHER        # theirs untouched
    e = _entry(home)
    for ev in ins.AGY_FLAT:                                                                   # flat: handlers directly
        assert e[ev] == [{"type": "command", "command": f"{ins.hook_exe()} agy {ev}", "timeout": 5}]
    for ev in ins.AGY_MATCHED:                                                                # never "*": one tool only
        assert e[ev] == [{"matcher": ins.AGY_ASK_TOOL,
                          "hooks": [{"type": "command", "command": ins.agy_event_command(f"{ins.hook_exe()} agy", ev),
                                     "timeout": 5}]}]
    assert oct(p.stat().st_mode & 0o777) == "0o644"                                           # agy's own mode kept
    assert ins.State().d["agy_hooks"]["file"] == str(p)
    assert json.loads(Path(ins.State().d["agy_hooks"]["backup"]).read_text()) == THEIRS
    before = p.read_bytes()
    assert "already" in _run(ins.install_agy_hooks)[0] and p.read_bytes() == before           # idempotent


def test_agy_hooks_keep_everyone_else_s_groups_and_back_up_the_original(home):
    theirs = {"type": "command", "command": "/x/saint-pool --agy Stop", "timeout": 5}
    body = {"saint-pool": SAINT_HOOKS["saint-pool"],
            "ctxswitch": {"Stop": [theirs, {"type": "command", "command": "/old/venv/bin/ctx-hook agy Stop"}]}}
    p = _gemini(home, body)
    os.chmod(p, 0o640)
    _run(ins.install_agy_hooks)
    e = _entry(home)
    assert e["Stop"][0] == theirs                                                         # theirs, kept in front
    assert _cmds(e, "Stop") == ["/x/saint-pool --agy Stop", f"{ins.hook_exe()} agy Stop"]  # ours re-pointed, not doubled
    assert oct(p.stat().st_mode & 0o777) == "0o640"                                       # its mode kept
    assert oct(Path(ins.State().d["agy_hooks"]["backup"]).stat().st_mode & 0o777) == "0o600"
    assert json.loads(Path(ins.State().d["agy_hooks"]["backup"]).read_text()) == body
    before = p.read_bytes()
    assert "already" in _run(ins.install_agy_hooks)[0] and p.read_bytes() == before


def test_agy_hooks_a_moved_venv_repoints_without_duplicates(home, monkeypatch):
    _gemini(home)
    _run(ins.install_agy_hooks)
    monkeypatch.setattr(ins, "hook_exe", lambda: Path("/new/venv/bin/ctx-hook"))
    _run(ins.install_agy_hooks)
    e = _entry(home)
    assert _cmds(e, "PreInvocation") == [f"/new/venv/bin/ctx-hook agy PreInvocation"]
    assert len(e["PreInvocation"]) == 1 and len(e["PreToolUse"]) == 1


def test_agy_hooks_uninstall_removes_only_ours(home):
    p = _gemini(home, THEIRS)
    before = p.read_text()
    _run(ins.install_agy_hooks)
    assert "byte-for-byte" in _run(ins.uninstall_agy_hooks)[0]
    assert p.read_text() == before and "agy_hooks" not in ins.State().d
    _run(ins.install_agy_hooks)                                                           # now with an edit of theirs
    d = json.loads(p.read_text()); d["saint-pool"]["Stop"] = [{"type": "command", "command": "/x/y"}]
    p.write_text(json.dumps(d))
    assert "removed from" in _run(ins.uninstall_agy_hooks)[0]
    d = json.loads(p.read_text())
    assert sorted(d) == ["other-plugin", "saint-pool"] and d["saint-pool"]["Stop"] == [{"type": "command", "command": "/x/y"}]
    assert "not in" in _run(ins.uninstall_agy_hooks)[0]


def test_agy_hooks_uninstall_removes_a_file_we_created(home):
    p = _gemini(home)                                                                   # agy's dir, no hooks.json yet
    _run(ins.install_agy_hooks)
    assert p.exists() and ins.State().d["agy_hooks"]["backup"] is None
    assert oct(p.stat().st_mode & 0o777) == "0o600"                                         # written fresh: not readable-by-all
    assert "removed" in _run(ins.uninstall_agy_hooks)[0] and not p.exists()              # it didn't exist before
    p.write_text(json.dumps(SAINT_HOOKS))                                               # theirs alone
    _run(ins.install_agy_hooks)
    assert "ctxswitch" in json.loads(p.read_text())
    s = json.loads(p.read_text()); s["other-plugin"] = OTHER; p.write_text(json.dumps(s))   # agy changed it since
    _run(ins.uninstall_agy_hooks)
    assert json.loads(p.read_text()) == THEIRS                                         # ours gone, their edit kept


def test_agy_hooks_a_file_we_cant_read_is_left_alone(home):
    for bad in ("[1, 2]", "{not json", '"just a string"'):
        p = _gemini(home)
        p.write_text(bad)
        assert "isn't a JSON object" in _run(ins.install_agy_hooks)[0] and p.read_text() == bad
        assert "isn't a JSON object" in _run(ins.uninstall_agy_hooks)[0] and p.read_text() == bad


def test_agy_hooks_dry_run_changes_nothing(home):
    p = _gemini(home, SAINT_HOOKS)
    before = p.read_text()
    assert "would add" in _run(ins.install_agy_hooks, dry=True)[0]
    assert p.read_text() == before and not (home / ".local").exists()
    _run(ins.install_agy_hooks)
    assert "restore" in _run(ins.uninstall_agy_hooks, dry=True)[0] and p.read_text() != before


def test_doctor_checks_agy_hooks(home, monkeypatch):
    monkeypatch.setattr(ins, "_systemctl", lambda *a: None)
    monkeypatch.setattr(ins.shutil, "which", lambda c: None)
    from ctxswitch.common.config import Config
    cfg = Config(); cfg.hub.url = "http://127.0.0.1:9"
    assert "agy hooks" not in {c.name for c in ins.doctor(cfg)}                            # agy never ran here
    _gemini(home)
    assert [c.status for c in ins.doctor(cfg) if c.name == "agy hooks"] == ["!"]
    _run(ins.install_agy_hooks)
    assert [c.status for c in ins.doctor(cfg) if c.name == "agy hooks"] == ["✓"]
    d = json.loads(ins.agy_hooks_json().read_text())
    d[ins.AGY_HOOK_NAME]["Stop"] = []
    ins.agy_hooks_json().write_text(json.dumps(d))
    (c,) = [c for c in ins.doctor(cfg) if c.name == "agy hooks"]
    assert c.status == "!" and c.detail.endswith("missing for Stop") and c.fix == "ctx install"
    ins.agy_hooks_json().write_text("{not json")
    (c,) = [c for c in ins.doctor(cfg) if c.name == "agy hooks"]
    assert c.status == "!" and "not valid JSON" in c.detail
