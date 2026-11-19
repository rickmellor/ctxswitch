"""ctx-hook agy <Event>: the one JSON object it must answer with, whatever it was handed, and the agy inbox it
claims. The hub's matcher is another job's: here a fake HubClient says "other" or doesn't answer, exactly as
test_misplace_hook.py does for Claude. No real ~/.gemini and no real inbox: everything under tmp_path."""
import io
import json
import sys
import time

import pytest

from ctxswitch import hook
from ctxswitch.common.config import Config
from ctxswitch.common.schema import question_text

SID = "conv-1"
TOOL = hook.AGY_ASK_TOOL
PROMPT = "rename the hub's metrics endpoint to /v1/samples and update the board"
OTHER = {"verdict": "other", "id": 7, "target_key": "devbox/agy/aaaa1111", "target_title": "hub endpoint rename",
         "target_where": "devbox · ctxswitch@main", "why": "names the hub's metrics endpoint"}
REASON = (f'ctx: this looks like it\'s for "{OTHER["target_title"]}" ({OTHER["target_where"]}) — {OTHER["why"]}.\n'
          "Press ↑ then Enter within a minute to send it here anyway, or move it from the ctx board (t).")


class Cfg(Config):
    """A Config that answers with a fixed host id, whatever this machine's hostname is."""
    host_id = "gpu-host"


class FakeHub:
    def __init__(self, reply=None, raises=None):
        self.reply, self.raises = reply, raises
        self.checks = []

    def prompt_check(self, key, prompt, prompt_id, cwd):
        self.checks.append((key, prompt, prompt_id, cwd))
        if self.raises:
            raise self.raises
        return self.reply


@pytest.fixture
def agyhome(tmp_path, monkeypatch):
    spool, inbox = tmp_path / "spool", tmp_path / "inbox"
    monkeypatch.setenv("CTXSWITCH_SPOOL", str(spool))
    monkeypatch.setenv("CTXSWITCH_AGY_INBOX", str(inbox))
    monkeypatch.setattr(hook, "CONFIRM_FILE", tmp_path / "state" / "prompt-confirm.json")
    return type("H", (), {"spool": spool, "inbox": inbox, "tmp": tmp_path})()


def _ev(event, **kw):
    return {"hookEventName": event, "conversationId": SID, "cwd": "/r", "workspacePaths": ["/r"],
            "transcriptPath": "/t.json", "modelName": "gemini-4-hydrogen-medium", **kw}


def _run(event, raw, capsys, *, argv=None):
    """One hook command: stdin in, the one object it printed out (asserting it printed exactly one)."""
    old, sys.stdin = sys.stdin, io.StringIO(raw)
    try:
        assert hook.main(argv or ["agy", event]) == 0
    finally:
        sys.stdin = old
    out = capsys.readouterr().out
    assert out.count("\n") == 1, out
    return json.loads(out)


def _hub(monkeypatch, reply=None, raises=None):
    monkeypatch.setattr("ctxswitch.common.config.load", lambda *a: Cfg())
    h = FakeHub(reply, raises)
    monkeypatch.setattr("ctxswitch.client.hubclient.HubClient", lambda cfg: h)
    return h


def _inbox(*texts, raw=None):
    """What the collector (#3) left waiting for this conversation."""
    p = hook.agy_inbox_dir() / f"{SID}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    body = raw if raw is not None else "".join(json.dumps({"msg_id": i + 1, "text": t}) + "\n" for i, t in enumerate(texts))
    p.write_text(body)
    return p


def _records(spool):
    return [json.loads(p.read_text()) for p in sorted(spool.glob("*.json"))]


# -- PreToolUse: an explicit decision, or agy denies the tool ----------------------------------------------------
def test_pre_tool_use_always_answers_ask(agyhome, capsys):
    for raw in (json.dumps(_ev("PreToolUse", toolCall={"name": "Bash", "args": {"command": "ls"}, "id": "t1"})),
                json.dumps(_ev("PreToolUse", toolCall={"name": TOOL, "args": {"question": "Red or blue?"}, "id": "t2"})),
                json.dumps(_ev("PreToolUse")),                                    # no toolCall at all
                "", "{not json", "[]", json.dumps([1, 2])):
        assert _run("PreToolUse", raw, capsys) == {"decision": "ask"}


def test_pre_tool_use_answers_ask_with_an_unwritable_spool(agyhome, capsys, monkeypatch):
    (agyhome.tmp / "blocker").write_text("")
    monkeypatch.setenv("CTXSWITCH_SPOOL", str(agyhome.tmp / "blocker" / "sub"))    # mkdir there raises
    raw = json.dumps(_ev("PreToolUse", conversationId=SID, toolCall={"name": TOOL, "args": {"question": "q?"}}))
    assert _run("PreToolUse", raw, capsys) == {"decision": "ask"}


