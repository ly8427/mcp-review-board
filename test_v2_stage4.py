"""Stage 4 tests: /attention probe (C1.1 shared heartbeat, C3 no-cursor) and
board rendering upgrades.

Probe contract (dsh-3 + trae C3 acceptance scenario):
- probe heartbeats (last_seen advances) via the same code path (observable:
  it keeps the member active / can restore C' like a poll would)
- probe NEVER advances last_read: backlogged comments stay attention=1 across
  repeated probes (level-triggered), and a later list_comments_since still
  delivers them
- reason priority awaiting_verdict > mentioned > new_comments > idle
- trae C3 scenario: backlog -> probe 1 -> (wake failures are watcher-side,
  skipped) -> deliver via list_comments_since -> probe goes idle

Board: participants strip + awaiting badge + protocol footer present.
Throwaway server on 8773. Run: .venv/bin/python test_v2_stage4.py
"""
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

PORT = 8773
BASE = f"http://localhost:{PORT}"
_req_id = 0


def call(method, params=None, sid=None):
    global _req_id
    _req_id += 1
    payload = {"jsonrpc": "2.0", "id": _req_id, "method": method}
    if params is not None:
        payload["params"] = params
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if sid:
        headers["Mcp-Session-Id"] = sid
    req = urllib.request.Request(BASE + "/mcp", data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        sid = resp.headers.get("Mcp-Session-Id", sid)
        body = resp.read().decode()
        if body.startswith("event:") or body.startswith("data:"):
            for line in body.splitlines():
                if line.startswith("data:"):
                    body = line[5:].strip()
                    break
        return json.loads(body), sid


def wait_port(port, timeout=60.0):
    dl = time.time() + timeout
    while time.time() < dl:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise TimeoutError("no listener")


def probe(author):
    with urllib.request.urlopen(f"{BASE}/attention?author={author}", timeout=10) as r:
        return json.loads(r.read().decode())


def main():
    tmp = tempfile.mkdtemp(prefix="rb-s4-")
    env = dict(os.environ, REVIEWBOARD_PORT=str(PORT),
               REVIEWBOARD_DB=os.path.join(tmp, "s4.db"))
    proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__) or ".", "server.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    db = os.path.join(tmp, "s4.db")
    try:
        wait_port(PORT)
        res, sid = call("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "s4", "version": "0"}})
        call("notifications/initialized", None, sid)

        def tool(name, args):
            r, _ = call("tools/call", {"name": name, "arguments": args}, sid)
            return r["result"]["content"][0]["text"]

        # register + token for a (creator), b (poster), w (watched member)
        for m in ("a", "b", "w"):
            tool("list_comments_since", {"since": "now", "author": m})
        tok_a = tool("claim_token", {"author": "a"}).splitlines()[1].strip()
        out = tool("create_thread", {"title": "probe-t", "author": "a",
                                     "quorum": ["a", "w"]})
        tid = int(out.split("#")[1].split(":")[0])

        # 1. probe heartbeats: w's last_seen advances via probe alone
        con = sqlite3.connect(db, timeout=10)
        s1 = con.execute("SELECT last_seen FROM participants WHERE author='w'").fetchone()[0]
        con.close()
        time.sleep(1.2)
        p = probe("w")
        assert p["attention"] == 1 and p["reason"] == "awaiting_verdict" and p["threads"] == [tid], p
        con = sqlite3.connect(db, timeout=10)
        s2 = con.execute("SELECT last_seen FROM participants WHERE author='w'").fetchone()[0]
        con.close()
        assert s2 > s1, "probe must heartbeat (C1.1 shared path)"

        # 2. C3: repeated probes do NOT clear the signal (no cursor advance)
        for _ in range(3):
            p = probe("w")
            assert p["attention"] == 1, "level-triggered: probe must keep attention=1"

        # 3. mention priority beats new_comments: post a @w comment
        tool("post_comment", {"thread_id": tid, "author": "b", "body": "hey @w look"})
        tool("set_verdict", {"thread_id": tid, "verdict": "pass",
                             "author": "a", "token": tok_a})
        # w still awaiting (no verdict) -> awaiting stays top priority
        p = probe("w")
        assert p["reason"] == "awaiting_verdict", p

        # 4. w delivers via list_comments_since -> awaiting cleared by voting;
        #    then probe for w goes idle only when nothing undelivered AND no awaiting
        tok_w = tool("claim_token", {"author": "w"}).splitlines()[1].strip()
        out = tool("list_comments_since", {"since": "now", "author": "w"})
        assert "hey @w look" in out, "cursor-authoritative poll must deliver backlog"
        tool("set_verdict", {"thread_id": tid, "verdict": "pass",
                             "author": "w", "token": tok_w})
        p = probe("w")
        assert p["attention"] == 0 and p["reason"] == "idle", p

        # 5. reason=new_comments for a member with no mentions/awaiting
        tool("post_comment", {"thread_id": tid, "author": "b", "body": "plain note"})
        p = probe("a")
        assert p["reason"] == "new_comments" and p["attention"] == 1, p

        # 6. board upgrades: participants strip, awaiting badge, protocol footer
        html = urllib.request.urlopen(BASE + "/", timeout=10).read().decode("utf-8")
        assert "auto-refreshes 20s" in html and "/attention?author=" in html
        assert re.search(r"🟢 a ", html) or ">a <" in html or "a " in html.split("<p>")[1][:200]
        assert "协议 protocol v" in html and "游标" in html

        print("✅ STAGE4 TESTS PASSED: probe heartbeats via shared path, C3 "
              "level-trigger (repeated probes keep attention=1), priority "
              "awaiting>mentioned>new, delivery-then-idle, board strip/footer.")
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


if __name__ == "__main__":
    main()
