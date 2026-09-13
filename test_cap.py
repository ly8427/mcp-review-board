"""Regression test for the per-thread discussion cap.

Spawns a throwaway server instance (port 8767, temp DB, cap=3), then checks:
  - comments 1..3 post fine
  - comment 4 (post_comment) is REJECTED with the lock message
  - reply into the full thread is REJECTED too
  - set_status still works on a locked thread (conclusion path)

Run:  .venv/bin/python test_cap.py     (inside WSL, from the project dir)
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

PORT = 8767
URL = f"http://localhost:{PORT}/mcp"
CAP = 3

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


def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="rb-captest-")
    env = dict(
        os.environ,
        REVIEWBOARD_PORT=str(PORT),
        REVIEWBOARD_DB=os.path.join(tmpdir, "cap.db"),
        REVIEWBOARD_THREAD_CAP=str(CAP),
    )
    proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__) or ".", "server.py")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_port(PORT)
        res, sid = call("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "cap-test", "version": "0.1"},
        })
        call("notifications/initialized", None, sid)

        def tool(name: str, args: dict) -> str:
            res, _ = call("tools/call", {"name": name, "arguments": args}, sid)
            return res["result"]["content"][0]["text"]

        out = tool("create_thread", {"title": "cap test"})
        tid = int(out.split("#")[1].split(":")[0])
        for i in range(1, CAP + 1):
            out = tool("post_comment", {"thread_id": tid, "author": "tester",
                                        "body": f"comment {i}"})
            assert f"Posted comment #{i}" in out, f"post {i} should succeed: {out}"

        out = tool("post_comment", {"thread_id": tid, "author": "tester",
                                    "body": "should be rejected"})
        assert "ERROR" in out and "cap reached" in out, f"post over cap must be rejected: {out}"

        out = tool("reply_comment", {"comment_id": 1, "author": "tester",
                                     "body": "reply into full thread"})
        assert "ERROR" in out and "cap reached" in out, f"reply over cap must be rejected: {out}"

        out = tool("set_status", {"thread_id": tid, "status": "resolved",
                                  "author": "tester"})
        assert "resolved" in out, f"set_status must still work on locked thread: {out}"

        out = tool("get_thread", {"thread_id": tid})
        assert f"comments: {CAP}/{CAP}" in out, f"get_thread should show cap usage: {out}"

        print("✅ CAP TEST PASSED: posts 1..3 ok, 4th post rejected, reply rejected, "
              "set_status works, usage shown as N/CAP.")
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


if __name__ == "__main__":
    main()
