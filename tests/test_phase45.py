"""Quick-reply to an ask, "since you last looked", cross-host workstreams and tmux pop-ups."""
import os
import subprocess
import time
from types import SimpleNamespace

from starlette.testclient import TestClient

from ctxswitch.client import fmt
from ctxswitch.client.notify import BOX_CMD, OPEN_RC, Notifier, box_lines, text, tmux_box, tmux_popup
from ctxswitch.client.tui import app as tui
from ctxswitch.collector import deliver
from ctxswitch.collector.gitinfo import branch
from ctxswitch.hub.api import Hub
from ctxswitch.hub.store import Store, workstream_of

KEY = "laptop/claude/s1"
H = {"Authorization": "Bearer t"}
WRAP = "Another Claude session sent a message:\n" + deliver.HEADER     # how Claude shows a ctx message


def _session(store, key=KEY, live=True, remote="git@github.com:me/proj.git", branch_="main", root="/r", when="2026-10-29T10:00:00Z"):
    h, harness, sid = key.split("/")
    store.upsert_session({"key": key, "host_id": h, "harness": harness, "session_id": sid, "title": sid, "cwd": root,
                          "repo_root": root, "repo_remote": remote, "git_branch": branch_, "last_activity": when})
    if live:
        store.set_liveness(h, [{"key": key, "status": "idle", "status_since": when, "pid": 1, "socket": "/x"}])


def _turn(idx, user, agent="", t="2026-10-29T10:00:00Z", origin="human", asks=(), complete=True, end=None):
    return {"idx": idx, "started_at": t, "ended_at": end or t, "user_text": user, "user_origin": origin, "assistant_text": agent,
            "tally": {}, "asks": list(asks), "events": [], "complete": complete, "offset_end": idx * 100 + 10}


def _ask(ref, text, kind="plain", answer=None, at=None):
    return {"ref": ref, "kind": kind, "text": text, "answered": answer is not None, "answer": answer, "answered_at": at}


Q = "Tabs or spaces?"
T0, T1 = "2026-10-29T10:00:00Z", "2026-10-29T10:05:00Z"


def test_quick_reply_records_what_you_wrote_not_the_wrapper(tmp_path):
    store = Store(tmp_path / "hub.db")
    _session(store)
    store.upsert_turns(KEY, [_turn(0, "set up the repo", Q, asks=[_ask("0:plain", Q)])], None)
    _session(store, "devbox/claude/s2")                             # another session's "0:plain" with an answer queued
    store.upsert_turns("devbox/claude/s2", [_turn(0, "go", Q, asks=[_ask("0:plain", Q)])], None)
    c = TestClient(Hub(store, token="t").app())
    assert c.post("/v1/sessions/devbox/claude/s2/messages", headers=H, json={"body": "tabs", "answers": "0:plain"}).status_code == 200
    url = f"/v1/sessions/{KEY}/messages"
    mid = c.post(url, headers=H, json={"body": "spaces,  4 of them", "answers": "0:plain"}).json()["id"]
    assert c.post(url, headers=H, json={"body": "tabs", "answers": "0:plain"}).status_code == 409     # one on its way
    store.set_message_status(mid, "laptop", "sent")
    # the collector closes the ask with whatever Claude recorded as the next prompt: the wrapped message
    wrapped = WRAP + "spaces,  4 of them"
    store.upsert_turns(KEY, [_turn(0, "set up the repo", Q, asks=[_ask("0:plain", Q, answer=wrapped, at=T1)]),
                             _turn(1, wrapped, "ok", t=T1, origin="peer")], None)
    (a,) = store.session(KEY)["asks"]
    assert (a["answered"], a["answer"]) == (1, "spaces, 4 of them")
    store.upsert_turns(KEY, [_turn(0, "set up the repo", Q, asks=[_ask("0:plain", Q, answer=wrapped, at=T1)])], None)
    assert store.session(KEY)["asks"][0]["answer"] == "spaces, 4 of them"        # a replayed batch doesn't undo it
    s = store.session(KEY)
    assert (Q, "spaces, 4 of them") in [(q, ans) for _, q, ans in fmt.decisions(s)]
    assert s["messages"][0]["ask_ref"] == "0:plain"
    assert c.post(url, headers=H, json={"body": "x", "answers": "0:plain"}).status_code == 409     # answered now


