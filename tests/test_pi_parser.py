from conftest import feed_all

from ctxswitch.collector.adapters.pi import PiParser

T = "2026-10-28T10:00:{:02d}.000Z"


def session(i="01a1-id", cwd="/home/rick/repos/demo"):
    return {"type": "session", "version": 3, "id": i, "timestamp": T.format(0), "cwd": cwd}


def msg(role, content, i, parent, sec, **extra):
    m = {"role": role, "content": content, "timestamp": sec}
    m.update(extra)
    return {"type": "message", "id": i, "parentId": parent, "timestamp": T.format(sec), "message": m}


def test_pi_turns_tally_branch_and_plain_ask():
    rows = [
        session(),
        {"type": "model_change", "id": "m1", "parentId": None, "timestamp": T.format(0), "provider": "saint", "modelId": "saint-auto"},
        msg("user", [{"type": "text", "text": "add a flag"}], "u1", "m1", 1),
        msg("assistant", [{"type": "thinking", "thinking": "..."}, {"type": "toolCall", "id": "c1", "name": "read", "arguments": {}}], "a1", "u1", 2,
            model="qwen", provider="saint", stopReason="toolUse"),
        msg("toolResult", [{"type": "text", "text": "x"}], "r1", "a1", 3, toolName="read", toolCallId="c1", isError=False),
        msg("assistant", [{"type": "toolCall", "id": "c2", "name": "edit", "arguments": {}}, {"type": "toolCall", "id": "c3", "name": "bash", "arguments": {}}], "a2", "r1", 4, stopReason="toolUse"),
        msg("toolResult", [{"type": "text", "text": "err"}], "r2", "a2", 5, toolName="bash", toolCallId="c3", isError=True),
        msg("assistant", [{"type": "text", "text": "Added --flag. Want a test for it?"}], "a3", "r2", 6, stopReason="stop"),
        msg("user", [{"type": "text", "text": "yes"}], "u2", "a3", 7),
        # branch: a user message re-rooted at a1 (pi tree edit)
        msg("user", [{"type": "text", "text": "actually, rename it"}], "u3", "a1", 8),
        {"type": "compaction", "id": "k1", "parentId": "u3", "timestamp": T.format(9), "summary": "s", "firstKeptEntryId": "u3", "tokensBefore": 1},
        msg("assistant", [{"type": "text", "text": "Renamed."}], "a4", "k1", 10, stopReason="stop"),
    ]
    p = feed_all(PiParser(), rows)
    p.finish()
    turns = p.drain()
    assert [t.user_text for t in turns] == ["add a flag", "yes", "actually, rename it"]
    t0 = turns[0]
    assert (t0.tally.reads, t0.tally.edits, t0.tally.bash, t0.tally.errors) == (1, 1, 1, 1)
    assert t0.assistant_text == "Added --flag. Want a test for it?"
    assert t0.asks[0].kind == "plain" and t0.asks[0].answered and t0.asks[0].answer == "yes"
    assert "branch" in turns[2].events and "compaction" in turns[2].events
    assert turns[2].assistant_text == "Renamed." and all(t.complete for t in turns)
    assert p.meta["session_id"] == "01a1-id" and p.meta["cwd"] == "/home/rick/repos/demo"
    assert p.meta["model"] == "saint/qwen" and p.meta["first_prompt"] == "add a flag"
    assert p.meta["started_at"] == T.format(0) and p.meta["last_activity"] == T.format(10)


def test_pi_state_roundtrip():
    import json
    rows = [session(), msg("user", [{"type": "text", "text": "q"}], "u1", None, 1)]
    p = feed_all(PiParser(), rows)
    p2 = PiParser.from_state(json.loads(json.dumps(p.state())))
    p2.feed(msg("assistant", [{"type": "text", "text": "a"}], "a1", "u1", 2, stopReason="stop"), 300)
    p2.finish()
    t = p2.drain()[0]
    assert t.user_text == "q" and t.assistant_text == "a" and t.offset_end == 300 and "branch" not in t.events


def test_pi_last_turn_completes_when_the_run_stops_not_on_a_tool_pause():
    p = feed_all(PiParser(), [session(), msg("user", [{"type": "text", "text": "go"}], "u1", None, 1),
                              msg("assistant", [{"type": "toolCall", "id": "c1", "name": "read", "arguments": {}}], "a1", "u1", 2, stopReason="toolUse")])
    assert not p.current.complete                                   # waiting on a tool result: still working
    p.feed(msg("toolResult", [{"type": "text", "text": "x"}], "r1", "a1", 3, toolName="read", toolCallId="c1", isError=False), 400)
    p.feed(msg("assistant", [{"type": "text", "text": "Done."}], "a2", "r1", 4, stopReason="stop"), 500)
    assert p.current.complete and p.drain() == []                   # no next prompt needed; the turn stays open
    p.feed(msg("assistant", [{"type": "toolCall", "id": "c2", "name": "bash", "arguments": {}}], "a3", "a2", 5, stopReason="toolUse"), 600)
    assert not p.current.complete                                   # a follow-up run reopens it
