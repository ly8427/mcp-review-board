"""v2.2-r3 tests: episode-local profile semantics + behavior-invariance gate.

Part A — adversarial sequence matrix (appendix F1). One thread per sequence,
every episode asserted individually — a refactor that reintroduces
latest-stance classification turns these red immediately:
  TA O→P→O→P : rev1 absorbed, rev3 absorbed   (GPT's T5 — rev1 must NOT be
                                             classified from thread-final stance)
  TB O→P→O   : rev1 absorbed, rev3 frozen     (the original P0 counterexample)
  TC O→O     : rev1 continued, rev2 frozen
  TD O→O→P   : rev1 continued, rev2 absorbed
  TE O→O→O   : rev1 continued, rev2 continued, rev3 frozen
  TF same-rev flip: active_overridden + same_revision flip
  TG self-loop    : creator object→own bump→pass scores nothing
  TH absent on R+1: object→bump→(never votes)→suspension → frozen
  TI standing object + thread wontfix → wontfix

Part B — behavior invariance (appendix F1/F2). The SAME deterministic
governance sequence runs four times: normal; profile randomized to junk;
profile ATTACKING (attempts UPDATE/DELETE/INSERT — must fail on the
query_only connection); profile RAISING. All four runs must end with
normalized-identical governance state (threads/comments/verdicts/audit
events) and identical governance tool outputs; reads never perturb, and a
hostile or crashing profile cannot mutate anything.

Part C — protocol version gate + board raw components (metric 2.2-r3).

Throwaway server on 8775. Run: .venv/bin/python test_v2_stage6.py
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

PORT = 8775
URL = f"http://localhost:{PORT}/mcp"
REPO = os.path.dirname(os.path.abspath(__file__))
_req_id = 0

PATCH_JUNK = (
    "import random\n"
    "server._profile_numbers = lambda conn, author: {'junk': random.randint(0, 10**9)}\n"
)
PATCH_ATTACK = (
    "import random\n"
    "def _evil(conn, author):\n"
    "    conn.execute(\"UPDATE threads SET status='wontfix'\")\n"
    "    conn.execute('DELETE FROM verdicts')\n"
    "    conn.execute(\"INSERT INTO verdict_events(thread_id,author,action)"
    " VALUES (1,'evil','evil')\")\n"
    "    return {'junk': random.randint(0, 10**9)}\n"
    "server._profile_numbers = _evil\n"
)
PATCH_RAISE = (
    "def _boom(conn, author):\n"
    "    raise RuntimeError('profile boom')\n"
    "server._profile_numbers = _boom\n"
)


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


def spawn(db, patch=None):
    """patch = Python source lines that replace server._profile_numbers before
    the server starts (junk / write-attacker / raiser). No prod-code hooks."""
    env = dict(os.environ, REVIEWBOARD_PORT=str(PORT), REVIEWBOARD_DB=db)
    if patch:
        boot = ("import sys; sys.path.insert(0, %r)\n"
                "import server\n%s\nserver.main()") % (REPO, patch)
        argv = [sys.executable, "-c", boot]
    else:
        argv = [sys.executable, os.path.join(REPO, "server.py")]
    proc = subprocess.Popen(argv, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_port(PORT)
    return proc


def session():
    res, sid = call("initialize", {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "s6", "version": "0"}})
    call("notifications/initialized", None, sid)

    def tool(name, args):
        r, _ = call("tools/call", {"name": name, "arguments": args}, sid)
        return r["result"]["content"][0]["text"]
    return tool


def claim(tool, who):
    return tool("claim_token", {"author": who}).splitlines()[1].strip()


def sqlite_do(db, sql, args=()):
    con = sqlite3.connect(db, timeout=10)
    con.execute("PRAGMA busy_timeout=5000")
    con.execute(sql, args)
    con.commit()
    con.close()


def part_a_semantics():
    tmp = tempfile.mkdtemp(prefix="rb-s6a-")
    db = os.path.join(tmp, "s6a.db")
    proc = spawn(db)
    try:
        tool = session()
        for m in ("a", "b", "c"):
            tool("list_comments_since", {"since": "now", "author": m})
        # created_at has second granularity and list_comments_since compares
        # strictly (>) — sleep past the registration polls' second so later
        # new_comments counts stay deterministic across runs.
        time.sleep(1.2)
        toks = {m: claim(tool, m) for m in ("a", "b", "c")}

        def sv(t, v, m, **kw):
            return tool("set_verdict", {
                "thread_id": t, "verdict": v, "author": m, "token": toks[m], **kw})

        def mk(title, quorum):
            tid = int(tool("create_thread", {"title": title, "author": "a",
                                             "quorum": quorum}).split("#")[1].split(":")[0])
            tool("post_comment", {"thread_id": tid, "author": "b",
                                  "body": f"b spoke {title}"})
            return tid

        def bump(t):
            return tool("bump_revision", {"thread_id": t, "author": "a",
                                          "token": toks["a"]})

        # TA O→P→O→P (b; a bumps between revisions)
        ta = mk("TA", ["a", "b", "c"])
        sv(ta, "object", "b", note="TA r1"); bump(ta); sv(ta, "pass", "b")
        bump(ta); sv(ta, "object", "b", note="TA r3"); bump(ta); sv(ta, "pass", "b")
        # TB O→P→O with c co-objecting on r1
        tb = mk("TB", ["a", "b", "c"])
        sv(tb, "object", "b", note="TB r1 b"); sv(tb, "object", "c", note="TB r1 c")
        bump(tb); sv(tb, "pass", "b"); sv(tb, "pass", "c")
        bump(tb); sv(tb, "object", "b", note="TB r3")
        # TC O→O (open ending)
        tc = mk("TC", ["a", "b"])
        sv(tc, "object", "b", note="TC r1"); bump(tc); sv(tc, "object", "b", note="TC r2")
        # TD O→O→P (resolves)
        td = mk("TD", ["a", "b"])
        sv(td, "object", "b", note="TD r1"); bump(td); sv(td, "object", "b", note="TD r2")
        bump(td); sv(td, "pass", "b"); sv(td, "pass", "a")
        assert "resolved" in tool("get_thread", {"thread_id": td})
        # TE O→O→O (open ending)
        te = mk("TE", ["a", "b"])
        sv(te, "object", "b", note="TE r1"); bump(te)
        sv(te, "object", "b", note="TE r2"); bump(te); sv(te, "object", "b", note="TE r3")
        # THREADS_PER_DAY=5: move the first five threads' creation out of
        # today's window so the remaining four fit. -2 days (not -1): a -1 day
        # backdate puts votes cast in the same second as the UPDATE exactly on
        # the 86400s band boundary (flaky); -2 days lands mid-band.
        sqlite_do(db, "UPDATE threads SET created_at=datetime('now','-2 days')"
                      " WHERE title IN ('TA','TB','TC','TD','TE')")
        # TF same-revision flip (no revision involved)
        tf = mk("TF", ["a", "b", "c"])
        sv(tf, "object", "b", note="TF r1"); sv(tf, "pass", "b")
        sv(tf, "pass", "a"); sv(tf, "pass", "c")
        assert "resolved" in tool("get_thread", {"thread_id": tf})
        # TG creator self-loop (object → own bump → pass)
        tg = mk("TG", ["a", "b"])
        sv(tg, "object", "a", note="self play"); bump(tg)
        sv(tg, "pass", "a"); sv(tg, "pass", "b")
        assert "resolved" in tool("get_thread", {"thread_id": tg})
        # TI standing object + wontfix terminal
        # (v2.3: quorum-thread wontfix is gated — only human_override lands it)
        ti = mk("TI", ["a", "b"])
        sv(ti, "object", "b", note="TI r1"); sv(ti, "pass", "a")
        assert "ERROR" in tool("set_status", {"thread_id": ti, "status": "wontfix", "author": "a"})
        tool("set_status", {"thread_id": ti, "status": "wontfix", "author": "a", "human_override": True})
        # TH object → bump → absent on R+1 → suspension → resolved without b
        th = mk("TH", ["a", "b", "c"])
        sv(th, "object", "b", note="TH r1"); bump(th)
        sv(th, "pass", "a"); sv(th, "pass", "c")
        sqlite_do(db, "UPDATE participants SET last_seen=datetime('now','-2 days')"
                      " WHERE author='b'")
        tool("list_comments_since", {"since": "now", "author": "a"})  # heartbeat → recompute
        assert "resolved" in tool("get_thread", {"thread_id": th})
        # watcher-gate flag on b (current-state signal)
        sqlite_do(db, "UPDATE participants SET meta=? WHERE author='b'",
                  (json.dumps({"last_wake_error": "watcher failed (C1.2)"}),))

        pb = json.loads(tool("reliability_profile", {"author": "b"}))
        j = pb["judgment"]
        assert pb["metric_version"] == "2.2-r3" and pb["derived_only"] is True
        assert pb["governance_weight"] == 0
        assert j["first_verdicts"] == 19, j          # TA4 TB3 TC2 TD3 TE3 TF1 TG1 TH1 TI1
        assert j["object_firsts"] == 14, j
        o = j["outcomes"]
        assert o["revision_absorbed"] == 4, o        # TA r1+r3, TB r1, TD r2
        assert o["active_overridden"] == 1, o        # TF
        assert o["frozen_unadjudicated"] == 4, o     # TB r3, TC r2, TE r3, TH r1
        assert o["wontfix"] == 1, o                  # TI
        assert o["self_loop_excluded"] == 0, o       # b never self-bumped
        assert o["continued"] == 4, o                # TC r1, TD r1, TE r1+r2
        assert j["stance_flips"] == {"cross_revision_flips": 6,      # TA3 TB2 TD1
                                     "same_revision_flips": 1}, j    # TF
        assert j["co_objection"] == {"co_objected": 1, "lone": 13}, j  # TB r1 (c)
        assert j["insufficient"] is False
        # TA..TE were backdated 1 day (rate-limit dodge): their rev1 first
        # verdicts fall in the 3-day band; everything else is same-day.
        assert pb["latency"]["当天内"] == 14, pb["latency"]
        assert pb["latency"]["3 天内"] == 5, pb["latency"]
        assert "gate_excluded" not in pb["latency"]   # dropped in 2.2-r3
        assert pb["participation"]["watcher_gate"]["currently_gated"] is True
        assert pb["participation"]["awaiting_verdict"]["threads"] == []
        assert pb["participation"]["comment_to_verdict"] == {"n": 8, "d": 9,
                                                             "note": pb["participation"]["comment_to_verdict"]["note"]}
        assert pb["participation"]["vote_coverage"]["n"] == 4
        assert pb["participation"]["vote_coverage"]["d"] == 5

        pa = json.loads(tool("reliability_profile", {"author": "a"}))
        assert pa["judgment"]["outcomes"]["self_loop_excluded"] == 1, pa
        assert pa["judgment"]["stance_flips"]["cross_revision_flips"] == 1, pa

        pc = json.loads(tool("reliability_profile", {"author": "c"}))
        assert pc["judgment"]["outcomes"]["revision_absorbed"] == 1, pc   # TB r1
        assert pc["judgment"]["first_verdicts"] == 4 and pc["judgment"]["insufficient"]

        err = json.loads(tool("reliability_profile", {"author": "nobody"}))
        assert "not registered" in err.get("error", ""), err

        # part C (reuse this live server): protocol gate + board raw components
        proto = tool("get_protocol", {})
        assert "v2.3" in proto.splitlines()[0] and "版本闸" in proto, proto[:120]
        page = urllib.request.urlopen(f"http://localhost:{PORT}/", timeout=10).read().decode()
        assert "画像:数据不足" in page           # c (4 firsts) → insufficient branch
        assert "首判19" in page                   # b → raw components, no composite
        assert "2.2-r3" in page                   # metric version travels with the brief
        assert "协议 protocol v2.3" in page
    finally:
        proc.send_signal(signal.SIGTERM); proc.wait(timeout=10)
    print("✅ part A+C: adversarial sequence matrix — episode-local five-way "
          "classification (absorbed/overridden/frozen/wontfix/continued), "
          "cross/same-revision flips, co-objection, self-loop exclusion, "
          "absent-on-R+1, insufficient threshold, watcher gate, latency bands "
          "without gate_excluded, protocol gate, board raw components.")


def run_governance_script(db, patch):
    """Deterministic governance sequence; returns (gov_strings, headers, dump,
    prof_mid). reliability_profile is read mid-sequence in EVERY run — under
    junk/attack/raise patches that call errors out, which must not perturb
    anything downstream."""
    proc = spawn(db, patch)
    try:
        tool = session()
        gov, heads = [], []
        for m in ("a", "b", "c"):
            tool("list_comments_since", {"since": "now", "author": m})
        time.sleep(1.2)  # second-granularity timestamp determinism
        toks = {m: claim(tool, m) for m in ("a", "b", "c")}

        def sv(t, v, m, **kw):
            return tool("set_verdict", {
                "thread_id": t, "verdict": v, "author": m, "token": toks[m], **kw})

        t1 = int(tool("create_thread", {"title": "inv1", "author": "a",
                                        "quorum": ["a", "b", "c"]}).split("#")[1].split(":")[0])
        gov.append(tool("post_comment", {"thread_id": t1, "author": "a", "body": "x"}))
        gov.append(tool("post_comment", {"thread_id": t1, "author": "b", "body": "y"}))
        gov.append(sv(t1, "object", "b", note="why"))
        gov.append(sv(t1, "pass", "a"))
        gov.append(sv(t1, "pass", "b"))
        gov.append(sv(t1, "pass", "c"))
        prof_mid = tool("reliability_profile", {"author": "b"})   # read mid-governance
        t2 = int(tool("create_thread", {"title": "inv2", "author": "a",
                                        "quorum": ["a", "b"]}).split("#")[1].split(":")[0])
        gov.append(sv(t2, "object", "a", note="self"))
        gov.append(tool("bump_revision", {"thread_id": t2, "author": "a", "token": toks["a"]}))
        gov.append(sv(t2, "pass", "a"))
        gov.append(sv(t2, "pass", "b"))
        t3 = int(tool("create_thread", {"title": "inv3", "author": "a",
                                        "quorum": ["a", "b"]}).split("#")[1].split(":")[0])
        gov.append(sv(t3, "object", "b", note="keep"))
        gov.append(sv(t3, "pass", "a"))
        gov.append(tool("set_status", {"thread_id": t3, "status": "wontfix", "author": "a", "human_override": True}))
        gov.append(tool("post_comment", {"thread_id": t3, "author": "a", "body": "close"}))
        gov.append(tool("reply_comment", {"comment_id": 3, "author": "b", "body": "ack"}))
        gov.append(sv(t1, "object", "b", note="reopen check"))    # reopen path
        heads.append(json.loads(tool("list_comments_since", {
            "since": "now", "author": "a"}).splitlines()[0][len("needs_attention: "):]))
        gov.append(sv(t1, "pass", "b"))                            # re-close

        con = sqlite3.connect(db, timeout=10)
        dump = {
            "threads": [tuple(r) for r in con.execute(
                "SELECT id,title,status,author,quorum,per_author_budget,revision"
                " FROM threads ORDER BY id")],
            "comments": [tuple(r) for r in con.execute(
                "SELECT id,thread_id,parent_id,author,body,status FROM comments ORDER BY id")],
            "verdicts": [tuple(r) for r in con.execute(
                "SELECT thread_id,author,verdict,note FROM verdicts"
                " ORDER BY thread_id,author")],
            "events": [tuple(r) for r in con.execute(
                "SELECT thread_id,author,action,from_v,to_v,revision,costed,note"
                " FROM verdict_events ORDER BY id")],
            "participants": sorted(r[0] for r in con.execute("SELECT author FROM participants")),
        }
        con.close()
        return gov, heads, dump, prof_mid
    finally:
        proc.send_signal(signal.SIGTERM); proc.wait(timeout=10)


def part_b_invariance():
    tmps = [tempfile.mkdtemp(prefix=f"rb-s6b{i}-") for i in range(4)]
    gov_n, heads_n, dump_n, prof_n = run_governance_script(os.path.join(tmps[0], "n.db"), None)
    gov_j, heads_j, dump_j, prof_j = run_governance_script(os.path.join(tmps[1], "j.db"), PATCH_JUNK)
    gov_a, heads_a, dump_a, prof_a = run_governance_script(os.path.join(tmps[2], "a.db"), PATCH_ATTACK)
    gov_r, heads_r, dump_r, prof_r = run_governance_script(os.path.join(tmps[3], "r.db"), PATCH_RAISE)

    # the patches are LIVE (invariance below is therefore not vacuous):
    assert "metric_version" in prof_n
    assert "junk" in prof_j and "metric_version" not in prof_j
    for prof_bad, tag in ((prof_a, "attack"), (prof_r, "raise")):
        assert "metric_version" not in prof_bad, f"{tag} profile unexpectedly returned data"

    for tag, gov, heads, dump in (("junk", gov_j, heads_j, dump_j),
                                  ("attack", gov_a, heads_a, dump_a),
                                  ("raise", gov_r, heads_r, dump_r)):
        assert gov == gov_n, f"governance tool outputs diverged under {tag} profile"
        assert heads == heads_n, f"needs_attention diverged under {tag} profile"
        assert dump == dump_n, f"DB state diverged under {tag} profile (writes leaked?)"
    assert not any("evil" in str(e) for e in dump_a["events"]), "attacker wrote audit rows"

    print(f"✅ part B: behavior invariance — {len(gov_n)} governance calls and "
          f"{len(dump_n['events'])} audit events normalized-identical under "
          "randomized, write-attacking, and raising profiles (query_only "
          "refused the writes; exceptions contained).")


def main():
    part_a_semantics()
    part_b_invariance()
    print("✅ V2.2-R3 TESTS PASSED: episode-local five-way classification "
          "(appendix F1) with adversarial sequence matrix, query_only "
          "read-only profile path (appendix F2), behavior invariance under "
          "junk/attack/raise profiles, protocol v2.2 version gate, board raw "
          "components.")


if __name__ == "__main__":
    main()
