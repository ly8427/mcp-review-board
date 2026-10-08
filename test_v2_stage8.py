"""Stage 8 (v2.6, issue #3) tests: monotonic id delivery watermark.

The second-resolution last_read watermark could permanently lose a comment
committed in the same wall-clock second as the reader's own poll (strict >
over truncated timestamps). The delivery truth is now the monotonic
comments.id watermark (participants.last_delivered_id):

  1. Same-second regression: post -> poll -> immediate post -> poll must
     deliver the second comment (no sleeps — this is exactly the window the
     old cursor lost intermittently; deterministic green on the id cursor).
  2. Probe face: /attention exposes last_delivered_id, and the probe never
     advances it (C3 unchanged — only list_comments_since writes).
  3. Own-comment exclusion carries over to the id path (v2.5 B-2), while
     the read face still returns own comments.
  4. A never-polled member's FIRST poll shows the since-window only, never a
     full-history dump (registration leaves cursor 0 / last_read NULL; the
     first read advances the watermark from its pre-fetch MAX floor).
  5. Interleaved concurrency (#375/#376 requirement): concurrent posters vs
     a live poller — every posted comment must appear in some poll response
     (zero loss; the serial same-second regression cannot exercise the
     rows-vs-watermark statement gap).

Throwaway server on 8778. Run: .venv/bin/python test_v2_stage8.py
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

PORT = 8778
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
    tmp = tempfile.mkdtemp(prefix="rb-s8-")
    DB = os.path.join(tmp, "s8.db")
    env = dict(os.environ, REVIEWBOARD_PORT=str(PORT), REVIEWBOARD_DB=DB)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__) or ".", "server.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_port(PORT)
        res, sid = call("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "s8", "version": "0"}})
        call("notifications/initialized", None, sid)

        def tool(name, args):
            res, _ = call("tools/call", {"name": name, "arguments": args}, sid)
            return res["result"]["content"][0]["text"]

        def poll(member):
            return tool("list_comments_since", {"since": "now", "author": member})

        for m in ("h", "a", "b"):
            poll(m)
        tok = {}
        for m in ("a", "b"):
            tok[m] = tool("claim_token", {"author": m}).split("\n")[1].strip()

        out = tool("create_thread", {"title": "idcursor", "author": "h",
                                     "quorum": ["a", "b"]})
        tid = int(out.split("#")[1].split(":")[0])
        # a passes, b objects with a note — an open thread with a standing
        # object: no auto-resolve, and a's probe reasons stay new/idle only.
        tool("set_verdict", {"thread_id": tid, "verdict": "pass",
                             "author": "a", "token": tok["a"]})
        tool("set_verdict", {"thread_id": tid, "verdict": "object",
                             "author": "b", "note": "hold", "token": tok["b"]})

        # --- 1. same-second regression -------------------------------------
        tool("post_comment", {"thread_id": tid, "author": "h", "body": "c1"})
        out1 = poll("a")
        assert "c1" in out1, out1
        id1 = probe("a")["last_delivered_id"]
        assert id1 >= 1, id1
        # the loss window: a's cursor just wrote; h posts in the SAME second
        tool("post_comment", {"thread_id": tid, "author": "h", "body": "c2-same-second"})
        out2 = poll("a")
        assert "c2-same-second" in out2, \
            "same-second comment lost — the exact race issue #3 fixes:\n" + out2
        p = probe("a")
        assert p["last_delivered_id"] > id1, (p, id1)
        assert p["attention"] == 0, p  # delivered; own next post excluded below

        # --- 2. probe never advances the watermark (C3) --------------------
        tool("post_comment", {"thread_id": tid, "author": "h", "body": "c3"})
        p1 = probe("a")
        assert p1["reason"] == "new_comments" and p1["attention"] == 1, p1
        assert p1["last_delivered_id"] == p["last_delivered_id"], \
            "probe must not advance the watermark (C3)"
        probe("a")  # repeat: still level-triggered
        p2 = probe("a")
        assert p2["attention"] == 1 and \
            p2["last_delivered_id"] == p1["last_delivered_id"], p2
        out3 = poll("a")
        assert "c3" in out3, out3
        assert probe("a")["last_delivered_id"] > p1["last_delivered_id"]

        # --- 3. own comments: excluded from own wake, kept on read face ----
        tool("post_comment", {"thread_id": tid, "author": "a", "body": "own-note"})
        p3 = probe("a")
        assert p3["attention"] == 0, \
            f"own comment must not re-raise own attention (v2.5 B-2 on id path): {p3}"
        out4 = poll("a")
        assert "own-note" in out4, \
            f"read face must still return own comments: {out4}"

        # --- 4. new member: cursor starts at MAX(id) ------------------------
        outd = poll("d")
        first_line = out4 and outd.splitlines()[0]
        assert '"new_comments": 0' in first_line, \
            f"new member must start from the since-window, not full history: {first_line}"
        # d is unregistered in any quorum; probe is idle and exposes cursor face
        pd = probe("d")
        assert pd["attention"] == 0 and "last_delivered_id" in pd, pd

        # --- 5. interleaved: concurrent posters vs poller, zero loss -------
        # dsh #375 requirement / opencode #376 rider: with since="now" the
        # time branch is fully bypassed and the id branch is the only safety
        # net — the harshest shape, and the serial part-1 regression cannot
        # exercise the rows-vs-watermark statement gap.
        import itertools
        import threading
        _ids = itertools.count(10_000)

        def call2(method, params=None, sid=None):
            payload = {"jsonrpc": "2.0", "id": next(_ids), "method": method}
            if params is not None:
                payload["params"] = params
            req = urllib.request.Request(
                URL, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"},
                method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                got_sid = resp.headers.get("Mcp-Session-Id", sid)
                body = resp.read().decode()
                if body.startswith("event:") or body.startswith("data:"):
                    for line in body.splitlines():
                        if line.startswith("data:"):
                            body = line[5:].strip()
                            break
                return json.loads(body), got_sid

        out = tool("create_thread", {"title": "interleave", "author": "h",
                                     "quorum": ["a", "b"],
                                     "per_author_budget": 60})
        tid2 = int(out.split("#")[1].split(":")[0])
        posted, seen_text = [], []
        plock = threading.Lock()
        stop = threading.Event()

        def poster(n):
            for i in range(6):
                tool("post_comment", {"thread_id": tid2, "author": "h",
                                      "body": f"il-{n}-{i}"})
                with plock:
                    posted.append(f"il-{n}-{i}")

        def poll_once(sid):
            r, _ = call2("tools/call", {"name": "list_comments_since",
                         "arguments": {"since": "now", "author": "a"}}, sid)
            return r["result"]["content"][0]["text"]

        _, sid2 = call2("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "s8b", "version": "0"}})
        call2("notifications/initialized", None, sid2)

        def poller():
            while not stop.is_set():
                seen_text.append(poll_once(sid2))
                time.sleep(0.02)

        pt = threading.Thread(target=poller)
        pt.start()
        threads = [threading.Thread(target=poster, args=(n,)) for n in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stop.set()
        pt.join()
        seen_text.append(poll_once(sid2))  # final drain
        blob = "\n".join(seen_text)
        lost = [b for b in posted if b not in blob]
        assert len(posted) == 18, len(posted)
        assert not lost, f"interleaved loss: {lost} — the #375/#376 race shape"

        print("✅ STAGE8 TESTS PASSED: monotonic id delivery watermark — "
              "same-second regression, probe C3 face, own-comment exclusion "
              "on the id path, never-polled first read, interleaved "
              "posters-vs-poller zero loss.")
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    main()
