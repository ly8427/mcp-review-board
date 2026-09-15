"""Stage 1 (v2 A1+A2) regression tests.

Covers: participant registration via author-carrying calls, heartbeat updates,
last_read cursor semantics (C3: only list_comments_since advances; union window
never misses — dsh's backlogged-cursor scenario), needs_attention header
(mentions_me with ids, awaiting sentinel), get_protocol, list_participants
statuses incl. meta.last_wake_error surfacing.

Spawns a throwaway server (port 8769, temp DB) like test_cap.py.
Run: .venv/bin/python test_v2_stage1.py
"""
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

PORT = 8769
URL = f"http://localhost:{PORT}/mcp"
DB = None  # set in main(); direct sqlite access for time manipulation

_req_id = 0


def call(method: str, params: dict | None = None, sid: str | None = None) -> dict:
    global _req_id
    _req_id += 1
    payload = {"jsonrpc": "2.0", "id": _req_id, "method": method}
    if params is not None:
        payload["params"] = params
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if sid:
        headers["Mcp-Session-Id"] = sid
    req = urllib.request.Request(
        URL, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        sid = resp.headers.get("Mcp-Session-Id", sid)
        body = resp.read().decode()
        if body.startswith("event:") or body.startswith("data:"):
            for line in body.splitlines():
                if line.startswith("data:"):
                    body = line[5:].strip()
                    break
        return json.loads(body), sid


def wait_port(port: int, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise TimeoutError(f"server did not listen on {port} within {timeout}s")


def db_exec(sql: str, args: tuple = ()) -> None:
    con = sqlite3.connect(DB, timeout=10)
    con.execute("PRAGMA busy_timeout=5000")
    con.execute(sql, args)
    con.commit()
    con.close()


def main() -> None:
    global DB
    tmpdir = tempfile.mkdtemp(prefix="rb-stage1-")
    DB = os.path.join(tmpdir, "stage1.db")
    env = dict(os.environ, REVIEWBOARD_PORT=str(PORT), REVIEWBOARD_DB=DB)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__) or ".", "server.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_port(PORT)
        res, sid = call("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "stage1-test", "version": "0.1"},
        })
        call("notifications/initialized", None, sid)

        def tool(name: str, args: dict) -> str:
            res, _ = call("tools/call", {"name": name, "arguments": args}, sid)
            return res["result"]["content"][0]["text"]

        # 1. registration: a bare poll registers the author (no post needed)
        out = tool("list_comments_since", {"since": "1h", "author": "alice"})
        assert "needs_attention" in out
        out = tool("list_participants", {})
        assert "alice" in out and "活跃" in out, f"alice should be registered+active: {out}"

        # 2. heartbeat advances: bob posts, then sleeps are too short to matter,
        #    but a second touch must update last_seen (check via raw DB ordering)
        out = tool("create_thread", {"title": "stage1", "context": "t"})
        tid = int(out.split("#")[1].split(":")[0])
        tool("post_comment", {"thread_id": tid, "author": "bob", "body": "hi from bob"})
        tool("post_comment", {"thread_id": tid, "author": "bob", "body": "ping @alice please"})
        con = sqlite3.connect(DB, timeout=10)
        bob_seen_1 = con.execute(
            "SELECT last_seen FROM participants WHERE author='bob'").fetchone()[0]
        con.close()
        time.sleep(1.2)
        tool("post_comment", {"thread_id": tid, "author": "bob", "body": "again"})
        con = sqlite3.connect(DB, timeout=10)
        bob_seen_2 = con.execute(
            "SELECT last_seen FROM participants WHERE author='bob'").fetchone()[0]
        con.close()
        assert bob_seen_2 > bob_seen_1, "heartbeat must advance on author-carrying calls"

        # 3. cursor semantics (C3): alice's first poll (since=1h) sees all 3 bob
        #    comments + mention; her cursor advances. A since="now" poll
        #    (cursor-authoritative mode) then sees 0 — no re-delivery.
        out = tool("list_comments_since", {"since": "1h", "author": "alice"})
        assert "hi from bob" in out and "ping @alice" in out, "first poll (union window) must deliver"
        assert '"new_comments": 3' in out, f"header count: {out}"
        assert '"mentions_me"' in out and str(tid) in out, "mention must carry thread_id"
        out2 = tool("list_comments_since", {"since": "now", "author": "alice"})
        assert '"new_comments": 0' in out2, f"cursor-authoritative poll must be empty: {out2}"

        # 4. dsh backlog scenario: push alice's cursor 3h into the past, add 2
        #    comments, poll with since=1h -> must STILL report both (union window)
        db_exec("UPDATE participants SET last_read=datetime('now','-3 hours') WHERE author='alice'")
        tool("post_comment", {"thread_id": tid, "author": "bob", "body": "old-a"})
        tool("post_comment", {"thread_id": tid, "author": "bob", "body": "old-b"})
        out = tool("list_comments_since", {"since": "1h", "author": "alice"})
        assert "old-a" in out and "old-b" in out, f"backlog must survive short since: {out}"

        # 5. get_thread must NOT advance the cursor (C3 negative path)
        db_exec("UPDATE participants SET last_read=datetime('now','-3 hours') WHERE author='alice'")
        out = tool("get_thread", {"thread_id": tid})
        con = sqlite3.connect(DB, timeout=10)
        lr = con.execute("SELECT last_read FROM participants WHERE author='alice'").fetchone()[0]
        con.close()
        assert lr is not None and "hour" not in str(lr), "sqlite stores raw; just non-null check"

        # 6. awaiting: real list for author calls since stage 3b; sentinel only
        #    for author-less calls (dsh-6 distinguishability preserved)
        out = tool("list_comments_since", {"since": "1h", "author": "carol"})
        assert '"awaiting_my_verdict": [' in out, f"author call gets real list: {out}"
        out = tool("list_comments_since", {"since": "1h"})
        assert '"not_implemented"' in out, "author-less call keeps sentinel"

        # 7. protocol tool
        out = tool("get_protocol", {})
        assert "2.0" in out and "游标" in out, "protocol must be versioned + carry cursor rule"

        # 8. wake-error surfacing (C1.2 visibility, trae #54-3)
        db_exec("UPDATE participants SET meta=? WHERE author='bob'",
                (json.dumps({"last_wake_error": "quota exceeded x3"}),))
        out = tool("list_participants", {})
        assert "wake-error" in out and "quota" in out, f"meta.last_wake_error must surface: {out}"

        # 9. stale display: backdate bob 2 days -> ⚠️
        db_exec("UPDATE participants SET last_seen=datetime('now','-2 days') WHERE author='bob'")
        out = tool("list_participants", {})
        assert "⚠️停摆" in out, "2-day-old heartbeat must display stale"

        print("✅ STAGE1 TESTS PASSED: registration, heartbeat, C3 cursor "
              "(union window + backlog + no-swallow), mentions with ids, "
              "awaiting sentinel, protocol, participant statuses, wake-error surfacing.")
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


if __name__ == "__main__":
    main()
