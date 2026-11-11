"""The UserPromptSubmit prompt check: ctx-hook's block, the re-send that confirms it, failing open, and the install
group that runs it. The hub's matcher itself (POST /v1/prompt-check, the misplaced table) is another job's: here a
fake says "other" or doesn't answer at all."""
import io
import json
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

import pytest

from ctxswitch import hook
from ctxswitch.client import install as ins
from ctxswitch.collector.deliver import HEADER as OPERATOR_HEADER
from ctxswitch.collector.deliver import agent_header
from ctxswitch.common.config import Config
from ctxswitch.common.schema import session_key

PEER_HEADER = agent_header("devbox/claude/deadbeef", "pool switch", "claude")
PROMPT = "rename the hub's metrics endpoint to /v1/samples and update the board"
OTHER = {"verdict": "other", "id": 7, "target_key": "devbox/claude/aaaa1111", "target_title": "hub endpoint rename",
         "target_where": "devbox · ctxswitch@main", "why": "names the hub's metrics endpoint"}
REASON = (f'ctx: this looks like it\'s for "{OTHER["target_title"]}" ({OTHER["target_where"]}) — {OTHER["why"]}.\n'
          "Press ↑ then Enter within a minute to send it here anyway, or move it from the ctx board (t).")
BLOCK = json.dumps({"decision": "block", "reason": REASON}, ensure_ascii=False)


class Cfg(Config):
    """A Config that answers with a fixed host id, whatever this machine's hostname is."""
    host_id = "gpu-host"


@pytest.fixture
def conf(tmp_path, monkeypatch):
    """The re-send ledger points at a tmp file, and the clock is this object's ``t`` (advance it with ``.t +=``)."""
    p = tmp_path / "state" / "prompt-confirm.json"

    class Clock:
        t = 1000.0

    clock = Clock()
    monkeypatch.setattr(hook, "CONFIRM_FILE", p)
    monkeypatch.setattr(hook, "_now", lambda: clock.t)
    return type("Conf", (), {"path": p, "clock": clock, "resent": []})()


def _ev(prompt=PROMPT, sid="s1", **kw):
    return {"session_id": sid, "hook_event_name": "UserPromptSubmit", "prompt": prompt,
            "prompt_id": "p-1", "cwd": "/home/rick/repos/ctxswitch", "permission_mode": "default"} | kw


def _hash(prompt=PROMPT, sid="s1"):
    return hook._confirm_hash(session_key("gpu-host", "claude", sid), prompt)


def _ledger(path, h, ts, row_id=7):
    """The ledger as the hook itself left it after blocking this prompt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({h: {"ts": ts, "id": row_id}}))


def _told(conf):
    """Point the hook's "the row is resent" POST at a recorder instead of a hub."""
    def fake(cfg, row_id):
        conf.resent.append(row_id)

    return fake


def _silent(path):
    return not path.exists() or json.loads(path.read_text()) == {}


class FakeHub:
    """HubClient.prompt_check over a canned answer, raising ``raises`` instead when given, counting every call."""

    def __init__(self, reply=None, raises=None):
        self.reply, self.raises = reply, raises
        self.checks = []

    def prompt_check(self, key, prompt, prompt_id, cwd):
        self.checks.append((key, prompt, prompt_id, cwd))
        if self.raises:
            raise self.raises
        return self.reply


# -- the block path ---------------------------------------------------------------------------------------------
def test_block_path_records_the_hash_and_returns_the_reason(conf):
    hub = FakeHub(OTHER)
    assert hook.prompt_check(_ev(), cfg=Cfg(), client=hub, path=conf.path) == REASON
    assert hub.checks == [("gpu-host/claude/s1", PROMPT, "p-1", "/home/rick/repos/ctxswitch")]
    assert json.loads(conf.path.read_text()) == {_hash(): {"ts": conf.clock.t, "id": 7}}


def test_main_prints_exactly_the_block_json_and_nothing_else(conf, capsys, monkeypatch):
    hub = FakeHub(OTHER)
    monkeypatch.setattr("ctxswitch.common.config.load", lambda *a: Cfg())
    monkeypatch.setattr("ctxswitch.client.hubclient.HubClient", lambda cfg: hub)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_ev())))
    assert hook.main(["prompt-check"]) == 0
    assert capsys.readouterr().out == BLOCK + "\n"


# -- the re-send that confirms ----------------------------------------------------------------------------------
def test_resend_within_the_window_goes_through_and_says_resent(conf, monkeypatch):
    monkeypatch.setattr(hook, "_resend", _told(conf))
    _ledger(conf.path, _hash(), conf.clock.t)
    hub = FakeHub(OTHER)
    assert hook.prompt_check(_ev(), cfg=Cfg(), client=hub, path=conf.path) is None
    assert hub.checks == []                                              # not asked again: it's already decided
    assert conf.resent == [7]
    assert json.loads(conf.path.read_text()) == {}                       # once, not twice


