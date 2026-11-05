"""GET /v1/launch: one answer for "how do I start or resume this session on its host", built from what each host
published of its [[claude_profiles]] and what is installed there. The rule itself stays in the client
(client/tmux.py); these tests hold the hub's answer against it, built from the same ClaudeProfile objects."""
import asyncio
import json
import re

import httpx
from starlette.testclient import TestClient

from ctxswitch.client import tmux
from ctxswitch.collector import main as C
from ctxswitch.common.config import ClaudeProfile, Config
from ctxswitch.hub.api import Hub
from ctxswitch.hub.launch import recipe
from ctxswitch.hub.store import Store

H = {"Authorization": "Bearer t"}
MAX_HOME = "/home/rmellor/.claude-max"          # a published config_dir is expanded: a profile is a fixed place


def _profiles() -> list[ClaudeProfile]:
    """The two accounts one host offers: the corporate Bedrock one (Claude's own default directory) and a personal
    Max subscription behind a wrapper in its own config directory."""
    return [ClaudeProfile(name="bedrock", command=["claude"], model="opus"),
            ClaudeProfile(name="max", command=["/usr/local/bin/claude-wrapper"], config_dir=MAX_HOME,
                          unset=["CLAUDE_CODE_USE_BEDROCK", "AWS_PROFILE"],
                          env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HTTP_PROXY": "http://proxy.internal:8080"},
                          model="claude-fable-5-1")]


def _hub(tmp_path, profiles=None, host="laptop", agents=None, hc=True) -> tuple[Store, TestClient]:
    cfg = Config()
    cfg.claude_profiles = profiles if profiles is not None else _profiles()
    store = Store(tmp_path / "hub.db")
    store.host_seen(host, C.host_info(cfg, which=lambda c, path=None: {"pi": "/usr/bin/pi"}.get(c),
                                      hc=lambda: "/usr/bin/hc" if hc else None))
    if agents is not None:                                # a host that says what it has, whatever its profile commands
        was = json.loads(store.db.execute("SELECT info FROM hosts WHERE host_id=?", (host,)).fetchone()[0])
        store.host_seen(host, dict(was, agents=agents))
    return store, TestClient(Hub(store, token="t").app())


def _session(store, key, harness, session_id, cwd="~/repos/ctxswitch", transcript=None, host="laptop"):
    store.upsert_session({"key": key, "host_id": host, "harness": harness, "session_id": session_id,
                          "title": "a session", "cwd": cwd, "started_at": "2026-10-28T10:00:00Z",
                          "last_activity": "2026-10-28T10:00:00Z", "ended": True,
                          # what an indexer actually enqueues: the metadata dict *is* the row's meta
                          "transcript_path": transcript or ""})


# -- what a collector publishes ------------------------------------------------------------------------------------

def test_a_host_publishes_its_profiles_what_it_has_installed_and_herdr():
    cfg = Config(); cfg.claude_profiles = _profiles()
    info = C.host_info(cfg, which=lambda c, path=None: {"pi": "/usr/bin/pi"}.get(c), hc=lambda: "/usr/bin/hc")
    assert [p["name"] for p in info["profiles"]] == ["bedrock", "max"]
    assert info["profiles"][0]["argv"] == ["claude"] and info["profiles"][0]["config_dir"] == ""
    assert info["profiles"][0]["model"] == "opus"
    assert info["profiles"][1]["config_dir"] == MAX_HOME and info["profiles"][1]["unset"] == ["CLAUDE_CODE_USE_BEDROCK", "AWS_PROFILE"]
    # `n` offers Claude by each profile's command: an absolute wrapper counts without a `claude` on the PATH
    assert info["agents"] == {"claude": True, "pi": True, "agy": False, "ctx": False}
    assert info["hc"] is True
    assert C.host_info(cfg, which=lambda c, path=None: None, hc=lambda: None)["hc"] is False


