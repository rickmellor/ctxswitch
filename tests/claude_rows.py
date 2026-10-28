"""Synthetic Claude Code transcript rows mirroring the real structure (no real content)."""
S = "11111111-2222-4333-8444-555555555555"
BASE = {"cwd": "/home/rick/repos/demo", "gitBranch": "feat/x", "sessionId": S, "version": "2.1.280",
        "userType": "external", "isSidechain": False}


def user(text, ts, **kw):
    d = dict(BASE, type="user", uuid=f"u{ts}", timestamp=ts, message={"role": "user", "content": text}); d.update(kw); return d


def assistant(blocks, ts, model="claude-opus-5-5"):
    return dict(BASE, type="assistant", uuid=f"a{ts}", timestamp=ts,
                message={"role": "assistant", "model": model, "content": blocks, "stop_reason": "end_turn"})


def text(t): return {"type": "text", "text": t}
def tool_use(name, tid, inp=None): return {"type": "tool_use", "id": tid, "name": name, "input": inp or {}}


def tool_result(tid, content, ts, is_error=False, tur=None):
    item = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error: item["is_error"] = True
    d = dict(BASE, type="user", uuid=f"r{ts}", timestamp=ts, message={"role": "user", "content": [item]})
    if tur is not None: d["toolUseResult"] = tur
    return d


def system(subtype, ts, **kw):
    d = dict(BASE, type="system", subtype=subtype, timestamp=ts, uuid=f"s{ts}"); d.update(kw); return d


def meta(type_, **kw):
    d = {"type": type_, "sessionId": S}; d.update(kw); return d
