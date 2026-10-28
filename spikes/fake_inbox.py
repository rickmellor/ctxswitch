"""Spike (a): pose as a Claude Code session so a real SendMessage reveals the inbox-socket wire format.

Registers a temporary entry in ~/.claude/sessions pointing messagingSocketPath at our own listener and logs
every byte that arrives. Run it yourself (not from an agent), then in any Claude session ask:
    "send 'hello from spike' to @ctxswitch-spike"
and read the capture file. Ctrl-C removes the registry entry and socket.

    python3 spikes/fake_inbox.py spikes/out/inbox_capture.txt
"""
import json
import os
import signal
import socket
import sys
import threading
import time

pid = os.getpid()
with open(f"/proc/{pid}/stat") as f:
    proc_start = f.read().rsplit(")", 1)[1].split()[19]  # field 22 (starttime), same as the registry's procStart
runtime = os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000")
sock_dir = os.path.join(runtime, "cc-socks")
os.makedirs(sock_dir, exist_ok=True)
sock_path = os.path.join(sock_dir, f"{pid}.sock")
reg = os.path.expanduser(f"~/.claude/sessions/{pid}.json")
now = int(time.time() * 1000)
entry = {
    "pid": pid, "sessionId": "00000000-0000-4000-8000-00000000c7c5", "cwd": os.getcwd(),
    "startedAt": now, "procStart": proc_start, "version": "2.1.280", "peerProtocol": 1,
    "peerFeatures": ["notify_idle", "reply_across_default_dirs", "artifact_yield"], "kind": "interactive",
    "entrypoint": "cli", "messagingSocketPath": sock_path, "name": "ctxswitch-spike", "nameSource": "user",
    "nameSince": now, "status": "idle", "updatedAt": now, "statusUpdatedAt": now,
}
os.makedirs(os.path.dirname(sys.argv[1]), exist_ok=True)
out = open(sys.argv[1], "a", buffering=1)


def cleanup(*_):
    for p in (reg, sock_path):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
    out.write("# cleaned up\n")
    sys.exit(0)


signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)
try:
    os.unlink(sock_path)
except FileNotFoundError:
    pass
srv = socket.socket(socket.AF_UNIX)
srv.bind(sock_path)
os.chmod(sock_path, 0o600)
srv.listen(8)
with open(reg, "w") as f:
    json.dump(entry, f)
out.write(f"# listening {sock_path} registry {reg}\n")
print(f"listening on {sock_path}; registered as @ctxswitch-spike; capture -> {sys.argv[1]}; Ctrl-C to clean up")


def handle(conn):
    conn.settimeout(40)
    buf = b""
    try:
        while True:
            d = conn.recv(65536)
            if not d:
                break
            buf += d
    except Exception as e:  # noqa: BLE001 - spike
        out.write(f"# recv end: {e!r}\n")
    out.write(f"--- {time.strftime('%H:%M:%S')} {len(buf)} bytes\n{buf.decode('utf-8', 'replace')}\n")
    conn.close()


def heartbeat():
    while True:
        time.sleep(15)
        entry["updatedAt"] = int(time.time() * 1000)
        with open(reg, "w") as f:
            json.dump(entry, f)


threading.Thread(target=heartbeat, daemon=True).start()
while True:
    conn, _ = srv.accept()
    threading.Thread(target=handle, args=(conn,), daemon=True).start()
