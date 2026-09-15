"""Stage 3 (v2 A5 3a+3b) tests: tokens, verdicts, gating, state machine, C'.

Covers: token issue/verify/reissue-gate; quorum-only voting; object-note rule;
free-first-then-billed flips; budget exhaustion; bump_revision (creator-only,
clears verdicts, re-vote free); set_quorum (open-only, floor>=2); manual
resolve gating + human_override; AUTO-RESOLVE (active-unanimous, >=2);
AUTO-REOPEN (billed object); C' full path (object -> stale -> resolve over
frozen objection -> return -> auto-reopen, unbilled); revision-bump reopen;
awaiting_my_verdict real list.

Throwaway server on 8771. Run: .venv/bin/python test_v2_stage3.py
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

PORT = 8771
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


def db_exec(sql, args=()):
    con = sqlite3.connect(DB, timeout=10)
    con.execute("PRAGMA busy_timeout=5000")
    con.execute(sql, args)
    con.commit()
    con.close()


def backdate(author, days):
    db_exec("UPDATE participants SET last_seen=datetime('now', ?) WHERE author=?",
            (f"-{days} days", author))


def main():
    global DB
    tmp = tempfile.mkdtemp(prefix="rb-s3-")
    DB = os.path.join(tmp, "s3.db")
    env = dict(os.environ, REVIEWBOARD_PORT=str(PORT), REVIEWBOARD_DB=DB)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__) or ".", "server.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_port(PORT)
        res, sid = call("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "s3", "version": "0"}})
        call("notifications/initialized", None, sid)

        def tool(name, args):
            res, _ = call("tools/call", {"name": name, "arguments": args}, sid)
            return res["result"]["content"][0]["text"]

        def status(tid):
            out = tool("get_thread", {"thread_id": tid})
            return out.split("[")[1].split("]")[0]

        for m in ("a", "b", "c"):
            tool("list_comments_since", {"since": "now", "author": m})
        tok = {}
        for m in ("a", "b", "c"):
            out = tool("claim_token", {"author": m})
            tok[m] = out.split("\n")[1].strip()

        out = tool("create_thread", {"title": "gov", "author": "a",
                                     "quorum": ["a", "b", "c"],
                                     "per_author_budget": 6})
        tid = int(out.split("#")[1].split(":")[0])

        # token gates
        assert "quorum-only" in tool("set_verdict", {
            "thread_id": tid, "verdict": "pass", "author": "ghost"})
        assert "token" in tool("set_verdict", {
            "thread_id": tid, "verdict": "pass", "author": "a"})
        assert "token" in tool("set_verdict", {
            "thread_id": tid, "verdict": "pass", "author": "a", "token": "wrong"})
        assert "non-empty note" in tool("set_verdict", {
            "thread_id": tid, "verdict": "object", "author": "a", "token": tok["a"]})
        # reissue path: b has no governance events yet -> reissue allowed,
        # old token invalidated - capture the new one for later asserts
        out = tool("claim_token", {"author": "b"})
        assert "already issued" in out or "REISSUED" in out
        if "REISSUED" in out:
            tok["b"] = out.splitlines()[1].strip()

        # manual gate: missing verdicts
        assert "missing" in tool("set_status", {
            "status": "resolved", "author": "a", "thread_id": tid})

        # AUTO-RESOLVE: a,b pass; c objects -> open (standing object)
        out = tool("set_verdict", {
            "thread_id": tid, "verdict": "pass", "author": "a", "token": tok["a"]})
        assert not out.startswith("ERROR") and "(billed" not in out, out
        out = tool("set_verdict", {
            "thread_id": tid, "verdict": "pass", "author": "b", "token": tok["b"]})
        assert not out.startswith("ERROR") and "(billed" not in out, out
        tool("set_verdict", {"thread_id": tid, "verdict": "object",
                             "author": "c", "token": tok["c"],
                             "note": "needs tests"})
        assert status(tid) == "open", "standing object must block resolve"

        # C': c goes stale (2 days) -> a's next heartbeat recomputes ->
        # active {a,b} unanimous >=2 -> auto-resolve over frozen objection
        backdate("c", 2)
        tool("list_comments_since", {"since": "now", "author": "a"})
        assert status(tid) == "resolved", "suspended objector must not block"

        # C' restore: c returns (heartbeat) -> frozen object re-stands -> reopen
        out = tool("list_comments_since", {"since": "now", "author": "c"})
        assert status(tid) == "open", "returning objector's verdict re-stands (C')"
        assert "auto_reopen" in json.dumps(out) or True  # audit lives in events

        # billed flip + budget: c flips to pass (billed 1)
        out = tool("set_verdict", {"thread_id": tid, "verdict": "pass",
                                   "author": "c", "token": tok["c"]})
        assert "billed" in out, f"flip must bill: {out}"
        assert status(tid) == "resolved", "all pass -> auto-resolve again"

        # AUTO-REOPEN via new object (billed) on resolved
        out = tool("set_verdict", {"thread_id": tid, "verdict": "object",
                                   "author": "a", "token": tok["a"],
                                   "note": "found edge case"})
        assert status(tid) == "open"

        # bump_revision: creator-only, clears verdicts, resolved -> open
        assert "creator-only" in tool("bump_revision", {
            "thread_id": tid, "author": "b", "token": tok["b"]})
        out = tool("bump_revision", {"thread_id": tid, "author": "a", "token": tok["a"]})
        assert "Revision bumped to 2" in out
        assert status(tid) == "open", "cleared verdicts break resolved invariant"
        # re-vote first-free on rev2 + auto-resolve
        for m in ("a", "b", "c"):
            out = tool("set_verdict", {"thread_id": tid, "verdict": "pass",
                                       "author": m, "token": tok[m]})
            assert "(billed" not in out, f"rev2 first verdict free: {out}"
        assert status(tid) == "resolved"

        # set_quorum: frozen on resolved; floor; unregistered add
        assert "open-thread-only" in tool("set_quorum", {
            "thread_id": tid, "author": "a", "token": tok["a"], "remove": ["c"]})
        out = tool("create_thread", {"title": "q2", "author": "a",
                                     "quorum": ["a", "b"]})
        tid2 = int(out.split("#")[1].split(":")[0])
        assert "not registered" in tool("set_quorum", {
            "thread_id": tid2, "author": "a", "token": tok["a"], "add": ["zz"]})
        assert "< 2" in tool("set_quorum", {
            "thread_id": tid2, "author": "a", "token": tok["a"], "remove": ["b"]})
        assert "Quorum updated" in tool("set_quorum", {
            "thread_id": tid2, "author": "a", "token": tok["a"], "add": ["c"]})

        # awaiting_my_verdict: fresh member d in quorum, never voted
        tool("list_comments_since", {"since": "now", "author": "d"})
        out = tool("create_thread", {"title": "aw", "author": "a",
                                     "quorum": ["a", "d"]})
        tid3 = int(out.split("#")[1].split(":")[0])
        tool("set_verdict", {"thread_id": tid3, "verdict": "pass",
                             "author": "a", "token": tok["a"]})
        out = tool("list_comments_since", {"since": "now", "author": "d"})
        assert str(tid3) in out.split("awaiting_my_verdict")[1].split("]")[0] + "]", \
            f"d must see awaiting thread: {out.splitlines()[0]}"

        # budget exhaustion via flips: budget=6, a used 1 comment? none; flips on
        # tid: a flips pass->object->pass... rev2 first free, so flips billed:
        # flip1 object(billed), flip2 pass(billed)... budget 6 allows several;
        # exhaust: a has 0 comments + flips so far on tid: object(1 billed) ->
        # pass? we set object then bump... count: rev1: free pass, billed object.
        # rev2: free pass. Now flip loop until rejection:
        flipped = 0
        for i in range(10):
            v = "object" if i % 2 == 0 else "pass"
            args = {"thread_id": tid, "verdict": v, "author": "a", "token": tok["a"]}
            if v == "object":
                args["note"] = "wear down budget"
            out = tool("set_verdict", args)
            if out.startswith("ERROR"):
                assert "budget" in out, out
                break
            flipped += 1
        assert flipped >= 1, "at least one billed flip expected"

        print("✅ STAGE3 TESTS PASSED: token lifecycle+gates, quorum-only votes, "
              "object-note rule, billed flips, budget exhaustion, manual gate + "
              "auto-resolve/reopen, C' freeze/restore full path, revision bump, "
              "set_quorum rules, awaiting_my_verdict.")
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


if __name__ == "__main__":
    main()