def test_claude_counts_installed_when_only_a_profile_command_is_found():
    """`n` offers Claude by each profile's command — a Max box may run a wrapper and no bare `claude`."""
    cfg = Config(); cfg.claude_profiles = [ClaudeProfile(name="bedrock"), ClaudeProfile(name="max", command=["wrap"])]
    assert C.host_info(cfg, which=lambda c, path=None: None)["agents"]["claude"] is False
    assert C.host_info(cfg, which=lambda c, path=None: "/x/" + c)["agents"]["claude"] is True
    cfg.claude_profiles = [ClaudeProfile(name="bedrock"), ClaudeProfile(name="max", command=["/opt/wrap"])]
    assert C.host_info(cfg, which=lambda c, path=None: None)["agents"]["claude"] is True


def test_the_ingest_body_carries_the_launch_facts(tmp_path, monkeypatch):
    """The collector's own push: the hub must find the profiles in the `info` of an ordinary ingest."""
    cfg = Config()
    cfg.claude_profiles = _profiles()
    cfg.collector.host_id, cfg.collector.queue_path = "laptop", str(tmp_path / "q.db")
    cfg.hub.url, cfg.hub.token = "http://hub", "t"
    col = C.Collector(cfg, once=True)
    col._emit("session", {"key": "laptop/pi/p1", "host_id": "laptop", "harness": "pi", "session_id": "p1"})
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"acked": 1})
    monkeypatch.setattr(C.shutil, "which", lambda c, path=None: "/usr/bin/" + c)
    real = httpx.AsyncClient                       # the real class: the lambda below must not route back through the patch
    monkeypatch.setattr(C.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items()
                                                                                     if k != "base_url"}))
    asyncio.run(col.push_loop())
    info = seen[0]["info"]
    assert [p["name"] for p in info["profiles"]] == ["bedrock", "max"]
    assert info["agents"] == {"claude": True, "pi": True, "agy": True, "ctx": True} and info["hc"] is True


# -- resuming, host by host ----------------------------------------------------------------------------------------

def test_the_hub_answer_is_the_argv_the_client_would_build(tmp_path, monkeypatch):
    """Parity, both profiles and all four harnesses: the same ClaudeProfile objects give the same argv, so a JS
    front end and `ctx` can't diverge on how a session comes back."""
    # the client names `ctx` by this install's own path; the hub names the command, as tmux.remote_script does when
    # the path means nothing on the machine that will run it
    monkeypatch.setattr(tmux, "_ctx_bin", lambda: "ctx")
    store, c = _hub(tmp_path)
    profiles = _profiles()
    for tp, sid, want in ((f"{MAX_HOME}/projects/-r/m.jsonl", "m", "max"),
                          ("/home/rmellor/.claude/projects/-r/b.jsonl", "b", "bedrock"),
                          ("/elsewhere/projects/-r/o.jsonl", "o", "bedrock")):     # nowhere: the first profile
        key = f"laptop/claude/{sid}"
        _session(store, key, "claude", sid, transcript=tp)
        mine = c.get("/v1/launch", params={"key": key}, headers=H).json()
        assert mine["argv"] == tmux.resume_argv(store.session(key, turns=0), profiles)
        assert mine["profile"] == want and mine["harness"] == "claude" and mine["key"] == key
        assert mine["host"] == "laptop" and mine["cwd"] == "~/repos/ctxswitch"
    maxed = c.get("/v1/launch", params={"key": "laptop/claude/m"}, headers=H).json()
    assert maxed["argv"] == ["env", "-u", "CLAUDE_CODE_USE_BEDROCK", "-u", "AWS_PROFILE",
                             f"PATH={maxed['env']['PATH']}", f"HTTP_PROXY={maxed['env']['HTTP_PROXY']}",
                             f"CLAUDE_CONFIG_DIR={MAX_HOME}", "/usr/local/bin/claude-wrapper", "--resume", "m"]
    assert maxed["label"] == "claude[max]" and maxed["config_dir"] == MAX_HOME
    assert maxed["installed"] is True                # the absolute wrapper is a command, so Claude is installed here
    bed = c.get("/v1/launch", params={"key": "laptop/claude/b"}, headers=H).json()
    assert bed["argv"] == ["claude", "--resume", "b"] and bed["label"] == "claude[bedrock]"   # more than one account
    assert bed["env"] == {} and bed["unset"] == [] and bed["config_dir"] == ""
    for harness, sid, tp in (("pi", "p1", "/home/rmellor/.pi/agent/sessions/a.jsonl"), ("agy", "g1", ""),
                             ("shell", "s1", "")):
        key = f"laptop/{harness}/{sid}"
        _session(store, key, harness, sid, transcript=tp)
        mine = c.get("/v1/launch", params={"key": key}, headers=H).json()
        assert mine["argv"] == tmux.resume_argv(store.session(key, turns=0), profiles), harness
        assert mine["profile"] is None and mine["label"] == harness and mine["key"] == key and mine["env"] == {}
    assert c.get("/v1/launch", params={"key": "laptop/pi/p1"}, headers=H).json()["argv"] == [
        "pi", "--session", "/home/rmellor/.pi/agent/sessions/a.jsonl"]
    assert c.get("/v1/launch", params={"key": "laptop/agy/g1"}, headers=H).json()["argv"] == [
        "agy", "--conversation", "g1"]
    assert c.get("/v1/launch", params={"key": "laptop/shell/s1"}, headers=H).json()["argv"] == [
        "ctx", "shell", "--id", "s1"]


