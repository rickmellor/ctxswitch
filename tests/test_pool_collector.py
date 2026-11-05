"""The local Qwen pool as the collector sees it: what each Claude session was told at its last SessionStart (or by a
`saint-pool` call it ran), and where this host's switch stands now. The board needs both to say "this session thinks
the pool is on, and its host has since flipped it off"."""
import asyncio
import json

import pytest
from claude_rows import S, assistant, text, tool_result, user

from ctxswitch.collector import main as C
from ctxswitch.collector.adapters.claude import ClaudeParser
from ctxswitch.collector.indexer import Indexer
from ctxswitch.collector.state import CollectorState
from ctxswitch.common.config import CollectorConfig, Config
from ctxswitch.common.schema import SessionMeta

HOOK = "/home/rick/.local/bin/saint-pool --hook"
CTX_ON = ('{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "LOCAL POOL ON '
          '(http://gpu-host:4000): construction goes to the local Qwen pool via saint-agent."}}')
CTX_UNREACH = ('{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "LOCAL POOL ON but '
               'SAINT at http://gpu-host:4000 is unreachable: work in this session as normal."}}')


def hook(ts, stdout, hook_name="SessionStart:startup", command=HOOK, **kw):
    """A Claude `attachment` entry for a SessionStart hook that ran and printed something."""
    a = {"type": "hook_success", "hookName": hook_name, "hookEvent": "SessionStart", "stdout": stdout,
         "command": command, "exitCode": 0}
    d = {"type": "attachment", "timestamp": ts, "sessionId": S, "attachment": a}
    d.update(kw)
    return d


def parse(rows):
    p = ClaudeParser()
    for i, r in enumerate(rows):
        p.feed(r, (i + 1) * 100)
    return p


# -- the SessionStart hook's stdout -------------------------------------------------------------------------------

def test_a_startup_hook_that_said_the_pool_is_on_sets_it_with_how_and_when():
    p = parse([user("go", "2026-10-30T22:00:00Z"), hook("2026-10-30T22:00:01Z", CTX_ON)])
    assert (p.meta["pool"], p.meta["pool_how"], p.meta["pool_at"]) == ("on", "startup", "2026-10-30T22:00:01Z")


@pytest.mark.parametrize("stdout,want", [
    (CTX_ON, "on"),
    (CTX_UNREACH, "unreachable"),                 # "LOCAL POOL ON but SAINT … is unreachable"
    ('{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "LOCAL POOL ON (http://x:4000)"}}', "on"),
    ("", "off"), ("   \n", "off"),                # OFF: the hook prints nothing at all
])
def test_the_hooks_stdout_decides_the_state(stdout, want):
    p = parse([hook("2026-10-30T22:00:00Z", stdout, hook_name="SessionStart:resume")])
    assert p.meta["pool"] == want and p.meta["pool_how"] == "resume"


def test_a_hook_without_a_colon_in_its_name_is_a_startup():
    for name in ("SessionStart", "SessionStart:clear", "SessionStart:compact"):
        p = parse([hook("2026-10-30T22:00:00Z", CTX_ON, hook_name=name)])
        assert p.meta["pool_how"] == ("startup" if name == "SessionStart" else name.split(":")[1])


def test_another_sessions_hook_says_nothing_about_the_pool():
    p = parse([hook("2026-10-30T22:00:00Z", CTX_ON, command="/home/rick/.local/bin/ctx hook session-start")])
    assert "pool" not in p.meta
    p = parse([hook("2026-10-30T22:00:00Z", CTX_ON, command="grep -n saint-pool ~/.local/bin/saint-pool")])
    assert "pool" not in p.meta                                          # a hook that merely mentions the script


def test_a_sidechains_hook_changes_nothing_and_a_later_one_wins():
    p = parse([hook("2026-10-30T22:00:00Z", CTX_ON), hook("2026-10-30T22:01:00Z", "", hook_name="SessionStart:clear",
                                                          isSidechain=True)])
    assert (p.meta["pool"], p.meta["pool_at"]) == ("on", "2026-10-30T22:00:00Z")