def test_pre_tool_use_spools_only_the_question_tool(agyhome, capsys):
    for name, args in (("Bash", {"command": "ls"}), (TOOL, {"question": "Red or blue?", "options": ["Red", "Blue"]})):
        _run("PreToolUse", json.dumps(_ev("PreToolUse", toolCall={"name": name, "args": args, "id": "t1"})), capsys)
    (rec,) = _records(agyhome.spool)
    assert rec["harness"] == "agy" and rec["session_id"] == SID and rec["event"] == "PreToolUse"
    assert rec["data"]["tool_name"] == TOOL and rec["data"]["cwd"] == "/r" and rec["data"]["transcript_path"] == "/t.json"
    assert rec["data"]["tool_input"] == {"question": "Red or blue?", "options": ["Red", "Blue"]}
    assert rec["data"]["questions"] == [{"question": "Red or blue?", "options": ["Red", "Blue"]}]


def test_the_question_text_is_made_the_same_way_as_claude_s(agyhome, capsys):
    for args, want in (({"prompt": "Ship it?"}, "Ship it?"),                       # no key says "question"
                       ({"theQuestion": "Ship it?", "other": "x"}, "Ship it?"),
                       ({"q": "Pick one", "choices": ["a", "b"]}, "Pick one"),     # a list that isn't options
                       ({"question": "a" * 700}, question_text([{"question": "a" * 700}])),
                       ("just a string", "just a string"), (None, "(question)")):
        for p in agyhome.spool.glob("*.json"):
            p.unlink()
        _run("PreToolUse", json.dumps(_ev("PreToolUse", toolCall={"name": TOOL, "args": args, "id": "t"})), capsys)
        (rec,) = _records(agyhome.spool)
        assert question_text(rec["data"]["questions"]) == want, args


# -- the other events answer {} unless they have something to inject --------------------------------------------
def test_post_tool_use_and_post_invocation_are_silent(agyhome, capsys):
    assert _run("PostToolUse", json.dumps(_ev("PostToolUse", toolCall={"name": "Bash", "args": {}}, stepIdx=1)), capsys) == {}
    assert _run("PostInvocation", json.dumps(_ev("PostInvocation", invocationNum=0)), capsys) == {}


def test_post_tool_use_of_the_question_tool_is_spooled(agyhome, capsys):
    _run("PostToolUse", json.dumps(_ev("PostToolUse", toolCall={"name": TOOL, "args": {}}, stepIdx=2)), capsys)
    _run("PostToolUse", json.dumps(_ev("PostToolUse", toolCall={"name": "Bash"}, error="boom")), capsys)
    (rec,) = _records(agyhome.spool)
    assert rec["event"] == "PostToolUse" and rec["data"]["tool_name"] == TOOL


def test_stop_is_spooled_and_carries_why_the_turn_ended(agyhome, capsys):
    assert _run("Stop", json.dumps(_ev("Stop", terminationReason="NO_TOOL_CALL", fullyIdle=True)), capsys) == {}
    (rec,) = _records(agyhome.spool)
    assert rec["event"] == "Stop" and rec["data"] == {"reason": "NO_TOOL_CALL", "cwd": "/r", "transcript_path": "/t.json"}


def test_pre_invocation_is_never_spooled(agyhome, capsys):
    _run("PreInvocation", json.dumps(_ev("PreInvocation", invocationNum=0, initialNumSteps=0, lastUserInput=PROMPT)), capsys)
    assert _records(agyhome.spool) == []


def test_an_unknown_event_or_a_bad_conversation_id_spools_nothing(agyhome, capsys):
    assert _run("PreCompress", json.dumps(_ev("PreCompress")), capsys) == {}
    assert hook.handle_agy("Stop", _ev("Stop", conversationId="../x"), agyhome.spool) == {}
    assert hook.handle_agy("PreInvocation", _ev("PreInvocation", conversationId=""), agyhome.spool) == {}
    assert _records(agyhome.spool) == []


# -- the agy inbox ----------------------------------------------------------------------------------------------
def test_pre_invocation_injects_each_inbox_message_once(agyhome, capsys):
    p = _inbox("first line", "second line")
    out = _run("PreInvocation", json.dumps(_ev("PreInvocation", invocationNum=1)), capsys)
    assert out == {"injectSteps": [{"userMessage": "first line"}, {"userMessage": "second line"}]}
    assert not p.exists() and list(agyhome.inbox.iterdir()) == []         # claimed whole, then deleted
    assert _run("PreInvocation", json.dumps(_ev("PreInvocation", invocationNum=1)), capsys) == {}


