"""v2.1 tests: two-phase token lifecycle (R1/R2/R3 fixes, thread #9 incident).

Scenarios (REVIEWBOARD_UNACKED_REISSUE_MIN=0 for tests unless testing the limit):
1. provisional issue: claim → unacked; old-style immediate re-claim at limit 0
   → free REISSUE, old token invalidated (old fails governance, new works)
2. ack_token: wrong token rejected; right token acks; claim then says acked+locked
3. auto-ack: fresh member claims, uses token in set_verdict → acked recorded
   (claim afterwards shows the acked/locked message)
4. rate limit: with UNACKED_REISSUE_MIN high, immediate re-claim of an unacked
   token is rejected with the wait hint
5. reset_token: without human_override rejected; with it clears → fresh claim works
6. acked 24h lock: backdate governance event >24h → acked reissue path works
7. legacy migration: a pre-v2.1-style meta (token_hash, no token_acked key)
   becomes acked on init_db (verified via fresh server start on prepared DB)
8. governance regressions: stage-3 style claim→vote one-session flow still fine

Throwaway server on 8774 (UNACKED limit = 0 by default here).
Run: .venv/bin/python test_v2_stage5.py
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

PORT = 8774
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


def spawn(db, unacked_min="0"):
    env = dict(os.environ, REVIEWBOARD_PORT=str(PORT), REVIEWBOARD_DB=db,
               REVIEWBOARD_UNACKED_REISSUE_MIN=unacked_min)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__) or ".", "server.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_port(PORT)
    return proc


def session():
    res, sid = call("initialize", {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "s5", "version": "0"}})
    call("notifications/initialized", None, sid)
    def tool(name, args):
        r, _ = call("tools/call", {"name": name, "arguments": args}, sid)
        return r["result"]["content"][0]["text"]
    return tool


def meta_of(db, author):
    con = sqlite3.connect(db, timeout=10)
    row = con.execute("SELECT meta FROM participants WHERE author=?", (author,)).fetchone()
    con.close()
    return json.loads(row[0] or "{}")


def main():
    global DB
    tmp = tempfile.mkdtemp(prefix="rb-s5-")
    DB = os.path.join(tmp, "s5.db")

    # ---- part A: main lifecycle, UNACKED limit = 0 ----
    proc = spawn(DB, "0")
    try:
        tool = session()
        for m in ("a", "b", "c"):
            tool("list_comments_since", {"since": "now", "author": m})

        # 1. provisional + free reissue invalidates old
        tok1 = tool("claim_token", {"author": "a"}).splitlines()[1].strip()
        assert meta_of(DB, "a").get("token_acked") is False
        out = tool("claim_token", {"author": "a"})
        assert "REISSUED" in out and "never persisted" not in out, out
        tok2 = out.splitlines()[-1].strip()
        tid = int(tool("create_thread", {"title": "t", "author": "a",
                                         "quorum": ["a", "b", "c"]}).split("#")[1].split(":")[0])
        assert "token" in tool("set_verdict", {  # old token now invalid
            "thread_id": tid, "verdict": "pass", "author": "a", "token": tok1})
        assert "Verdict recorded" in tool("set_verdict", {  # new works
            "thread_id": tid, "verdict": "pass", "author": "a", "token": tok2})

        # 3. auto-ack happened via that successful use
        assert meta_of(DB, "a").get("token_acked") is True
        out = tool("claim_token", {"author": "a"})
        assert "issued AND acknowledged" in out and "reset_token" in out, out

        # 2. ack_token paths (member b)
        tokb = tool("claim_token", {"author": "b"}).splitlines()[1].strip()
        assert "mismatch" in tool("ack_token", {"author": "b", "token": "0" * 32})
        assert "acknowledged" in tool("ack_token", {"author": "b", "token": tokb})
        assert "already acknowledged" in tool("ack_token", {"author": "b", "token": tokb})
        assert meta_of(DB, "b").get("token_acked") is True

        # 6. acked 24h lock: backdate a's governance event beyond 24h
        con = sqlite3.connect(DB, timeout=10)
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("UPDATE verdict_events SET created_at=datetime('now','-2 days')"
                    " WHERE author='a'")
        con.commit(); con.close()
        out = tool("claim_token", {"author": "a"})
        assert "REISSUED" in out and "acked path" in out, out
        # de-facto: new unacked token; use it to re-confirm
        tok3 = out.splitlines()[-1].strip()
        assert "Verdict recorded" in tool("set_verdict", {
            "thread_id": tid, "verdict": "object", "author": "a",
            "token": tok3, "note": "for test"})
        # cleanup object -> pass so thread can resolve later; also member c
        tokc = tool("claim_token", {"author": "c"}).splitlines()[1].strip()
        tool("set_verdict", {"thread_id": tid, "verdict": "pass", "author": "c", "token": tokc})
        tool("set_verdict", {"thread_id": tid, "verdict": "pass", "author": "a", "token": tok3})

        # 5. reset_token
        assert "human_override" in tool("reset_token", {"author": "b"})
        out = tool("reset_token", {"author": "b", "human_override": True})
        assert "cleared" in out and "next claim_token issues a fresh token" in out
        tokb2 = tool("claim_token", {"author": "b"}).splitlines()[1].strip()
        assert tokb2 != tokb
        assert "Verdict recorded" in tool("set_verdict", {
            "thread_id": tid, "verdict": "pass", "author": "b", "token": tokb2})
        st = sqlite3.connect(DB, timeout=10)
        print("thread status:", st.execute("SELECT status FROM threads WHERE id=?", (tid,)).fetchone())
        st.close()
    finally:
        proc.send_signal(signal.SIGTERM); proc.wait(timeout=10)

    # ---- part B: rate limit (UNACKED limit = 30) ----
    tmp2 = tempfile.mkdtemp(prefix="rb-s5b-")
    db2 = os.path.join(tmp2, "s5b.db")
    proc = spawn(db2, "30")
    try:
        tool = session()
        tool("list_comments_since", {"since": "now", "author": "x"})
        tool("claim_token", {"author": "x"})
        out = tool("claim_token", {"author": "x"})
        assert "never acknowledged" in out and "min" in out, out
    finally:
        proc.send_signal(signal.SIGTERM); proc.wait(timeout=10)

    # ---- part C: legacy migration ----
    tmp3 = tempfile.mkdtemp(prefix="rb-s5c-")
    db3 = os.path.join(tmp3, "s5c.db")
    proc = spawn(db3, "0")  # create schema
    proc.send_signal(signal.SIGTERM); proc.wait(timeout=10)
    con = sqlite3.connect(db3, timeout=10)
    con.execute("PRAGMA busy_timeout=5000")
    now = "2026-09-16 00:00:00"
    con.execute("INSERT INTO participants(author, first_seen, last_seen, last_read, meta)"
                " VALUES ('legacy', ?, ?, NULL, ?)",
                (now, now, json.dumps({"token_hash": "ab" * 32})))
    con.commit(); con.close()
    proc = spawn(db3, "0")  # restart -> init_db migration runs
    try:
        assert meta_of(db3, "legacy").get("token_acked") is True, "legacy backfill"
        tool = session()
        out = tool("claim_token", {"author": "legacy"})
        # no governance events ever -> the acked 24h window is trivially open,
        # so a reissue is fine — what matters is it took the ACKED path, not
        # the free-unacked one (which would mean the migration didn't run)
        assert "acked path" in out and "never acknowledged" not in out, out
    finally:
        proc.send_signal(signal.SIGTERM); proc.wait(timeout=10)

    print("✅ V2.1 TESTS PASSED: provisional issue, free unacked reissue "
          "(old invalidated), ack_token paths, auto-ack on governance use, "
          "rate limit, reset_token(human_override), acked-24h path, legacy "
          "migration, one-session claim→vote regression.")


if __name__ == "__main__":
    main()
