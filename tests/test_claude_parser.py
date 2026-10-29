from claude_rows import S, assistant, meta, system, text, tool_result, tool_use, user
from conftest import feed_all

from ctxswitch.collector.adapters.claude import ClaudeParser


def test_two_turns_with_tally_and_turn_duration():
    rows = [
        user("fix the flaky test", "2026-10-28T10:00:00Z"),
        assistant([text("Looking."), tool_use("Read", "t1", {"file_path": "a.py"})], "2026-10-28T10:00:05Z"),
        tool_result("t1", "contents", "2026-10-28T10:00:06Z"),
        assistant([tool_use("Edit", "t2"), tool_use("Bash", "t3", {"command": "pytest"})], "2026-10-28T10:00:10Z"),
        tool_result("t2", "ok", "2026-10-28T10:00:11Z"),
        tool_result("t3", "1 failed", "2026-10-28T10:00:12Z", is_error=True),
        assistant([text("Fixed the import; tests pass now.")], "2026-10-28T10:00:20Z"),
        system("turn_duration", "2026-10-28T10:00:21Z", durationMs=21000),
        meta("ai-title", aiTitle="Flaky test fix"),
        user("great, now push", "2026-10-28T10:05:00Z"),
        assistant([text("Pushed.")], "2026-10-28T10:05:10Z"),
    ]
    p = feed_all(ClaudeParser(), rows)
    turns = p.drain()
    assert len(turns) == 1, "second turn is still open"
    t0 = turns[0]
    assert t0.user_text == "fix the flaky test" and t0.user_origin == "human"
    assert t0.assistant_text == "Fixed the import; tests pass now."
    assert (t0.tally.reads, t0.tally.edits, t0.tally.bash, t0.tally.errors) == (1, 1, 1, 1)
    assert t0.complete and t0.ended_at == "2026-10-28T10:00:21Z"
    assert t0.asks == []
    assert p.current is not None and p.current.user_text == "great, now push" and not p.current.complete
    assert p.meta["title"] == "Flaky test fix" and p.meta["git_branch"] == "feat/x" and p.meta["session_id"] == S
    assert p.meta["model"] == "claude-opus-5-5" and p.meta["harness_version"] == "2.1.280"
    assert p.meta["started_at"] == "2026-10-28T10:00:00Z" and p.meta["last_activity"] == "2026-10-28T10:05:10Z"
    p.finish()
    assert p.drain()[0].complete


def test_ask_user_question_and_answer():
    q = {"questions": [{"question": "Which DB?", "header": "DB", "options": [{"label": "pg", "description": ""}], "multiSelect": False}]}
    rows = [
        user("design the store", "2026-10-28T10:00:00Z"),
        assistant([tool_use("AskUserQuestion", "q1", q)], "2026-10-28T10:00:05Z"),
    ]
    p = feed_all(ClaudeParser(), rows)
    cur = p.current
    assert cur is not None and len(cur.asks) == 1
    ask = cur.asks[0]
    assert ask.kind == "question" and ask.text == "Which DB?" and not ask.answered
    p.feed(tool_result("q1", 'User has answered your questions: "Which DB?"="pg".', "2026-10-28T10:01:00Z",
                       tur={"questions": q["questions"], "answers": {"Which DB?": "pg"}, "annotations": {}}), 300)
    assert ask.answered and ask.answer == "Which DB?: pg" and ask.answered_at == "2026-10-28T10:01:00Z"
    assert cur.tally.other == 1


def test_plan_rejection_feedback_and_approval():
    rows = [
        user("plan it", "2026-10-28T10:00:00Z"),
        assistant([tool_use("ExitPlanMode", "p1")], "2026-10-28T10:00:05Z"),
        tool_result("p1", "The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if it was a "
                    "file edit, the new_string was NOT written to the file). To tell you how to proceed, the user said:\n"
                    "I'm still reviewing, make it multi-machine", "2026-10-28T10:01:00Z", is_error=True),
        assistant([tool_use("ExitPlanMode", "p2")], "2026-10-28T10:02:00Z"),
        tool_result("p2", "User has approved your plan.", "2026-10-28T10:03:00Z",
                    tur={"plan": "...", "filePath": "/x", "isAgent": False, "hasTaskTool": True}),
    ]
    p = feed_all(ClaudeParser(), rows)
    asks = p.current.asks
    assert [a.kind for a in asks] == ["plan", "plan"]
    assert asks[0].answered and asks[0].answer == "rejected: I'm still reviewing, make it multi-machine"
    assert asks[1].answered and asks[1].answer == "approved"


def test_plain_question_is_an_ask_until_next_prompt():
    rows = [
        user("do X", "2026-10-28T10:00:00Z"),
        assistant([text("Done with X.\nShall I also do Y?")], "2026-10-28T10:00:05Z"),
        system("turn_duration", "2026-10-28T10:00:06Z"),
    ]
    p = feed_all(ClaudeParser(), rows)
    assert p.current.complete and p.current.asks == []          # plain asks are added at close
    p.feed(user("yes, do Y", "2026-10-28T10:10:00Z"), 400)
    t0 = p.drain()[0]
    assert len(t0.asks) == 1 and t0.asks[0].kind == "plain" and t0.asks[0].text == "Shall I also do Y?"
    assert t0.asks[0].answered and t0.asks[0].answer == "yes, do Y"