def test_the_resent_post_reaches_the_hub(conf, monkeypatch):
    seen = []

    class Hub(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.headers.get("Authorization"),
                         json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Hub)
    Thread(target=srv.serve_forever, daemon=True).start()
    cfg = Cfg(); cfg.hub.url = f"http://127.0.0.1:{srv.server_port}/"; cfg.hub.token = "tok"
    monkeypatch.setattr("ctxswitch.common.config.load", lambda *a: cfg)
    _ledger(conf.path, _hash(), conf.clock.t)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_ev())))
    assert hook.main(["prompt-check"]) == 0
    srv.shutdown()
    assert seen == [("/v1/misplaced/7/state", "Bearer tok", {"state": "resent"})]


def test_resend_after_sixty_seconds_is_checked_again(conf, monkeypatch):
    monkeypatch.setattr(hook, "_resend", _told(conf))
    _ledger(conf.path, _hash(), conf.clock.t)
    conf.clock.t += 61
    hub = FakeHub(OTHER)
    assert hook.prompt_check(_ev(), cfg=Cfg(), client=hub, path=conf.path) == REASON
    assert len(hub.checks) == 1 and conf.resent == []                    # a stale ledger entry is not a re-send


def test_the_ledger_prunes_entries_older_than_the_window(conf):
    _ledger(conf.path, _hash("some other prompt entirely"), conf.clock.t - 61)
    conf.clock.t += 61
    hub = FakeHub(OTHER)
    assert hook.prompt_check(_ev(), cfg=Cfg(), client=hub, path=conf.path) == REASON
    assert list(json.loads(conf.path.read_text())) == [_hash()]         # the stale one is gone


def test_checking_a_prompt_touches_the_ledger_only_when_it_changes(conf):
    hub = FakeHub({"verdict": "ok"})
    assert hook.prompt_check(_ev(), cfg=Cfg(), client=hub, path=conf.path) is None
    assert not conf.path.exists()                                        # nothing to record, nothing written
    _ledger(conf.path, _hash("another prompt altogether"), conf.clock.t)
    before = conf.path.read_bytes()
    assert hook.prompt_check(_ev(), cfg=Cfg(), client=hub, path=conf.path) is None
    assert conf.path.read_bytes() == before                              # still young, still another prompt's


def test_a_resend_the_hub_refuses_still_lets_the_prompt_through(conf, monkeypatch):
    def grumpy(cfg, row_id):
        raise RuntimeError("hub is down")

    monkeypatch.setattr(hook, "_resend", grumpy)
    _ledger(conf.path, _hash(), conf.clock.t)
    assert hook.prompt_check(_ev(), cfg=Cfg(), client=FakeHub(OTHER), path=conf.path) is None


# -- fail open --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("hub", [FakeHub(raises=ConnectionError("no hub here")), FakeHub(raises=RuntimeError("boom")),
                                 FakeHub({"verdict": "ok"}), FakeHub(None), FakeHub("nonsense"),
                                 FakeHub({"verdict": "drift"})])
def test_anything_but_other_is_silence(conf, hub):
    assert hook.prompt_check(_ev(), cfg=Cfg(), client=hub, path=conf.path) is None
    assert _silent(conf.path)                                            # nothing to confirm later


def test_a_hub_that_hangs_still_answers_in_under_two_seconds(conf, capsys, monkeypatch):
    class Hanging:
        def prompt_check(self, *a):
            time.sleep(5)
            return OTHER

    monkeypatch.setattr("ctxswitch.common.config.load", lambda *a: Cfg())
    monkeypatch.setattr("ctxswitch.client.hubclient.HubClient", lambda cfg: Hanging())
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_ev())))
    t0 = time.monotonic()
    assert hook.main(["prompt-check"]) == 0
    assert time.monotonic() - t0 < 2.0
    assert capsys.readouterr().out == ""


def test_no_hub_at_all_is_silence(conf, capsys, monkeypatch):
    cfg = Config()
    cfg.hub.url = "http://127.0.0.1:9"                                   # nothing listens there
    monkeypatch.setattr("ctxswitch.common.config.load", lambda *a: cfg)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_ev())))
    assert hook.main(["prompt-check"]) == 0
    assert capsys.readouterr().out == ""


