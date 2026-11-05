"""hc_ask: the dialog herdr-core reports (OSC 7501 on a blocked terminal) becomes the session's open ask, so a
herdr-only agent — and a Claude session whose hook never fired — says what it is waiting on."""
from ctxswitch.client.tui import app as tui
from ctxswitch.hub.board import board
from ctxswitch.hub.store import Store

NOW = "2026-10-29T10:00:00Z"
LATER = "2026-10-29T10:05:00Z"
HERDR = "laptop/herdr/t2"
CLAUDE = "laptop/claude/abc"
ASK = {"kind": "question", "text": "Ship it?"}


def _snap(store, key=HERDR, status="waiting", ask=ASK, hc_at="t2", since=NOW):
    item = {"key": key, "status": status, "status_since": since, "hc_at": hc_at}
    if ask is not None:
        item["hc_ask"] = dict(ask)
    store.set_liveness("laptop", [item])


def _asks(store, key=HERDR):
    return store.session(key)["open_asks"]


def test_a_blocked_herdr_terminal_opens_an_ask_the_board_can_name(tmp_path):
    store = Store(tmp_path / "hub.db")
    _snap(store)
    (a,) = _asks(store)
    assert (a["ref"], a["kind"], a["text"], a["answered"], a["asked_at"]) == ("hc:t2", "question", "Ship it?", 0, NOW)
    assert [x["ref"] for x in store.attention("laptop") if x["item"] == "ask"] == ["hc:t2"]

    # a second poll of the same dialog keeps the wait's start: the board's ◐age is when it began asking
    _snap(store)
    assert len(store.session(HERDR)["asks"]) == 1
    assert store.session(HERDR)["open_asks"][0]["asked_at"] == NOW

    # the dialog re-worded (the agent re-asked in the same terminal) says the new thing, still open
    _snap(store, ask={"kind": "question", "text": "Ship it? (2 of 2)"})
    (a,) = _asks(store)
    assert a["text"] == "Ship it? (2 of 2)" and a["asked_at"] == NOW


def test_the_ask_closes_when_the_dialog_is_gone(tmp_path):
    store = Store(tmp_path / "hub.db")
    _snap(store)
    _snap(store, status="idle", ask=None, since=LATER)
    assert _asks(store) == []
    (a,) = store.session(HERDR)["asks"]
    assert (a["answered"], a["answer"]) == (1, "closed: dialog gone")
    assert a["answered_at"] is not None                   # closed just now, not back at the wait's start
    # what is left is the session itself at its prompt, not an ask
    assert not [x for x in store.attention("laptop") if x["item"] == "ask"]

    # and it can open again: the same terminal asking later is a fresh ask, stamped now
    _snap(store, ask={"kind": "permission", "text": "Run rm -rf?"}, since=LATER)
    (a,) = _asks(store)
    assert (a["ref"], a["kind"], a["asked_at"]) == ("hc:t2", "permission", LATER)


def test_a_session_that_left_the_snapshot_ends_and_closes_its_ask(tmp_path):
    store = Store(tmp_path / "hub.db")
    _snap(store)
    store.set_liveness("laptop", [])                  # nothing live on this host now
    s = store.session(HERDR)
    assert (s["live_status"], s["ended"], s["open_asks"]) == ("ended", True, [])
    assert s["asks"][0]["answer"] == "closed: dialog gone"
    assert store.attention("laptop") == []


def test_a_claude_dialog_herdr_saw_shows_on_the_board(tmp_path):
    store = Store(tmp_path / "hub.db")
    store.upsert_session({"key": CLAUDE, "host_id": "laptop", "harness": "claude", "session_id": "abc",
                          "title": "T", "cwd": "/r", "last_activity": NOW})
    _snap(store, key=CLAUDE, hc_at="t7")
    (a,) = _asks(store, CLAUDE)
    assert (a["ref"], a["kind"], a["text"]) == ("hc:t7", "question", "Ship it?")
    s = store.session(CLAUDE)
    assert tui.waiting_on(s) == "question: Ship it?"
    (r,) = [r for r in board(store)["rows"] if r["key"] == CLAUDE]
    assert (r["state"], r["waiting_on"]) == ("waiting", tui.waiting_on(s))

    # dismissed (Esc fires no hook): the hc ask goes with the hook's, so the board stops claiming a dialog
    _snap(store, key=CLAUDE, status="idle", ask=None, hc_at="t7", since=LATER)
    assert _asks(store, CLAUDE) == []
