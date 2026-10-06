"""Stage 7 (v2.5, thread #33) tests: verdict-staleness read faces, budget
truth, free convergence flips, bump exemption, mention word boundary,
own-comment wake exclusion, /attention observability fields.

Part 1 (Phase 1a): verdict_stale —
  /attention reason priority (awaiting > verdict_stale > new_comments),
  holder needs >=2 OWN followup comments, thread-level marker in
  list_threads/get_thread (visible to non-quorum readers), post receipt
  addendum, clearing on re-verdict.

Part 2 (Phase 1b): budget truth + free convergence + bump exemption —
  post/reply blocked at cap while the object→pass flip still goes through
  (free, ungated); pass→object at cap stays gated with the honest guidance;
  get_thread usage == _author_usage (billed flips included); /attention
  budget face mirrors it; creator may bump_revision while exhausted.

Throwaway server on 8776. Run: .venv/bin/python test_v2_stage7.py
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

PORT = 8776
URL = f"http://localhost:{PORT}/mcp"
DB = None
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
    dl = time.time() + timeout
    while time.time() < dl:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise TimeoutError("no listener")


def probe(author):
    with urllib.request.urlopen(
        f"http://localhost:{PORT}/attention?author={author}", timeout=10
    ) as r:
        return json.loads(r.read().decode())


def main():
    global DB
    tmp = tempfile.mkdtemp(prefix="rb-s7-")
    DB = os.path.join(tmp, "s7.db")
    env = dict(os.environ, REVIEWBOARD_PORT=str(PORT), REVIEWBOARD_DB=DB)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__) or ".", "server.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_port(PORT)
        res, sid = call("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "s7", "version": "0"}})
        call("notifications/initialized", None, sid)

        def tool(name, args):
            res, _ = call("tools/call", {"name": name, "arguments": args}, sid)
            return res["result"]["content"][0]["text"]

        for m in ("h", "a", "b"):
            tool("list_comments_since", {"since": "now", "author": m})
        tok = {}
        for m in ("a", "b"):
            tok[m] = tool("claim_token", {"author": m}).split("\n")[1].strip()

        out = tool("create_thread", {"title": "stale", "author": "h",
                                     "quorum": ["a", "b"],
                                     "per_author_budget": 6})
        tid = int(out.split("#")[1].split(":")[0])

        # a objects (free first verdict), then keeps posting over it
        out = tool("set_verdict", {"thread_id": tid, "verdict": "object",
                                   "author": "a", "note": "needs X",
                                   "token": tok["a"]})
        assert "object" in out, out
        # ts granularity is 1s and the rule is strict (>); separate the
        # verdict's landing second from the followup comments' seconds
        time.sleep(1.1)
        tool("post_comment", {"thread_id": tid, "author": "a", "body": "text pass 1"})
        p = probe("a")
        assert p["reason"] != "verdict_stale", f"1 own followup must not fire: {p}"
        tool("post_comment", {"thread_id": tid, "author": "a", "body": "text pass 2"})
        p = probe("a")
        assert p["reason"] == "verdict_stale" and p["threads"] == [tid], p

        # thread-level marker: visible to non-quorum readers (the host)
        assert "⚠ stale-object:['a']" in tool("list_threads", {"status": "open"})
        assert "⚠ stale-object: ['a']" in tool("get_thread", {"thread_id": tid})

        # receipt addendum fires at the source for OTHER posters too
        out = tool("post_comment", {"thread_id": tid, "author": "b", "body": "comment"})
        assert "verdict_stale" in out, out

        # priority: an uncast verdict outranks a stale one
        pb = probe("b")
        assert pb["reason"] == "awaiting_verdict", pb

        # re-verdict clears staleness (window resets with updated_at)
        out = tool("set_verdict", {"thread_id": tid, "verdict": "pass",
                                   "author": "a", "token": tok["a"]})
        assert "(free)" in out, out  # v2.5: object→pass convergence flip is free
        p = probe("a")
        assert p["reason"] != "verdict_stale", p
        assert "⚠ stale-object" not in tool("list_threads", {"status": "open"})

        # ---- part 2 (Phase 1b): budget truth + free convergence + bump exemption
        tok["h"] = tool("claim_token", {"author": "h"}).split("\n")[1].strip()
        tool("list_comments_since", {"since": "now", "author": "c"})
        tok["c"] = tool("claim_token", {"author": "c"}).split("\n")[1].strip()
        out = tool("create_thread", {"title": "budget", "author": "h",
                                     "quorum": ["c"], "per_author_budget": 2})
        tid2 = int(out.split("#")[1].split(":")[0])
        tool("set_verdict", {"thread_id": tid2, "verdict": "object",
                             "author": "c", "note": "x", "token": tok["c"]})
        time.sleep(1.1)  # keep the verdict second separate from followups
        tool("post_comment", {"thread_id": tid2, "author": "c", "body": "one"})
        tool("post_comment", {"thread_id": tid2, "author": "c", "body": "two"})
        # at cap: further posts are rejected...
        out = tool("post_comment", {"thread_id": tid2, "author": "c", "body": "no"})
        assert "ERROR" in out and "per-author budget" in out, out
        # ...but the object→pass convergence flip is FREE and ungated
        out = tool("set_verdict", {"thread_id": tid2, "verdict": "pass",
                                   "author": "c", "token": tok["c"]})
        assert "(free)" in out, out
        # pass→object at cap stays gated — with the honest new guidance
        out = tool("set_verdict", {"thread_id": tid2, "verdict": "object",
                                   "author": "c", "note": "y", "token": tok["c"]})
        assert "convergence is free" in out and "bump_revision" in out, out
        # display face == real quota (2 comments; the flips were free/gated)
        out = tool("get_thread", {"thread_id": tid2})
        assert "c 2/2" in out, out
        p = probe("c")
        assert p["budget"] == {str(tid2): {"used": 2, "cap": 2}}, p
        # billed flip lands in the display face: d on its own thread
        tool("list_comments_since", {"since": "now", "author": "d"})
        tok["d"] = tool("claim_token", {"author": "d"}).split("\n")[1].strip()
        out = tool("create_thread", {"title": "billed", "author": "h",
                                     "quorum": ["d"], "per_author_budget": 6})
        tid3 = int(out.split("#")[1].split(":")[0])
        tool("set_verdict", {"thread_id": tid3, "verdict": "pass",
                             "author": "d", "token": tok["d"]})
        out = tool("set_verdict", {"thread_id": tid3, "verdict": "object",
                                   "author": "d", "note": "z", "token": tok["d"]})
        assert "(billed 1)" in out, out
        tool("post_comment", {"thread_id": tid3, "author": "d", "body": "d1"})
        out = tool("get_thread", {"thread_id": tid3})
        assert "d 2/6" in out, out  # 1 comment + 1 billed flip, not comments-only
        # bump exemption: creator h exhausted on comments, bump still allowed
        tool("post_comment", {"thread_id": tid2, "author": "h", "body": "h1"})
        tool("post_comment", {"thread_id": tid2, "author": "h", "body": "h2"})
        out = tool("post_comment", {"thread_id": tid2, "author": "h", "body": "h3"})
        assert "ERROR" in out, out
        out = tool("bump_revision", {"thread_id": tid2, "author": "h", "token": tok["h"]})
        assert "Revision bumped to 2" in out, out

        print("stage7 part1 (verdict_stale) + part2 (budget/convergence/bump): OK")
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


if __name__ == "__main__":
    main()