def test_a_typed_answer_is_not_rewritten(tmp_path):
    store = Store(tmp_path / "hub.db")
    _session(store)
    Q2, T2 = "Width 80?", "2026-10-29T10:09:00Z"
    store.upsert_turns(KEY, [_turn(0, "go", Q, asks=[_ask("0:plain", Q, answer="tabs", at=T1)]),
                             _turn(1, "tabs", Q2, t=T1, asks=[_ask("1:plain", Q2)])], None)
    assert store.session(KEY)["asks"][0]["answer"] == "tabs"
    mid = store.add_message(KEY, "yes", "laptop", "1:plain")
    store.set_message_status(mid, "laptop", "sent")
    store.upsert_turns(KEY, [_turn(1, "tabs", Q2, t=T1, asks=[_ask("1:plain", Q2, answer=WRAP + "yes", at=T2)]),
                             _turn(2, WRAP + "yes", "ok", t=T2, origin="peer")], None)
    a = {x["ref"]: x["answer"] for x in store.session(KEY)["asks"]}
    assert a == {"0:plain": "tabs", "1:plain": "yes"}               # only the ask that message answered


def test_quick_reply_answers_an_older_tracker_ask_and_refuses_dialogs(tmp_path):
    store = Store(tmp_path / "hub.db")
    _session(store)
    store.upsert_turns(KEY, [_turn(0, "go", "Done. Want the docs too? Also check CI.")], None)
    store.apply_extraction(KEY, _turn(0, "go"), [], [], [{"text": "Want the docs too?"}], None)
    store.db.execute("INSERT INTO asks(key,ref,kind,text,turn_idx,asked_at) VALUES(?,?,?,?,?,?)",
                     (KEY, "hook:perm:Bash:1", "permission", "allow Bash: rm x", 0, T0))
    c = TestClient(Hub(store, token="t").app())
    url = f"/v1/sessions/{KEY}/messages"
    r = c.post(url, headers=H, json={"body": "allow", "answers": "hook:perm:Bash:1"})
    assert r.status_code == 400 and "dialog" in r.json()["error"]
    assert c.post(url, headers=H, json={"body": "x", "answers": "nope"}).status_code == 409
    s = store.session(KEY)
    ask, _ = fmt.reply_target(s)
    assert ask["ref"] == "llm:0:0"                                   # the replyable one, not the older dialog
    store.cancel_message(c.post(url, headers=H, json={"body": "no", "answers": ask["ref"]}).json()["id"])
    mid = c.post(url, headers=H, json={"body": "yes please", "answers": ask["ref"]}).json()["id"]    # cancelled: free again
    store.set_message_status(mid, "laptop", "sent")
    store.upsert_turns(KEY, [_turn(1, WRAP + "yes please", "", t=T1, origin="peer", complete=False)], None)
    a = {x["ref"]: x for x in store.session(KEY)["asks"]}
    assert (a["llm:0:0"]["answered"], a["llm:0:0"]["answer"]) == (1, "yes please")
    assert a["hook:perm:Bash:1"]["answered"] == 0


def test_reply_target_says_why_not():
    perm = {"ref": "h", "kind": "permission", "text": "allow?", "answered": 0}
    plain = {"ref": "p", "kind": "plain", "text": "?", "answered": 0}
    assert fmt.reply_target({"harness": "claude", "asks": [perm]})[1].startswith("it's waiting on a permission dialog")
    assert fmt.reply_target({"harness": "claude", "asks": [perm, plain]})[0] is plain
    assert fmt.reply_target({"harness": "pi", "open_asks": [plain]})[0] is plain          # a list row, not a full session
    assert "nothing to answer" in fmt.reply_target({"harness": "claude", "asks": []})[1]
    assert "Windows" in fmt.reply_target({"harness": "claude-win", "asks": [plain]})[1]