def test_the_inbox_skips_unparseable_lines_and_keeps_order(agyhome, capsys):
    p = _inbox(raw='{"msg_id": 1, "text": "one"}\n{half\nnot json\n{"msg_id": 4}\n'
                   '{"msg_id": 5, "text": "   "}\n{"msg_id": 6, "text": "two"}\n')
    assert _run("Stop", json.dumps(_ev("Stop")), capsys) == {"decision": "continue", "reason": "one\n\ntwo"}
    assert not p.exists()


@pytest.mark.parametrize("sid", ["../escape", "", None, "a/b", "a\\b", "."])
def test_a_conversation_id_that_cannot_name_a_file_reads_nothing(agyhome, sid):
    p = _inbox("a real message")                                         # another conversation's, not this one's
    assert hook.handle_agy("Stop", _ev("Stop", conversationId=sid), agyhome.spool) == {}
    assert p.exists()                                                    # untouched: not ours to take


def test_stop_continues_the_turn_with_what_is_waiting(agyhome, capsys):
    _inbox("operator says: use the other branch")
    assert _run("Stop", json.dumps(_ev("Stop", terminationReason="NO_TOOL_CALL")), capsys) == \
        {"decision": "continue", "reason": "operator says: use the other branch"}
    assert [r["event"] for r in _records(agyhome.spool)] == ["Stop"]      # and it is spooled too
    assert _run("Stop", json.dumps(_ev("Stop")), capsys) == {}


def test_the_inbox_s_text_is_never_printed_anywhere_else(agyhome, capsys, monkeypatch):
    """Only the injected step may carry it: the spool record of the same event holds none of it."""
    _inbox("secret: the kitchen code is 4")
    _run("PreInvocation", json.dumps(_ev("PreInvocation", invocationNum=1)), capsys)
    assert _records(agyhome.spool) == []
    _inbox("secret: the study code is 5")
    _run("Stop", json.dumps(_ev("Stop")), capsys)
    assert "study" not in json.dumps(_records(agyhome.spool))


# -- the resume briefing ----------------------------------------------------------------------------------------
def test_session_start_injects_the_brief(agyhome, capsys, monkeypatch):
    seen = []

    def fake(harness, sid, cfg=None):
        seen.append((harness, sid))
        return "the brief"

    monkeypatch.setattr(hook, "fetch_brief", fake)
    assert _run("SessionStart", json.dumps(_ev("SessionStart")), capsys) == \
        {"injectSteps": [{"systemMessage": {"systemMessage": "the brief"}}]}
    assert seen == [("agy", SID)]
    assert [r["event"] for r in _records(agyhome.spool)] == ["SessionStart"]


def test_session_start_says_nothing_when_the_hub_has_nothing_to_say(agyhome, capsys, monkeypatch):
    monkeypatch.setattr(hook, "fetch_brief", lambda *a, **k: None)
    assert _run("SessionStart", json.dumps(_ev("SessionStart")), capsys) == {}
    monkeypatch.setattr(hook, "fetch_brief", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("no hub")))
    assert _run("SessionStart", json.dumps(_ev("SessionStart")), capsys) == {}


def test_session_start_with_a_hub_that_is_down_is_silence_and_fast(agyhome, capsys, monkeypatch):
    cfg = Cfg(); cfg.hub.url = "http://127.0.0.1:9"                       # nothing listens there
    monkeypatch.setattr("ctxswitch.common.config.load", lambda *a: cfg)
    t0 = time.monotonic()
    assert _run("SessionStart", json.dumps(_ev("SessionStart")), capsys) == {}
    assert time.monotonic() - t0 < hook.BRIEF_TIMEOUT_S + 0.5


# -- the misplaced-prompt check on a turn's first model call ------------------------------------------------------
def test_invocation_zero_with_a_misplaced_prompt_blocks_the_turn(agyhome, monkeypatch, capsys):
    hub = _hub(monkeypatch, OTHER)
    out = _run("PreInvocation", json.dumps(_ev("PreInvocation", invocationNum=0, lastUserInput=PROMPT)), capsys)
    assert out == {"injectSteps": [{"errorMessage": {"userMessage": REASON}}], "terminationBehavior": "terminate"}
    assert hub.checks == [("gpu-host/agy/conv-1", PROMPT, None, "/r")]


