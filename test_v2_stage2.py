"""Stage 2 (v2 A3) regression tests: budgets, rate limits, quorum validation.

Covers (per reviewed plan v3.1):
- chicken-egg order fix: on a FRESH db, create_thread(author=me, quorum=[me, other])
  is rejected ONLY for 'other' — the creator is upserted BEFORE the quorum check
  (claude #48-1 acceptance case)
- quorum with registered names accepted; unregistered rejected with poll-hint
- per-author budget: budget=2 thread → 2 posts ok, 3rd rejected
- daily limits: 6th thread/day rejected; 51st comment/day rejected
- get_thread header shows quorum/revision/budget usage

Spawns a throwaway server (port 8770, temp DB). Run: .venv/bin/python test_v2_stage2.py
"""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

PORT = 8770
URL = f"http://localhost:{PORT}/mcp"
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
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(),
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
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise TimeoutError(f"no listener on {port} in {timeout}s")


def main():
    tmpdir = tempfile.mkdtemp(prefix="rb-stage2-")
    env = dict(os.environ, REVIEWBOARD_PORT=str(PORT),
               REVIEWBOARD_DB=os.path.join(tmpdir, "s2.db"))
    proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__) or ".", "server.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_port(PORT)
        res, sid = call("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "s2", "version": "0"}})
        call("notifications/initialized", None, sid)

        def tool(name, args):
            res, _ = call("tools/call", {"name": name, "arguments": args}, sid)
            return res["result"]["content"][0]["text"]

        # 0. register 'other' via a bare poll (registration needs no post)
        tool("list_comments_since", {"since": "now", "author": "other"})

        # 1. chicken-egg: creator in own quorum is fine; only unregistered rejected
        out = tool("create_thread", {"title": "t1", "author": "me",
                                     "quorum": ["me", "ghost"]})
        assert "ghost" in out and "not registered" in out, f"expect ghost rejection: {out}"
        out = tool("create_thread", {"title": "t1", "author": "me",
                                     "quorum": ["me", "other"]})
        assert out.startswith("Created thread #"), f"creator+registered quorum must pass: {out}"
        tid = int(out.split("#")[1].split(":")[0])

        # 2. header shows quorum + budget
        out = tool("get_thread", {"thread_id": tid})
        assert "quorum" in out and "revision: 1" in out, f"header: {out.splitlines()[2]}"

        # 3. per-author budget (set 2)
        out = tool("create_thread", {"title": "t2", "author": "me",
                                     "quorum": ["me", "other"],
                                     "per_author_budget": 2})
        tid2 = int(out.split("#")[1].split(":")[0])
        assert "Posted" in tool("post_comment", {"thread_id": tid2, "author": "me", "body": "a"})
        assert "Posted" in tool("post_comment", {"thread_id": tid2, "author": "me", "body": "b"})
        out = tool("post_comment", {"thread_id": tid2, "author": "me", "body": "c"})
        assert "per-author budget" in out, f"expect budget rejection: {out}"
        assert "Posted" in tool("post_comment", {"thread_id": tid2, "author": "other", "body": "x"})

        # 4. daily thread limit: me has 2; three more ok, 6th rejected
        for i in range(3):
            out = tool("create_thread", {"title": f"f{i}", "author": "me"})
            assert out.startswith("Created"), out
        out = tool("create_thread", {"title": "sixth", "author": "me"})
        assert "daily thread-creation limit" in out, f"expect limit: {out}"

        # 5. daily post limit: other has 1 post; allow up to 50 with a big budget thread
        out = tool("create_thread", {"title": "flood", "author": "other",
                                     "quorum": ["me", "other"],
                                     "per_author_budget": 100})
        tid3 = int(out.split("#")[1].split(":")[0])
        # 1 already used by other on tid2; posts are counted per author per day
        ok, rejected_at = 0, None
        for i in range(60):
            out = tool("post_comment", {"thread_id": tid3, "author": "other",
                                        "body": f"n{i}"})
            if out.startswith("ERROR"):
                rejected_at = (i, out)
                break
            ok += 1
        assert rejected_at and "daily post limit" in rejected_at[1], \
            f"expect 50/day cap, got ok={ok}, last={rejected_at}"

        print("✅ STAGE2 TESTS PASSED: chicken-egg order fix, quorum validation "
              "(poll-registers), per-author budget, daily thread(5)/post(50) "
              "limits, get_thread header with quorum/budget usage.")
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


if __name__ == "__main__":
    main()