def test_the_context_attachment_alongside_the_hook_is_not_read_twice():
    """Claude writes the hook's additionalContext again as a hook_additional_context entry: same text, one signal."""
    ctx = {"type": "attachment", "attachment": {"type": "hook_additional_context", "content": ["LOCAL POOL ON (x)"]}}
    p = parse([dict(ctx, timestamp="2026-10-30T22:00:00Z"), dict(ctx, timestamp="2026-10-30T22:01:00Z")])
    assert "pool" not in p.meta


# -- `saint-pool on|off|status` run inside the session -------------------------------------------------------------

def test_a_tool_result_saying_the_pool_is_off_sets_it_as_a_command():
    p = parse([user("toggle it", "2026-10-30T22:00:00Z"),
               assistant([text("running it.")], "2026-10-30T22:00:01Z"),
               tool_result("t1", "saint-pool: OFF", "2026-10-30T22:00:02Z")])
    assert (p.meta["pool"], p.meta["pool_how"], p.meta["pool_at"]) == ("off", "command", "2026-10-30T22:00:02Z")


@pytest.mark.parametrize("out,want", [
    ("saint-pool: ON (http://x:4000, reachable)", "on"),
    ("saint-pool: ON (http://x:4000, UNREACHABLE)", "unreachable"),
    ("saint-pool: ON (http://x) — WARNING: SAINT not reachable right now", "unreachable"),
    ("saint-pool: OFF", "off"),
    ("saint-pool: unknown command 'x' (on|off|status|--hook)", ""),
    ("16:  on)  URL=\"${2:-$(default_url)}\"\n42:  echo \"saint-pool: ON ($URL, reachable)\"", ""),   # a grep of the script
    ("here is the file:\nsaint-pool: OFF\nand the rest of it", "off"),
])
def test_only_whole_lines_of_a_tool_result_count(out, want):
    for content in (out, [{"type": "text", "text": out}]):        # str, or a list of blocks
        p = parse([hook("2026-10-30T22:00:00Z", CTX_ON), tool_result("t1", content, "2026-10-30T22:00:01Z")])
        assert p.meta["pool"] == (want or "on"), content


def test_the_last_line_of_a_tool_result_wins_over_an_earlier_one():
    p = parse([tool_result("t1", "saint-pool: ON (http://x:4000, reachable)\nthen:\nsaint-pool: OFF",
                           "2026-10-30T22:00:00Z")])
    assert p.meta["pool"] == "off"


def test_the_newest_signal_wins_whatever_it_came_from():
    rows = [hook("2026-10-30T22:00:00Z", CTX_ON),                                        # startup: on
            user("turn the pool off", "2026-10-30T22:01:00Z"), tool_result("t1", "saint-pool: OFF",
                                                                           "2026-10-30T22:01:01Z")]
    p = parse(rows)
    assert (p.meta["pool"], p.meta["pool_how"], p.meta["pool_at"]) == ("off", "command", "2026-10-30T22:01:01Z")
    p.feed(hook("2026-10-30T22:02:00Z", CTX_ON, hook_name="SessionStart:resume"), 900)   # resumed later, back on
    assert (p.meta["pool"], p.meta["pool_how"], p.meta["pool_at"]) == ("on", "resume", "2026-10-30T22:02:00Z")


def test_the_pool_survives_a_state_round_trip():
    p = parse([hook("2026-10-30T22:00:00Z", CTX_ON, hook_name="SessionStart:resume")])
    q = ClaudeParser.from_state(p.state())
    assert (q.meta["pool"], q.meta["pool_at"], q.meta["pool_how"]) == ("on", "2026-10-30T22:00:00Z", "resume")
    q.feed(tool_result("t1", "saint-pool: OFF", "2026-10-30T22:01:00Z"), 200)
    assert q.meta["pool"] == "off"


def test_a_session_meta_carries_the_three_fields():
    m = SessionMeta(host_id="laptop", harness="claude", session_id=S, transcript_path="/x.jsonl")
    assert (m.pool, m.pool_at, m.pool_how) == ("", "", "")
    assert {k: m.to_dict()[k] for k in ("pool", "pool_at", "pool_how")} == {"pool": "", "pool_at": "", "pool_how": ""}


# -- the indexer ships it in the session meta ---------------------------------------------------------------------