@pytest.mark.parametrize("invocation", [1, 2, None])
def test_any_later_model_call_never_asks_the_hub(agyhome, monkeypatch, capsys, invocation):
    hub = _hub(monkeypatch, OTHER)
    raw = json.dumps(_ev("PreInvocation", lastUserInput=PROMPT) | ({} if invocation is None else {"invocationNum": invocation}))
    assert _run("PreInvocation", raw, capsys) == {}
    assert hub.checks == []


@pytest.mark.parametrize("prompt", ["yes do it", "/compact", "", "   ", "\n  ",
                                   "Message from the operator (sent with ctxswitch)\nrename it now",
                                   "Message from another agent session, not from the operator\nrename it now"])
def test_short_slash_and_injected_prompts_are_never_asked_about(agyhome, monkeypatch, capsys, prompt):
    hub = _hub(monkeypatch, OTHER)
    assert _run("PreInvocation", json.dumps(_ev("PreInvocation", invocationNum=0, lastUserInput=prompt)), capsys) == {}
    assert hub.checks == []


@pytest.mark.parametrize("reply", [{"verdict": "ok"}, None, "nonsense", {"verdict": "drift"}])
def test_anything_but_other_lets_the_turn_run(agyhome, monkeypatch, capsys, reply):
    _hub(monkeypatch, reply)
    assert _run("PreInvocation", json.dumps(_ev("PreInvocation", invocationNum=0, lastUserInput=PROMPT)), capsys) == {}


def test_a_dead_hub_lets_the_turn_run(agyhome, monkeypatch, capsys):
    _hub(monkeypatch, raises=ConnectionError("no hub here"))
    assert _run("PreInvocation", json.dumps(_ev("PreInvocation", invocationNum=0, lastUserInput=PROMPT)), capsys) == {}


def test_a_hub_that_hangs_still_answers_in_under_two_seconds(agyhome, monkeypatch, capsys):
    class Hanging:
        def prompt_check(self, *a):
            time.sleep(5)
            return OTHER

    monkeypatch.setattr("ctxswitch.common.config.load", lambda *a: Cfg())
    monkeypatch.setattr("ctxswitch.client.hubclient.HubClient", lambda cfg: Hanging())
    t0 = time.monotonic()
    assert _run("PreInvocation", json.dumps(_ev("PreInvocation", invocationNum=0, lastUserInput=PROMPT)), capsys) == {}
    assert time.monotonic() - t0 < 2.0


def test_inbox_messages_beat_the_prompt_check(agyhome, monkeypatch, capsys):
    hub = _hub(monkeypatch, OTHER)
    _inbox("waiting message")
    out = _run("PreInvocation", json.dumps(_ev("PreInvocation", invocationNum=0, lastUserInput=PROMPT)), capsys)
    assert out == {"injectSteps": [{"userMessage": "waiting message"}]} and hub.checks == []


def test_the_same_prompt_blocked_twice_is_a_re_send_and_goes_through(agyhome, monkeypatch, capsys):
    """Claude's rule, shared: a blocked prompt sent again within the window is the operator insisting."""
    hub = _hub(monkeypatch, OTHER)
    monkeypatch.setattr(hook, "_resend", lambda cfg, row_id: None)
    raw = json.dumps(_ev("PreInvocation", invocationNum=0, lastUserInput=PROMPT))
    assert "terminate" in json.dumps(_run("PreInvocation", raw, capsys))
    assert _run("PreInvocation", raw, capsys) == {} and len(hub.checks) == 1


# -- the hub's side ----------------------------------------------------------------------------------------------
def test_agy_question_ask_is_opened_resolved_and_closed(tmp_path):
    from test_hooks import Feed
    ak = "laptop/agy/conv-1"
    f = Feed(tmp_path)
    f.store.upsert_session({"key": ak, "host_id": "laptop", "harness": "agy", "session_id": "conv-1",
                            "last_activity": "2026-10-28T10:00:00Z"})
    f.store.set_liveness("laptop", [{"key": ak, "status": "busy"}])
    assert f("PreToolUse", 1, harness="agy", sid="conv-1", tool_name="Bash", tool_input={"command": "ls"})
    assert f.attention() == []                                            # not the question tool: no ask
    assert f("PreToolUse", 2, harness="agy", sid="conv-1", tool_name=TOOL, tool_use_id="call_1",
             questions=[{"question": "Red or blue?", "options": [{"label": "Red"}]}],
             tool_input={"question": "Red or blue?", "options": ["Red", "Blue"]})
    assert f.attention() == [("question", "Red or blue?")]
    assert f("PostToolUse", 3, harness="agy", sid="conv-1", tool_name=TOOL, tool_use_id="call_1")
    assert f.asks(ak)[0]["answer"] == "answered" and f.attention() == []
    f("PreToolUse", 4, harness="agy", sid="conv-1", tool_name=TOOL, tool_use_id="call_2", questions=[{"question": "Which branch?"}])
    f("Stop", 5, harness="agy", sid="conv-1", reason="NO_TOOL_CALL")      # answered in the transcript, or Esc'd
    assert [a["answer"] for a in f.asks(ak)] == ["answered", "closed: turn ended"]
    f("PreToolUse", 6, harness="agy", sid="conv-1", tool_name=TOOL, questions=[{"question": "No id?"}])
    f("SessionStart", 7, harness="agy", sid="conv-1")
    assert [a["answer"] for a in f.asks(ak)][2:] == ["closed: session restarted"]