def test_since_you_last_looked(tmp_path):
    store = Store(tmp_path / "hub.db")
    _session(store)
    store.upsert_turns(KEY, [_turn(0, "a", "x", t="2026-10-29T10:00:00Z"),
                             _turn(1, "b", "y", t="2026-10-29T10:01:00Z", end="2026-10-29T10:09:00Z")], None)
    s = store.session(KEY)
    assert (s["unseen"], s["seen_through"], fmt.since_viewed(s)) == (None, None, None)      # never looked: no baseline
    store.mark_viewed(KEY, "2026-10-29T10:05:00.123456Z")            # #1 was still running then
    store.upsert_turns(KEY, [_turn(2, "c", "z", t="2026-10-29T10:10:00Z")], None)
    for i, kind in ((0, "decision"), (1, "claim"), (2, "claim")):
        store.apply_extraction(KEY, _turn(i, "x"), [{"kind": kind, "text": f"{kind}{i}", "quote": "q", "extra": {}}], [], [], None)
    s = store.session(KEY)
    assert (s["unseen"], s["seen_through"]) == (2, 0)
    d = fmt.since_viewed(s)
    assert [r["text"] for r in d["rows"]] == ["claim1", "claim2"]
    assert fmt.since_line(d) == "2 exchanges · 2 claims"
    store.mark_viewed(KEY, "2026-10-29T11:00:00Z")
    s = store.session(KEY)
    assert (s["unseen"], s["seen_through"], fmt.since_viewed(s)) == (0, 2, None)
    assert [x["unseen"] for x in store.sessions()] == [0]


def test_workstream_matches_clones_across_hosts():
    ws = workstream_of({"repo_remote": "git@github.com:Me/Proj.git", "git_branch": "main"})
    assert ws == "me/proj@main"
    assert workstream_of({"repo_remote": "git@github-personal:me/proj", "git_branch": "main"}) == ws   # an ssh alias
    assert workstream_of({"repo_remote": "https://github.com/me/proj.git/", "git_branch": "main"}) == ws
    assert workstream_of({"repo_remote": "ssh://git@gitlab.example:2222/me/proj.git", "git_branch": "main"}) == ws
    assert workstream_of({"repo_remote": "git@github.com:me/proj.git", "git_branch": "dev"}) != ws
    assert workstream_of({"host_id": "devbox", "repo_root": "/r/x/", "git_branch": "b"}) == "devbox:/r/x@b"
    assert workstream_of({"cwd": "/tmp"}) is None
    assert fmt.workstream_label(ws) == "proj@main" and fmt.workstream_label("devbox:/r/x@b") == "x@b"


def test_siblings_list_the_same_work_on_other_hosts_with_their_instructions(tmp_path):
    store = Store(tmp_path / "hub.db")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())          # siblings are live or recently active
    _session(store, when=now)
    _session(store, "devbox/pi/p1", remote="https://github.com/me/proj", when=now)
    _session(store, "devbox/claude/s3", remote="git@github.com:me/proj.git", branch_="dev", when=now)
    _session(store, "devbox/claude/s4", remote="git@github.com:me/other.git", when=now)     # same branch, other repo
    _session(store, "devbox/claude/old", live=False, when="2020-01-01T00:00:00Z")
    _session(store, "devbox/claude/empty")
    for k in (KEY, "devbox/pi/p1", "devbox/claude/s3", "devbox/claude/s4"):
        store.upsert_turns(k, [_turn(0, "x", t=now)], None)
    store.upsert_turns("devbox/claude/old", [_turn(0, "x", t="2020-01-01T00:00:00Z")], None)
    store.apply_extraction("devbox/pi/p1", _turn(0, "x"), [{"kind": "instruction", "text": "don't push", "quote": "q", "extra": {}}], [], [], None)
    s = store.session(KEY)
    assert s["workstream"] == "me/proj@main"
    assert [(x["key"], x["instructions"]) for x in s["siblings"]] == [("devbox/pi/p1", ["don't push"])]
    assert store.session("devbox/claude/s3")["siblings"] == []


