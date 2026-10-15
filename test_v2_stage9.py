"""Stage 9 (v2.7, thread #36 D1) tests: the get_thread verdict read face.

D1 (board decision, 4/4 pass): verdict events readable inside get_thread —
quorum seats with current votes, revision-addressed trails, the gate trail,
and C3 preserved (the face is a pure read). Acceptance anchors:

  - dsh #382 ①  events are revision-addressed; the face distinguishes
                "bump-cleared" from "never voted".
  - dsh #382 ②  C3 measured on the real face: get_thread never advances
                last_delivered_id / last_read.
  - pi  #384 ③  the counted set is on the face: auto_resolve's tally note
                (which members passed the gate) is rendered in the gate trail;
                seats carry roster + CURRENT liveness labels (historical
                per-event liveness is documented non-reconstructible).

Cases:
  1. fresh quorum thread → every seat `never voted`, liveness [active].
  2. pass + object(note) shown on seats; object→pass convergence flip renders
     the per-revision trail; auto_resolve lands in gate with its tally.
  3. bump_revision → seats flip to `bump-cleared (voted r1, not r2)`;
     bump + auto_reopen land in the gate trail; re-vote works on r2.
  4. stale heartbeat (direct fixture write, same-OS temp DB) → [suspended].
  5. C3: probe before/after two get_thread calls — cursor fields identical.
  6. free thread (no quorum) → no verdicts block at all.
  7. human_override status change lands in the gate trail.

Throwaway server on 8779. Run: .venv/bin/python test_v2_stage9.py
"""
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

PORT = 8779
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
    tmp = tempfile.mkdtemp(prefix="rb-s9-")
    DB = os.path.join(tmp, "s9.db")
    env = dict(os.environ, REVIEWBOARD_PORT=str(PORT), REVIEWBOARD_DB=DB)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__) or ".", "server.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_port(PORT)
        res, sid = call("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "s9", "version": "0"}})
        call("notifications/initialized", None, sid)

        def tool(name, args):
            res, _ = call("tools/call", {"name": name, "arguments": args}, sid)
            return res["result"]["content"][0]["text"]

        def poll(member):
            return tool("list_comments_since", {"since": "now", "author": member})

        for m in ("h", "a", "b"):
            poll(m)
        tok = {}
        for m in ("h", "a", "b"):
            tok[m] = tool("claim_token", {"author": m}).split("\n")[1].strip()

        out = tool("create_thread", {"title": "verdict face", "author": "h",
                                     "quorum": ["a", "b"]})
        tid = int(out.split("#")[1].split(":")[0])

        # --- 1. fresh seats face ------------------------------------------
        face = tool("get_thread", {"thread_id": tid})
        assert "verdicts (rev 1):" in face, face[:400]
        assert face.count("never voted") == 2, face[:600]
        assert "a: — never voted [active]" in face, face[:600]
        assert "b: — never voted [active]" in face, face[:600]
        assert "gate:" not in face, face[:600]

        # --- 2. votes, note, flip, auto_resolve tally ----------------------
        tool("set_verdict", {"thread_id": tid, "verdict": "pass",
                             "author": "a", "token": tok["a"]})
        tool("set_verdict", {"thread_id": tid, "verdict": "object",
                             "author": "b", "note": "flip when the gap lands",
                             "token": tok["b"]})
        face = tool("get_thread", {"thread_id": tid})
        assert "a: pass" in face and "[active]" in face, face[:600]
        assert "b: object" in face and "note: flip when the gap lands" in face, face
        assert "trail: r1:∅→object" in face, face
        assert "auto_resolve" not in face, face  # standing object → no gate fire

        # --- 5. C3: the face is a pure read (dsh #382 ②) -------------------
        tool("post_comment", {"thread_id": tid, "author": "h", "body": "c1"})
        poll("a")
        before = probe("a")
        tool("get_thread", {"thread_id": tid})
        tool("get_thread", {"thread_id": tid})
        after = probe("a")
        assert (before["last_delivered_id"] == after["last_delivered_id"]
                and before["last_read"] == after["last_read"]), (before, after)

        # convergence flip → auto_resolve with tally
        tool("set_verdict", {"thread_id": tid, "verdict": "pass",
                             "author": "b", "token": tok["b"]})
        face = tool("get_thread", {"thread_id": tid})
        assert "[resolved]" in face, face[:200]
        assert "trail: r1:∅→object r1:object→pass" in face, face
        gate = face.split("gate:")[1]
        assert "auto_resolve" in gate and "active quorum unanimous: a pass · b pass" in gate, gate
        assert "b: pass" in face, face

        # --- 3. bump clears; seats say bump-cleared, not never-voted -------
        tool("bump_revision", {"thread_id": tid, "author": "h", "token": tok["h"]})
        face = tool("get_thread", {"thread_id": tid})
        assert "verdicts (rev 2):" in face, face[:400]
        assert face.count("bump-cleared (voted r1, not r2)") == 2, face[:800]
        assert "never voted" not in face, face[:800]
        assert "[open]" in face, face[:200]          # auto_reopen after bump
        gate = face.split("gate:")[1]
        assert "bump_revision r2 by h" in gate, gate
        assert "auto_reopen" in gate, gate

        # re-vote on r2 keeps first-verdict semantics on the face
        tool("set_verdict", {"thread_id": tid, "verdict": "pass",
                             "author": "a", "token": tok["a"]})
        face = tool("get_thread", {"thread_id": tid})
        assert "a: pass" in face and "b: — bump-cleared (voted r1, not r2)" in face, face

        # --- 4. liveness face: stale heartbeat renders suspended -----------
        con = sqlite3.connect(DB)
        con.execute("UPDATE participants SET last_seen='2026-01-01 00:00:00'"
                    " WHERE author='a'")
        con.commit()
        con.close()
        face = tool("get_thread", {"thread_id": tid})
        assert "a: pass" in face and "[suspended]" in face, face

        # --- 6. free threads carry no verdict face -------------------------
        out = tool("create_thread", {"title": "free", "author": "h"})
        fid = int(out.split("#")[1].split(":")[0])
        face = tool("get_thread", {"thread_id": fid})
        assert "verdicts (" not in face, face[:400]

        # --- 7. human_override lands in the gate trail ---------------------
        tool("set_status", {"thread_id": tid, "status": "wontfix",
                            "author": "h", "human_override": True})
        face = tool("get_thread", {"thread_id": tid})
        gate = face.split("gate:")[1]
        assert "human_override" in gate and "by h" in gate, gate
        assert "[wontfix]" in face, face[:200]
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)
    print("✅ STAGE9 TESTS PASSED: verdict read face — seats/never-voted, "
          "notes+trails, auto_resolve tally in gate, bump-cleared vs "
          "never-voted, C3 pure-read, suspended liveness, free-thread "
          "exclusion, human_override gate event.")


if __name__ == "__main__":
    main()