def test_a_pi_session_with_no_transcript_cannot_be_resumed(tmp_path):
    store, c = _hub(tmp_path)
    _session(store, "laptop/pi/p9", "pi", "p9", transcript=None)
    assert tmux.resume_argv(store.session("laptop/pi/p9", turns=0), _profiles()) is None
    assert c.get("/v1/launch", params={"key": "laptop/pi/p9"}, headers=H).status_code == 400


def test_a_host_that_published_nothing_resumes_claude_plain(tmp_path):
    store = Store(tmp_path / "hub.db")
    store.host_seen("devbox", {"collector": "0.0.1"})
    _session(store, "devbox/claude/c1", "claude", "c1", transcript="/home/x/.claude/projects/-r/c1.jsonl", host="devbox")
    r = TestClient(Hub(store, token="t").app()).get("/v1/launch", params={"key": "devbox/claude/c1"}, headers=H).json()
    assert r["argv"] == ["claude", "--resume", "c1"] and r["profile"] == "default" and r["label"] == "claude"
    assert r["unset"] == [] and r["env"] == {} and r["config_dir"] == ""
    assert r["installed"] is None                            # the host said nothing about what it has


def test_a_transcript_under_a_relative_config_dir_is_still_that_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/home/rmellor")
    profiles = [ClaudeProfile(name="bedrock"), ClaudeProfile(name="max", config_dir="~/.claude-max")]
    store, c = _hub(tmp_path, profiles=profiles)
    _session(store, "laptop/claude/m2", "claude", "m2", transcript="/home/rmellor/.claude-max/projects/-r/m2.jsonl")
    r = c.get("/v1/launch", params={"key": "laptop/claude/m2"}, headers=H).json()
    assert r["profile"] == "max" and r["config_dir"] == "/home/rmellor/.claude-max"
    assert r["argv"] == tmux.resume_argv(store.session("laptop/claude/m2", turns=0), profiles)


# -- starting something new ----------------------------------------------------------------------------------------