def test_branch_is_read_from_head_including_linked_worktrees(tmp_path):
    (tmp_path / "r/.git").mkdir(parents=True)
    (tmp_path / "r/.git/HEAD").write_text("ref: refs/heads/feat/x\n")
    assert branch(str(tmp_path / "r")) == "feat/x"
    (tmp_path / "r/.git/worktrees/w").mkdir(parents=True)
    (tmp_path / "r/.git/worktrees/w/HEAD").write_text("ref: refs/heads/other\n")
    (tmp_path / "w").mkdir()
    (tmp_path / "w/.git").write_text(f"gitdir: {tmp_path}/r/.git/worktrees/w\n")
    assert branch(str(tmp_path / "w")) == "other"
    (tmp_path / "r/.git/HEAD").write_text("0123456789abcdef\n")
    assert branch(str(tmp_path / "r")) == "" and branch(str(tmp_path / "nope")) == ""


def _item(key, ref=None, item="ask", at="t0", title="T", txt="Q?"):
    return {"key": key, "ref": ref, "item": item, "asked_at": at, "title": title, "text": txt, "host_id": "devbox"}


def test_notifier_pops_up_only_what_is_new():
    n = Notifier()
    a = _item("k1", "r1")
    old_idle = _item("k0", item="idle")
    assert n.fresh([a, old_idle]) == [a] and n.fresh([a]) == []      # waiting at start: pops up once (idle doesn't)
    b, idle, wait = _item("k2", "r2"), _item("k3", item="idle"), _item("k4", item="waiting")
    assert n.fresh([a, b, idle, wait]) == [b, wait]                  # an agent that only finished: not by default
    assert n.fresh([a, b, idle, wait]) == []
    assert n.fresh([a]) == [] and n.fresh([a, b]) == [b]             # it left, then asked again
    c = _item("k5", "r5")
    assert n.fresh([a, b, c], skip="k5") == []                       # it's on screen
    assert n.fresh([a, b, c], skip="k5") == []
    assert n.fresh([a, b, c], skip="k1") == [c]                      # moved off it, still open: now it pops up
    assert n.fresh([a, b, c], skip="k5") == [] and n.fresh([a, b, c]) == []     # once
    m = Notifier(idle=True); m.fresh([])
    assert m.fresh([idle]) == [idle]
    assert text([b, wait]) == "ctx · devbox · T: Q?  (+1 more)"
    assert text([wait]).endswith("T: waiting on a dialog") and text([idle]).endswith("finished, your turn")


def test_notifier_pops_up_a_session_once_with_what_says_most():
    n = Notifier()
    wait, ask = _item("k1", item="waiting", at="t1"), _item("k1", "llm:3:0", txt="Which one?")
    perm = _item("k1", "hook:notif:9", txt="Claude needs your permission")
    assert n.fresh([wait, perm, ask]) == [ask]                       # its question, not "waiting on a dialog"
    assert n.fresh([wait, perm]) == [] and n.fresh([perm, ask, wait]) == []
    m = Notifier()
    assert m.fresh([wait, perm]) == [perm] and m.fresh([wait]) == []
    assert m.fresh([wait, perm, ask]) == []                          # popped already, still waiting: same thing
    assert m.fresh([]) == [] and m.fresh([ask]) == [ask]             # left (answered), asked again
    o = Notifier()
    assert o.fresh([wait, _item("k2", "r2")]) == [wait, _item("k2", "r2")]


def test_popup_waits_for_the_question_to_settle(monkeypatch):
    b, seen = _board()
    timers = []
    b.set_timer = lambda t, f: timers.append(t)
    b.popup_now = None
    tui.CtxApp.popup(b, [_item("k1", item="waiting")])
    tui.CtxApp.popup(b, [_item("k1", item="waiting"), _item("k1", "r1")])
    assert timers == [2.0] and b.attention[-1]["ref"] == "r1"         # one timer; it sees the latest list


def test_tmux_popup_escapes_formats():
    calls = []

    class R:
        returncode = 0
    assert tmux_popup("use #{pane_id} and #[fg=red]", run=lambda argv, **kw: calls.append(argv) or R())
    assert calls == [["tmux", "display-message", "-N", "-d", "8000", "use ##{pane_id} and ##[fg=red]"]]
    R.returncode = 1
    assert not tmux_popup("x", run=lambda argv, **kw: R())