def test_skips_sidechain_meta_and_marks_peer_compaction_and_commands():
    rows = [
        user("<command-name>/compact</command-name><command-message>compact</command-message>", "2026-10-28T10:00:00Z"),
        system("compact_boundary", "2026-10-28T10:00:01Z", compactMetadata={"trigger": "manual"}),
        user("summary text", "2026-10-28T10:00:02Z", isCompactSummary=True),
        user("subagent prompt", "2026-10-28T10:00:03Z", isSidechain=True),
        assistant([text("subagent reply")], "2026-10-28T10:00:04Z").__class__(
            **{**assistant([text("subagent reply")], "2026-10-28T10:00:04Z"), "isSidechain": True}),
        user("ignored meta input", "2026-10-28T10:00:05Z", isMeta=True),
        user("ctxswitch spike ping", "2026-10-28T10:00:06Z", isMeta=True, origin={"kind": "peer", "from": "uds:/x"}),
        assistant([text("ack")], "2026-10-28T10:00:07Z"),
        system("api_error", "2026-10-28T10:00:08Z", error="rate"),
    ]
    p = feed_all(ClaudeParser(), rows)
    p.finish()
    turns = p.drain()
    # the /compact wrapper is an operator action: it opens no turn, its events ride on the next prompt's turn
    assert [t.user_origin for t in turns] == ["peer"]
    ev = turns[0].events
    assert "slash_command:/compact" in ev and ev.count("compaction") == 2 and "meta_input" in ev
    assert turns[0].user_text == "ctxswitch spike ping" and turns[0].assistant_text == "ack"   # no sidechain leak
    assert "api_error" in ev


def test_state_roundtrip_mid_turn():
    rows = [user("start", "2026-10-28T10:00:00Z"), assistant([tool_use("AskUserQuestion", "q1", {"questions": [{"question": "A or B?"}]})], "2026-10-28T10:00:01Z")]
    p = feed_all(ClaudeParser(), rows)
    st = p.state()
    import json
    p2 = ClaudeParser.from_state(json.loads(json.dumps(st)))
    p2.feed(tool_result("q1", "A", "2026-10-28T10:00:02Z", tur={"answers": {"A or B?": "A"}}), 300)
    assert p2.current.asks[0].answered and p2.current.asks[0].answer == "A or B?: A"
    assert p2.current.offset_end == 300


def test_exit_only_session_has_no_turns_and_bash_input_is_a_turn():
    rows = [
        user("<local-command-caveat>Caveat: generated</local-command-caveat>", "2026-10-28T10:00:00Z"),
        user("<command-name>/exit</command-name><command-message>exit</command-message>", "2026-10-28T10:00:01Z"),
        user("<local-command-stdout>See ya!</local-command-stdout>", "2026-10-28T10:00:02Z"),
    ]
    p = feed_all(ClaudeParser(), rows); p.finish()
    assert p.drain() == [] and p.current is None and "first_prompt" not in p.meta
    rows = [
        user("<bash-input>hostname</bash-input>", "2026-10-28T10:00:03Z"),     # `!` shell input: the agent replies to it
        assistant([text("That's the Windows name.")], "2026-10-28T10:00:04Z"),
        user("real first ask\nsecond line", "2026-10-28T10:00:05Z"),
    ]
    p = feed_all(ClaudeParser(), rows)
    t0 = p.drain()[0]
    assert t0.user_text.startswith("<bash-input>") and t0.assistant_text == "That's the Windows name."
    assert p.meta["first_prompt"] == "real first ask"


def test_task_notification_prompt_is_task_origin():
    rows = [user("<task-notification>agent finished</task-notification>", "2026-10-28T10:00:00Z"),
            assistant([text("Continuing.")], "2026-10-28T10:00:01Z")]
    p = feed_all(ClaudeParser(), rows)
    assert p.current.user_origin == "task" and "first_prompt" not in p.meta


def test_interrupt_marker_is_an_event_not_a_prompt():
    marker = [{"type": "text", "text": "[Request interrupted by user for tool use]"}]
    rows = [user("run touch x", "2026-10-28T10:00:00Z"),
            assistant([tool_use("Bash", "t1", {"command": "touch x"})], "2026-10-28T10:00:01Z"),
            tool_result("t1", "The user doesn't want to proceed", "2026-10-28T10:00:05Z", is_error=True),
            user(marker, "2026-10-28T10:00:05Z"),
            user("[Request interrupted by user]", "2026-10-28T10:00:06Z")]
    p = feed_all(ClaudeParser(), rows)
    assert p.drain() == [] and p.current.user_text == "run touch x"
    assert p.current.events.count("interrupted") == 2