def _ix(tmp_path, events):
    cfg = CollectorConfig(claude_roots=[str(tmp_path / "claude")], pi_roots=[str(tmp_path / "pi")], backfill_days=30)
    return Indexer(cfg, "laptop", CollectorState(tmp_path / "c.db"), lambda k, p: events.append((k, p)))


def test_the_indexer_puts_the_pools_state_in_the_claude_sessions_meta(tmp_path):
    root = tmp_path / "claude" / "-home-rick-repos-demo"; root.mkdir(parents=True)
    events = []
    ix = _ix(tmp_path, events)
    f = root / f"{S}.jsonl"
    with open(f, "wb") as h:
        for r in [user("go", "2026-10-30T22:00:00Z"), hook("2026-10-30T22:00:01Z", CTX_ON),
                  assistant([text("ok")], "2026-10-30T22:00:02Z")]:
            h.write((json.dumps(r) + "\n").encode())
    ix.live_keys = {f"laptop/claude/{S}"}
    assert ix.scan_all() == 1
    sess = [p for k, p in events if k == "session"][0]
    assert (sess["pool"], sess["pool_at"], sess["pool_how"]) == ("on", "2026-10-30T22:00:01Z", "startup")


def test_the_indexer_leaves_the_other_harnesses_pool_empty(tmp_path):
    """Only Claude's parser reads the switch out of a transcript; the field is there for the board, blank elsewhere."""
    ix = _ix(tmp_path, [])
    f = tmp_path / "x.jsonl"
    f.touch()
    for harness in ("pi", "shell", "agy", "claude-win"):
        m = ix._meta({"cwd": "/x", "pool": "on", "pool_at": "t", "pool_how": "startup"},
                     f"laptop/{harness}/sid", f, harness, f.stat())
        assert (m["pool"], m["pool_at"], m["pool_how"]) == ("", "", ""), harness


# -- the host's own switch ---------------------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code=200):
        self.status_code = status_code


def _switch(tmp_path, url="http://gpu-host:4000\n"):
    p = tmp_path / "saint-pool"
    p.write_text(url)
    return str(p)


def test_a_collector_told_not_to_look_reports_nothing():
    assert C.read_pool("") is None


def test_a_missing_or_empty_switch_means_off_with_no_url(tmp_path):
    for path in (str(tmp_path / "nope"), _switch(tmp_path, ""), _switch(tmp_path, "  \n")):
        d = C.read_pool(path, get=lambda url, **kw: pytest.fail("off must not probe"))
        assert (d["state"], d["url"], d["host_id"]) == ("off", "", ""), path
        assert d["checked_at"].endswith("Z") and len(d["checked_at"]) == 20


def test_a_switch_that_names_a_SAINT_probes_its_health(tmp_path):
    seen = []
    d = C.read_pool(_switch(tmp_path, " http://gpu-host:4000/ \n# a later line is no URL\n"),
                    get=lambda url, **kw: seen.append((url, kw)) or _Resp(200))
    assert (d["state"], d["url"]) == ("on", "http://gpu-host:4000/")     # the line as written, minus whitespace
    assert seen == [("http://gpu-host:4000/health", {"timeout": 5})]     # ... but one slash on the probe


@pytest.mark.parametrize("get", [lambda url, **kw: _Resp(503), lambda url, **kw: _Resp(404),
                                 lambda url, **kw: (_ for _ in ()).throw(ConnectionError("down"))])
def test_a_SAINT_that_does_not_answer_is_unreachable(get, tmp_path):
    assert C.read_pool(_switch(tmp_path), get=get)["state"] == "unreachable"


def test_one_slow_answer_is_tried_again_before_the_host_is_called_unreachable(tmp_path):
    """SAINT's /health once took 3.3 s, then 10 ms: a single timeout mustn't flag every session on the host stale."""
    calls = []

    def get(url, **kw):
        calls.append(url)
        if len(calls) == 1:
            raise TimeoutError("read timed out")
        return _Resp(200)
    assert C.read_pool(_switch(tmp_path), get=get)["state"] == "on" and len(calls) == 2
    calls.clear()
    assert C.read_pool(_switch(tmp_path), get=lambda url, **kw: calls.append(url) or _Resp(503))["state"] == "unreachable"
    assert len(calls) == 2                                                      # and twice is all it tries