# -- what is never checked --------------------------------------------------------------------------------------
@pytest.mark.parametrize("prompt", ["/compact",
                                    OPERATOR_HEADER + "please rename the hub's metrics endpoint to /v1/samples",
                                    PEER_HEADER + "please rename the hub's metrics endpoint to /v1/samples",
                                    "yes do it", "", "   "])
def test_skips_never_ask_the_hub(conf, prompt):
    hub = FakeHub(OTHER)
    assert hook.prompt_check(_ev(prompt), cfg=Cfg(), client=hub, path=conf.path) is None
    assert hub.checks == [] and _silent(conf.path)


@pytest.mark.parametrize("ev", [{"hook_event_name": "Stop", "session_id": "s1", "prompt": PROMPT},
                                {"session_id": "s1", "prompt": PROMPT},
                                {"session_id": "a/b", "hook_event_name": "UserPromptSubmit", "prompt": PROMPT},
                                {"session_id": "", "hook_event_name": "UserPromptSubmit", "prompt": PROMPT},
                                {"session_id": "s1", "hook_event_name": "UserPromptSubmit"}])
def test_other_events_and_a_bad_session_id_ask_nothing(conf, ev):
    hub = FakeHub(OTHER)
    assert hook.prompt_check(ev, cfg=Cfg(), client=hub, path=conf.path) is None
    assert hub.checks == []


def test_the_key_is_this_host_and_the_session(conf):
    hub = FakeHub(OTHER)

    class Laptop(Cfg):
        host_id = "laptop"

    assert hook.prompt_check(_ev(sid="beefcafe"), cfg=Laptop(), client=hub, path=conf.path) is not None
    assert hub.checks[0][0] == "laptop/claude/beefcafe"


# -- install ----------------------------------------------------------------------------------------------------
@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for v in ("CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR", "XDG_CONFIG_HOME", "CTXSWITCH_SPOOL", "CTXSWITCH_CONFIG"):
        monkeypatch.delenv(v, raising=False)
    (tmp_path / ".claude").mkdir()
    monkeypatch.setattr(ins, "windows_claude_root", lambda: None)
    return tmp_path


def _groups(p, ev):
    s = json.loads(p.read_text())
    return [g for g in s["hooks"].get(ev, []) if ins._ours(g)]


def _run(fn, **kw):
    lines = []
    fn(ins.State(), dry=kw.pop("dry", False), log=lines.append, **kw)
    return lines


def test_install_adds_one_synchronous_prompt_check_group(home):
    p = home / ".claude" / "settings.json"
    _run(ins.install_claude)
    ours = _groups(p, "UserPromptSubmit")
    assert len(ours) == 2 and ours[0]["hooks"][0]["async"] is True        # the spool group stays; one check added
    (check,) = [g for g in ours if "prompt-check" in str(g)]
    (h,) = check["hooks"]
    assert h["command"] == f"{ins.hook_exe()} prompt-check"
    assert h["timeout"] == 3 and "async" not in h                        # it blocks the prompt; async output drops
    assert "matcher" not in check                                        # every prompt
    for ev in hook.EVENTS:                                               # every event still spools as before
        assert any(g["hooks"][0].get("async") is True for g in _groups(p, ev))


def test_install_twice_writes_the_group_once(home):
    p = home / ".claude" / "settings.json"
    _run(ins.install_claude)
    before = p.read_bytes()
    assert "already" in _run(ins.install_claude)[0]
    assert p.read_bytes() == before and len([g for g in _groups(p, "UserPromptSubmit") if "prompt-check" in str(g)]) == 1


def test_a_moved_venv_repoints_the_group_and_uninstall_removes_it(home, monkeypatch):
    p = home / ".claude" / "settings.json"
    _run(ins.install_claude)
    monkeypatch.setattr(ins, "hook_command", lambda: "/new/venv/bin/ctx-hook claude")
    _run(ins.install_claude)
    cmds = [h["command"] for g in _groups(p, "UserPromptSubmit") for h in g["hooks"]]
    assert cmds == ["/new/venv/bin/ctx-hook claude", "/new/venv/bin/ctx-hook prompt-check"]   # both re-pointed
    _run(ins.uninstall_claude)
    assert not p.exists() or _groups(p, "UserPromptSubmit") == []


def test_doctor_wants_the_prompt_check_group(home):
    p = home / ".claude" / "settings.json"
    _run(ins.install_claude)
    assert ins._check_claude(p).status == "✓"
    s = json.loads(p.read_text())
    s["hooks"]["UserPromptSubmit"] = [g for g in s["hooks"]["UserPromptSubmit"] if "prompt-check" not in str(g)]
    p.write_text(json.dumps(s))
    c = ins._check_claude(p)
    assert c.status == "✗" and "prompt check" in c.detail