def test_tmux_box_shows_the_items_until_a_key():
    items = [_item(f"k{n}", f"r{n}", txt=f"Q{n}?") for n in range(8)]
    lines = box_lines(items)
    assert lines[:2] == ["1 devbox · T: Q0?", "2 devbox · T: Q1?"] and lines[6] == "+2 more on the board"
    assert lines[-2:] == ["", "Enter opens 1 · 1-6 open that one · any other key closes"]
    long = box_lines([_item("k", "r", title="t" * 50, txt="x" * 200)])
    assert max(map(len, long)) <= 116 and long[0].endswith("…")       # a wrapped line would scroll the first away
    calls = []
    tmux_box(items[:1], popen=lambda argv, **kw: calls.append((argv, kw)))
    ((argv, kw),) = calls
    assert argv[:3] == ["tmux", "display-popup", "-E"] and argv[-1] == BOX_CMD     # started, not waited on
    assert argv[argv.index("-T") + 1] == " ctx: waiting on you "
    assert argv[argv.index("-s") + 1] == argv[argv.index("-S") + 1] == "fg=yellow"   # text and border
    assert argv[argv.index("-e") + 1] == "CTX_POPUP=devbox · T: Q0?\n\nEnter opens it · any other key closes"
    assert int(argv[argv.index("-h") + 1]) == 3 + 2                   # 3 lines + the border
    assert int(argv[argv.index("-w") + 1]) == len("Enter opens it · any other key closes") + 4
    assert kw["stdin"] is not None                                    # never the TUI's terminal


def test_box_command_prints_exactly_the_text_and_says_which_key():
    def press(key):
        r = subprocess.run(["sh", "-c", BOX_CMD], input=key, capture_output=True, text=True,
                           env=os.environ | {"CTX_POPUP": "a $HOME `x`\nb"})
        return r.returncode, r.stdout
    assert press("\n") == (OPEN_RC, "a $HOME `x`\nb")                # no trailing newline, nothing expanded
    assert [press(k)[0] for k in ("\r", "2", "6", " ", "x", "7", "")] == [OPEN_RC, OPEN_RC + 1, OPEN_RC + 5, 3, 3, 3, 0]


def _board(popups=True):
    seen = []
    b = SimpleNamespace(notifier=Notifier(), visible_key=None, popups=popups, box=None, settling=False,
                        cfg=SimpleNamespace(notify_style="popup"), opened=seen, statused=seen,
                        open_key=lambda k: seen.append(("open", k)), status_popup=lambda m, ids: seen.append(("status", m)),
                        wait_box=lambda box, keys, msg: seen.append(("wait", keys)))
    return b, seen


def test_box_enter_opens_the_session_it_named(monkeypatch):
    b, seen = _board()
    monkeypatch.setattr(tui, "popup_log", lambda line: None)
    for rc in (OPEN_RC, OPEN_RC + 1, OPEN_RC + 2, 0, 3, 1):
        tui.CtxApp.box_closed(b, rc, ["k1", "k2"], "m")
    assert seen == [("open", "k1"), ("open", "k2"), ("status", "m")]  # out of range or closed: nothing; no client: status


def test_popups_toggle_off_keeps_track_of_what_was_seen(monkeypatch):
    b, seen = _board(popups=False)
    boxes = []
    monkeypatch.setattr(tui, "popup_log", lambda line: None)
    monkeypatch.setattr(tui.tmux, "in_tmux", lambda: True)
    monkeypatch.setattr(tui, "tmux_box", lambda new: boxes.append(new) or SimpleNamespace(poll=lambda: None))
    a, c = _item("k1", "r1"), _item("k2", "r2")
    b.attention = [a]
    tui.CtxApp.popup_now(b)
    assert boxes == [] and seen == []                                 # off: nothing
    b.popups = True
    b.attention = [a, c]
    tui.CtxApp.popup_now(b)
    assert boxes == [[c]] and seen == [("wait", ["k2"])]              # on again: only what's new since