# -- the loop that reports it ------------------------------------------------------------------------------------

def _collector(tmp_path):
    cfg = Config()
    cfg.collector.host_id, cfg.collector.queue_path = "laptop", str(tmp_path / "q.db")
    return C.Collector(cfg)


async def test_the_pool_loop_reports_at_start_and_only_when_the_switch_moves(tmp_path, monkeypatch):
    col = _collector(tmp_path)
    now = [{"host_id": "", "state": "on", "url": "http://saint:4000", "checked_at": "2026-10-31T00:00:00Z"}]
    monkeypatch.setattr(C, "read_pool", lambda path, get=None: dict(now[0]))
    naps = []

    async def nap(s):
        naps.append(s)
        if len(naps) >= 3:                       # three polls of an unchanged switch: one event, then stop
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", nap)
    with pytest.raises(asyncio.CancelledError):
        await col.pool_loop()
    assert [k for _, k, _ in col.state.pending()] == ["pool"]
    assert col.state.pending()[0][2] == {"host_id": "laptop", "state": "on", "url": "http://saint:4000",
                                         "checked_at": "2026-10-31T00:00:00Z"}

    now[0].update(state="off", url="", checked_at="2026-10-31T00:01:00Z")     # the operator ran `saint-pool off`
    naps.clear()
    with pytest.raises(asyncio.CancelledError):
        await col.pool_loop()
    assert [p["state"] for _, k, p in col.state.pending() if k == "pool"] == ["on", "off"]


async def test_a_collector_with_no_switch_file_runs_no_pool_loop(tmp_path, monkeypatch):
    col = _collector(tmp_path)
    col.cfg.collector.pool_file = ""
    got = []
    monkeypatch.setattr(C, "read_pool", lambda path, get=None: got.append(path) or None)
    await col.pool_loop()
    assert got == [] and col.state.pending() == []


def test_a_transcript_read_before_the_pool_was_tracked_catches_up_once(tmp_path):
    """A saved parser state from an older collector has read past the pool lines: they're read again, once."""
    import json as _json
    from ctxswitch.collector.indexer import Indexer
    from ctxswitch.collector.state import CollectorState
    from ctxswitch.common.config import CollectorConfig
    root = tmp_path / "projects"; (root / "p").mkdir(parents=True)
    f = root / "p" / "s1.jsonl"
    hook = {"type": "attachment", "timestamp": "2026-11-01T10:00:00Z", "sessionId": "s1", "cwd": "/w",
            "attachment": {"type": "hook_success", "hookName": "SessionStart:resume", "hookEvent": "SessionStart",
                           "stdout": "LOCAL POOL ON (http://x:4000): ...", "command": "/u/bin/saint-pool --hook"}}
    rows = [hook, user("go", "2026-11-01T10:00:01Z")]
    f.write_text("".join(_json.dumps(r) + "\n" for r in rows))
    cfg = CollectorConfig(claude_roots=[str(root)], pi_roots=[], claude_win_roots=[], agy_roots=[], shell_roots=[])
    state, sent = CollectorState(tmp_path / "s.db"), []
    ix = Indexer(cfg, "laptop", state, lambda kind, p: sent.append((kind, p)))
    ix.process(f, "claude")
    rec = state.file(str(f))
    old = _json.loads(_json.dumps(rec["parser_state"]))
    for k in ("pool", "pool_at", "pool_how", "pool_seen"):                 # as an older collector saved it
        old["meta"].pop(k, None)
    state.save_file(str(f), "claude", rec["session_key"], rec["offset"], old, rec["size"], rec["mtime"], rec["turns_emitted"], True)
    sent.clear()
    ix2 = Indexer(cfg, "laptop", state, lambda kind, p: sent.append((kind, p)))
    ix2.process(f, "claude")
    metas = [p for k, p in sent if k == "session"]
    assert metas and (metas[-1]["pool"], metas[-1]["pool_how"]) == ("on", "resume")
    assert state.file(str(f))["parser_state"]["meta"]["pool_seen"] is True    # saved: next time, no second read