def test_agy_events_apply_once(tmp_path):
    from test_hooks import Feed
    f = Feed(tmp_path)
    kw = dict(harness="agy", sid="conv-1", name="a.json", tool_name=TOOL, questions=[{"question": "A?"}])
    assert f("PreToolUse", 1, **kw) and f("PreToolUse", 1, **kw) is False


def test_the_agy_spool_record_reaches_the_hub_as_an_ask(tmp_path):
    """hook.handle_agy → spool file → the collector's payload shape → the hub's ask, with no hand-written data."""
    from test_hooks import Feed
    spool = tmp_path / "spool"; spool.mkdir()
    hook.handle_agy("PreToolUse", {"conversationId": "conv-1", "toolCall": {"name": TOOL, "id": "c9",
                                                                            "args": {"question": "Deploy now?"}}}, spool)
    f = Feed(tmp_path)
    f.store.upsert_session({"key": "laptop/agy/conv-1", "host_id": "laptop", "harness": "agy", "session_id": "conv-1"})
    f.store.set_liveness("laptop", [{"key": "laptop/agy/conv-1", "status": "busy"}])
    rec = json.loads(next(spool.glob("*.json")).read_text())
    assert f.hooks.apply("laptop", {"name": "1-1.json", "ts": rec["ts"], **rec})
    assert f.attention() == [("question", "Deploy now?")]


def test_pre_tool_use_s_shell_line_answers_ask_even_when_ctx_hook_cant_start(tmp_path):
    """agy denies a tool when its hook prints nothing — so the installed shell line falls back to "ask" by itself
    when ctx-hook prints nothing (a broken venv), and otherwise passes its answer through, stdin included."""
    import subprocess
    from ctxswitch.client import install as ins
    silent, echo = tmp_path / "silent", tmp_path / "echo"
    silent.write_text("#!/bin/sh\nexit 1\n"); silent.chmod(0o755)
    echo.write_text("#!/bin/sh\ncat >/dev/null; echo '{\"decision\":\"allow\"}'\n"); echo.chmod(0o755)
    for exe, want in ((tmp_path / "missing", {"decision": "ask"}), (silent, {"decision": "ask"}), (echo, {"decision": "allow"})):
        line = ins.agy_event_command(f"{exe} agy", "PreToolUse")
        r = subprocess.run(["sh", "-c", line], input='{"conversationId": "c"}', capture_output=True, text=True, timeout=10)
        assert json.loads(r.stdout) == want, (exe, r.stdout, r.stderr)
    assert ins.agy_event_command("/x/ctx-hook agy", "Stop") == "/x/ctx-hook agy Stop"     # the others: plain


def test_main_still_answers_agy_when_reading_stdin_itself_fails(monkeypatch, capsys):
    from ctxswitch import hook
    class Broken:
        def read(self, n=-1):
            raise OSError("stdin closed")
    monkeypatch.setattr(hook.sys, "stdin", Broken())
    assert hook.main(["agy", "PreToolUse"]) == 0 and json.loads(capsys.readouterr().out) == {"decision": "ask"}
    assert hook.main(["agy", "Stop"]) == 0 and json.loads(capsys.readouterr().out) == {}


def test_ask_question_s_real_args_become_the_board_s_question(tmp_path):
    """agy 1.5.12's ask_question, as captured end to end: Claude's AskUserQuestion shape."""
    from ctxswitch import hook
    d = {"conversationId": "c1", "toolCall": {"name": "ask_question", "id": "call_1", "args": {"questions": [
        {"is_multi_select": False, "options": ["Tea", "Coffee"], "question": "Do you prefer tea or coffee?"}]}}}
    assert hook._agy_data(d)["questions"] == [{"question": "Do you prefer tea or coffee?", "options": ["Tea", "Coffee"]}]
    from ctxswitch.common.schema import question_text
    assert question_text(hook._agy_data(d)["questions"]) == "Do you prefer tea or coffee?"