def test_a_new_claude_session_uses_the_named_profile_and_its_model(tmp_path):
    store, c = _hub(tmp_path)
    maxed = c.get("/v1/launch", params={"host": "laptop", "harness": "claude", "profile": "max"}, headers=H).json()
    assert maxed["argv"][-2:] == ["--model", "claude-fable-5-1"]
    assert maxed["profile"] == "max" and maxed["label"] == "claude[max]" and maxed["key"] is None and maxed["cwd"] == "~"
    assert maxed["env"]["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    plain = c.get("/v1/launch", params={"host": "laptop", "harness": "claude"}, headers=H).json()
    assert plain["argv"] == ["claude", "--model", "opus"] and plain["profile"] == "bedrock"


def test_a_new_session_is_the_argv_the_client_would_build(tmp_path, monkeypatch):
    """Parity for `n`: the hub's argv, profile and label are what new_agent builds and names its window with."""
    store, c = _hub(tmp_path)
    profiles = _profiles()
    made: list[tuple[list[str], str]] = []
    monkeypatch.setattr(tmux, "in_tmux", lambda: True)
    monkeypatch.setattr(tmux, "shutil", type("W", (), {"which": staticmethod(lambda c, path=None: "/usr/bin/" + c)}))
    monkeypatch.setattr(tmux, "viewer_pane", lambda ps=None: tmux.Pane("%1", "@1", "mgr", 0, 1, "", "ctx"))
    monkeypatch.setattr(tmux, "panes", lambda: [])
    monkeypatch.setattr(tmux, "_run", lambda *a, **kw: "")
    monkeypatch.setattr(tmux, "_this_host", lambda: "laptop")
    monkeypatch.setattr(tmux, "_agent_window",
                        lambda ts, cwd, name, argv, role="agent": (made.append((argv, name)), "%9")[1])
    monkeypatch.setenv("TMUX_PANE", "%1")
    for harness, profile in (("claude", None), ("claude", "max"), ("claude", "bedrock"), ("pi", None), ("agy", None)):
        said = tmux.new_agent(harness, str(tmp_path), profile=profile, profiles=profiles)
        argv, label = made[-1]
        mine = c.get("/v1/launch", params={"host": "laptop", "harness": harness, "cwd": str(tmp_path)}
                     | ({"profile": profile} if profile else {}), headers=H).json()
        assert said.startswith("new "), said
        assert argv[-len(mine["argv"]):] == mine["argv"], (argv, mine["argv"])   # `n` may add extra args after them
        assert mine["profile"] == ((profile or "bedrock") if harness == "claude" else None)
        assert mine["label"] in label, (mine["label"], label)                     # tmux names the window "<label>:<dir>"
    assert made[1][0][0] == "env"                             # the max profile wraps its command in `env ...`


def test_a_new_shell_comes_back_with_the_key_to_label_its_terminal(tmp_path):
    store, c = _hub(tmp_path)
    r = c.get("/v1/launch", params={"host": "devbox", "harness": "shell", "cwd": "/r"}, headers=H).json()
    sid = r["key"].rsplit("/", 1)[1]
    assert r["key"] == f"devbox/shell/{sid}" and r["argv"] == ["ctx", "shell", "--id", sid]
    assert r["cwd"] == "/r" and r["harness"] == "shell" and r["profile"] is None and r["label"] == "shell"
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", sid)


def test_pi_and_agy_start_bare_and_say_whether_they_are_installed(tmp_path):
    store, c = _hub(tmp_path, agents={"claude": True, "pi": True, "agy": False, "ctx": True})
    assert c.get("/v1/launch", params={"host": "laptop", "harness": "pi"}, headers=H).json() == {
        "host": "laptop", "harness": "pi", "profile": None, "argv": ["pi"], "cwd": "~", "env": {}, "unset": [],
        "config_dir": "", "label": "pi", "key": None, "installed": True}
    assert c.get("/v1/launch", params={"host": "laptop", "harness": "agy"}, headers=H).json()["installed"] is False


# -- when it can't be answered, and what must not leak -------------------------------------------------------------

def test_an_unknown_session_is_404_and_a_bad_request_400(tmp_path):
    store, c = _hub(tmp_path)
    assert c.get("/v1/launch", params={"key": "laptop/claude/nope"}, headers=H).status_code == 404
    assert "error" in c.get("/v1/launch", headers=H).json()
    assert c.get("/v1/launch", params={"harness": "claude"}, headers=H).status_code == 400          # no host
    assert c.get("/v1/launch", params={"host": "laptop"}, headers=H).status_code == 400             # no harness
    assert c.get("/v1/launch", params={"host": "laptop", "harness": "herdr"}, headers=H).status_code == 400
    assert c.get("/v1/launch", params={"host": "laptop", "harness": "claude", "profile": "nope"},
                 headers=H).status_code == 400
    assert c.get("/v1/launch", params={"key": "laptop/claude/nope"}).status_code == 401              # before the look
    _session(store, "laptop/herdr/h1", "herdr", "h1")
    assert recipe(store, key="laptop/herdr/h1") is None          # no way to resume it from here


def test_a_host_the_hub_has_never_seen_still_answers(tmp_path):
    store, c = _hub(tmp_path)
    r = c.get("/v1/launch", params={"host": "gpu-host", "harness": "claude"}, headers=H).json()
    assert r["argv"] == ["claude"] and r["profile"] == "default" and r["installed"] is None


def test_the_route_and_the_function_agree(tmp_path):
    store, c = _hub(tmp_path)
    _session(store, "laptop/claude/m3", "claude", "m3", transcript=f"{MAX_HOME}/projects/-r/m3.jsonl")
    for kw in ({"key": "laptop/claude/m3"}, {"host": "laptop", "harness": "claude", "profile": "max", "cwd": "/r"}):
        assert c.get("/v1/launch", params=kw, headers=H).json() == recipe(store, **kw)


def test_a_profile_publishes_no_secrets():
    """A credential's name is enough to hide its value — in the env map and inside argv's `K=V` pairs alike. A
    profile that hands Claude a key this way publishes the command's shape, not the key, and says so by the mask."""
    p = ClaudeProfile(name="max", command=["claude"], config_dir="~/.claude-max",
                      env={"ANTHROPIC_API_KEY": "sk-ant-1", "FOO_TOKEN": "hunter2", "MY_SECRET": "s",
                           "ANTHROPIC_BASE_URL": "https://bedrock.example/v1", "PATH": "/usr/local/bin:/usr/bin"},
                      unset=["AWS_PROFILE"], model="opus")
    pub = p.to_public()
    assert pub["name"] == "max" and pub["unset"] == ["AWS_PROFILE"] and pub["model"] == "opus"
    assert pub["env"] == {"ANTHROPIC_API_KEY": "***", "FOO_TOKEN": "***", "MY_SECRET": "***",
                          "ANTHROPIC_BASE_URL": "https://bedrock.example/v1", "PATH": "/usr/local/bin:/usr/bin"}
    assert pub["argv"] == ["env", "-u", "AWS_PROFILE", "ANTHROPIC_API_KEY=***", "FOO_TOKEN=***", "MY_SECRET=***",
                           "ANTHROPIC_BASE_URL=https://bedrock.example/v1", "PATH=/usr/local/bin:/usr/bin",
                           f"CLAUDE_CONFIG_DIR={pub['config_dir']}", "claude"]
    assert "sk-ant-1" not in json.dumps(pub) and "hunter2" not in json.dumps(pub)


def test_a_launch_answer_repeats_the_host_mask(tmp_path):
    """The route's env and argv come from what the collector published, so nothing it masked comes back unmasked."""
    cfg = Config()
    cfg.claude_profiles = [ClaudeProfile(name="max", command=["claude"], config_dir="~/.claude-max",
                                         env={"FOO_TOKEN": "hunter2", "PATH": "/usr/local/bin:/usr/bin"})]
    store = Store(tmp_path / "hub.db")
    store.host_seen("laptop", C.host_info(cfg, which=lambda c, path=None: None, hc=lambda: None))
    c = TestClient(Hub(store, token="t").app())
    r = c.get("/v1/launch", params={"host": "laptop", "harness": "claude", "profile": "max"}, headers=H).json()
    assert r["env"] == {"FOO_TOKEN": "***", "PATH": "/usr/local/bin:/usr/bin"}
    assert r["argv"] == ["env", "FOO_TOKEN=***", "PATH=/usr/local/bin:/usr/bin",
                         f"CLAUDE_CONFIG_DIR={r['config_dir']}", "claude"]
    assert "hunter2" not in c.get("/v1/launch", params={"host": "laptop", "harness": "claude"}, headers=H).text


def test_the_default_account_is_matched_by_the_paths_shape_not_the_hubs_home(monkeypatch):
    """The hub runs in a container: its ~ is not the session host's. A transcript under /home/dev/.claude/projects
    belongs to the profile with no config_dir wherever the hub's HOME points."""
    from ctxswitch.hub import launch as L
    monkeypatch.setenv("HOME", "/nonexistent/hub-home")
    default = {"name": "bedrock", "argv": ["claude"], "config_dir": ""}
    personal = {"name": "max", "argv": ["claude-wrapper"], "config_dir": "/home/dev/.claude-max"}
    assert L._profile_of([default, personal], "/home/dev/.claude/projects/-home-dev-x/s1.jsonl") is default
    assert L._profile_of([default, personal], "/home/dev/.claude-max/projects/-home-dev-x/s2.jsonl") is personal
    assert L._owns(default, None) is False
