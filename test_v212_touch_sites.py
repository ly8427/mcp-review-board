#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_v212_touch_sites.py — v2.12.0 batch1 1.1a behavioural acceptance.

Frozen shape (thread #88: #1177 §六 / #1183 §四 / #1184 §四 / #1186 §二 /
#1188 §二 / #1190 §四):
  * 5 token-path sites: claim (first issue / unacked reissue / acked reissue),
    ack (incl. idempotent), reset — each success PATH advances last_seen,
    each REJECT path leaves last_seen byte-identical and creates no row.
  * 8 moved sites (create_thread x2, set_verdict, bump_revision, set_quorum x2,
    request_form_b, register_meta): rejected calls write NO liveness, no row.
  * F-2 (duplicate names in the resulting quorum) is rejected.
  * cold-start counter: a never-registered creator that succeeds gets
    f6_coldstart_count == 1 (kills the silent no-op regression, #1173 §三①).
  * 1.1b (C2): an invitee is registered but NOT present — new name gets
    first_seen set, last_seen NULL, last_read NULL, meta.invited_at (and
    invited_thread); an existing member's last_seen stays byte-identical when
    invited; presence is earned by the invitee's own first poll.
  * the mechanical gate (tools/check_touch_sites.py) prints 16 sites / 13 green
    / 3 documented out-of-scope red — numbers generated, never hand-written.
    (1.1a's numbers were 18/15/3; 1.1b moved the two invite loops to `_invite`.)

Baseline discipline (#1188 §二): before every "advanced" assertion the member's
last_seen is back-dated via UPDATE (never "now vs now"), and every rejection
asserts byte-identity. No clock injection, no new fixture mechanism.

Run: .venv/bin/python test_v212_touch_sites.py
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

import test_fixture as fx

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f" — {detail}" if detail and not cond else ""))


def ro(db, q, a=()):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        return c.execute(q, a).fetchone()
    finally:
        c.close()


def rw(db, q, a=()):
    """Write helper; returns the affected row count.

    Returns rowcount explicitly (not the cursor): the cursor dies with the
    connection in `finally`, so callers could never read `cur.rowcount` — that
    is exactly why the P15 fixture assert was impossible before (thread #91 §三).
    """
    c = sqlite3.connect(db, timeout=10)
    try:
        c.execute("PRAGMA busy_timeout=5000")
        cur = c.execute(q, a)
        count = cur.rowcount
        c.commit()
        return count
    finally:
        c.close()


def ls(db, n):
    r = ro(db, "SELECT last_seen FROM participants WHERE author=?", (n,))
    return r[0] if r else None


def has_row(db, n):
    return ro(db, "SELECT COUNT(*) FROM participants WHERE author=?", (n,))[0] == 1


def n_rows(db):
    return ro(db, "SELECT COUNT(*) FROM participants")[0]


def meta(db, n):
    r = ro(db, "SELECT meta FROM participants WHERE author=?", (n,))
    return json.loads(r[0] or "{}") if r else None


def backdate(db, n):
    """P15 three-piece fixture: (1) row pre-built by the caller, (2) the backdate
    ASSERTED (rowcount == 1 + sentinel read-back), (3) the sentinel legal.

    Without the assert, B2/D2's `ls(...) == base` is a false green when the row is
    missing: `ls()` returns None on both sides and "byte-identical" holds
    vacuously (thread #91 §三; B5/D4 would raise TypeError instead, so only the
    equality cases were silently green).  "Every case builds its row" is a
    fixture convention, not a check.
    """
    updated = rw(db, "UPDATE participants SET last_seen=datetime('now','-1 hour') WHERE author=?", (n,))
    assert updated == 1, (
        f"fixture: backdate matched {updated} rows for {n!r} "
        "(missing row => B2/D2 false green)")
    sentinel = ls(db, n)
    assert sentinel is not None, f"fixture: no last_seen read back for {n!r}"
    # legal sentinel: same shape as server._now_iso() (`%Y-%m-%d %H:%M:%S`),
    # parseable, and genuinely back-dated (~1h, not "now vs now")
    try:
        stamp = datetime.strptime(sentinel, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise AssertionError(
            f"fixture: backdated last_seen {sentinel!r} is not _now_iso()-shaped") from exc
    age_min = (datetime.now(timezone.utc).replace(tzinfo=None) - stamp).total_seconds() / 60
    assert 30 <= age_min <= 90, (
        f"fixture: backdated sentinel {sentinel!r} is {age_min:.1f} min old "
        "(want 30..90; the UPDATE no longer moves the row?)")
    return sentinel


def tok_of(out):
    m = re.search(r"\b[0-9a-f]{32}\b", out or "")
    return m.group(0) if m else ""


def main():
    tmp = tempfile.mkdtemp(prefix="rb-v212-")
    db = os.path.join(tmp, "t.db")
    srv = fx.stub_env(db)
    srv.init_db()
    with srv.db() as conn:
        srv._begin_write(conn)
        for n in ("a", "b", "c"):
            srv._touch(conn, n)
    fx.prime_beats(db, ["a", "b", "c"])

    tok = {}
    for n in ("a", "b", "c"):
        tok[n] = tok_of(srv.claim_token(n))
        assert tok[n], "fixture: no token for " + n
        srv.ack_token(n, tok[n])

    # ================= A. claim_token: registration lives in the issue branch
    print("\n=== A. claim_token first issue (the only site allowed to register)")
    before = n_rows(db)
    out = srv.claim_token("newbie")
    row = ro(db, "SELECT first_seen, last_seen FROM participants WHERE author='newbie'")
    check("A1 first issue succeeds", "Token issued" in out, out)
    check("A2 first issue creates exactly one row", n_rows(db) == before + 1)
    check("A3 first_seen == last_seen on the created row", bool(row) and row[0] == row[1], row)

    # ================= B. claim_token: rejected paths do not renew liveness
    print("\n=== B. claim_token rejects (rate limit / lock window) — the leak A fixes")
    old_min = srv.UNACKED_REISSUE_MIN
    srv.UNACKED_REISSUE_MIN = 10
    backdate(db, "newbie")
    base, cnt = ls(db, "newbie"), n_rows(db)
    out = srv.claim_token("newbie")
    check("B1 unacked rate-limit rejects", "ERROR" in out and "rate limit" in out, out)
    check("B2 rate-limit reject leaves last_seen byte-identical", ls(db, "newbie") == base)
    check("B3 rate-limit reject creates no row", n_rows(db) == cnt)

    srv.UNACKED_REISSUE_MIN = 0
    backdate(db, "newbie")
    base = ls(db, "newbie")
    out = srv.claim_token("newbie")
    check("B4 unacked reissue succeeds", "REISSUED" in out, out)
    check("B5 unacked reissue ADVANCES last_seen", ls(db, "newbie") > base)

    print("\n=== C. claim_token acked lock window vs reissue")
    with srv.db() as conn:
        srv._begin_write(conn)
        srv._audit(conn, "a", "verdict", thread_id=None, note="recent governance event")
    backdate(db, "a")
    base = ls(db, "a")
    out = srv.claim_token("a")
    check("C1 in-window acked claim rejects", "issued AND acknowledged" in out, out)
    check("C2 in-window reject leaves last_seen byte-identical", ls(db, "a") == base)
    rw(db, "UPDATE verdict_events SET created_at=datetime('now','-2 days') WHERE author='a'")
    backdate(db, "a")
    base = ls(db, "a")
    out = srv.claim_token("a")
    check("C3 acked reissue after window succeeds", "REISSUED" in out, out)
    check("C4 acked reissue ADVANCES last_seen", ls(db, "a") > base)
    tok["a"] = tok_of(out)
    srv.ack_token("a", tok["a"])

    # ================= D. ack_token
    print("\n=== D. ack_token: mismatch rejected, idempotent ack = presence (decision (a))")
    t_ackm = tok_of(srv.claim_token("ackm"))
    backdate(db, "ackm")
    base = ls(db, "ackm")
    out = srv.ack_token("ackm", "0" * 32)
    check("D1 wrong token rejects", "mismatch" in out, out)
    check("D2 wrong token leaves last_seen byte-identical", ls(db, "ackm") == base)
    backdate(db, "ackm")
    base = ls(db, "ackm")
    out = srv.ack_token("ackm", t_ackm)
    check("D3 correct ack succeeds", "acknowledged" in out, out)
    check("D4 correct ack ADVANCES last_seen", ls(db, "ackm") > base)
    backdate(db, "ackm")
    base = ls(db, "ackm")
    out = srv.ack_token("ackm", t_ackm)
    check("D5 idempotent ack is a non-ERROR success", "already acknowledged" in out, out)
    check("D6 idempotent ack ADVANCES last_seen (decision (a))", ls(db, "ackm") > base)
    before = n_rows(db)
    out = srv.ack_token("ghost_ack", "0" * 32)
    check("D7 ack without a token rejects", "ERROR" in out, out)
    check("D8 rejected ack creates no row", n_rows(db) == before)

    # ================= E. reset_token
    print("\n=== E. reset_token")
    before = n_rows(db)
    out = srv.reset_token("ghost_reset", human_override=True)
    check("E1 unknown author rejects", "unknown author" in out, out)
    check("E2 unknown-author reset creates no row (old code did)", n_rows(db) == before)
    backdate(db, "ackm")
    base = ls(db, "ackm")
    out = srv.reset_token("ackm", human_override=True)
    check("E3 reset succeeds", "cleared" in out, out)
    check("E4 successful reset ADVANCES last_seen", ls(db, "ackm") > base)

    # ================= F. create_thread: rejected calls write nothing
    print("\n=== F. create_thread: rejected calls write NO liveness / no invitee rows")
    backdate(db, "a")
    base, cnt = ls(db, "a"), n_rows(db)
    out = srv.create_thread(title="dupq", author="a", quorum=["b", "b"],
                            context=fx.wi("a", "b"))
    check("F1 duplicate quorum rejects", "duplicate names" in out, out)
    check("F2 rejected create leaves creator last_seen byte-identical", ls(db, "a") == base)
    check("F3 rejected create registers nobody", n_rows(db) == cnt)

    backdate(db, "a")
    base = ls(db, "a")
    out = srv.create_thread(title="noinfra", author="a", quorum=["a", "b"], context=None)
    check("F4 missing wake_infra rejects", "ERROR" in out, out)
    check("F5 wake_infra reject leaves creator last_seen byte-identical", ls(db, "a") == base)

    old_cap = srv.THREADS_PER_DAY
    srv.THREADS_PER_DAY = 1
    out = srv.create_thread(title="cap1", author="a")
    check("F6 first free thread within cap succeeds", "Created thread" in out, out)
    backdate(db, "a")
    base = ls(db, "a")
    out = srv.create_thread(title="cap2", author="a")
    check("F7 daily-cap rejects", "daily thread-creation limit" in out, out)
    check("F8 daily-cap reject leaves creator last_seen byte-identical", ls(db, "a") == base)
    srv.THREADS_PER_DAY = old_cap

    backdate(db, "a")
    base = ls(db, "a")
    out = srv.create_thread(title="okq", author="a", quorum=["a", "b", "inv1"],
                            context=fx.wi("a", "b", "inv1"))
    check("F9 successful create", "Created thread" in out, out)
    check("F10 successful create ADVANCES creator last_seen", ls(db, "a") > base)
    check("F11 successful create registers the invitee", has_row(db, "inv1"))
    tid_q = int(re.search(r"#(\d+)", out).group(1))

    print("\n=== G. cold start: counter survives the moved _touch (#1173 §三①)")
    out = srv.create_thread(title="cold", author="coldone",
                            quorum=["coldone", "b", "inv2"],
                            context=fx.wi("coldone", "b", "inv2"))
    check("G1 never-registered creator can create", "Created thread" in out, out)
    m = meta(db, "coldone")
    check("G2 f6_coldstart flag persisted on success path", bool(m) and m.get("f6_coldstart") is True, m)
    check("G3 f6_coldstart_count == 1 (not a silent no-op)", bool(m) and m.get("f6_coldstart_count") == 1, m)

    print("\n=== G2. set_quorum cold-start: gate writes nothing when the call is rejected")
    tok_cold = tok_of(srv.claim_token("coldq"))
    out = srv.create_thread(title="coldq-free", author="coldq")
    tid_coldq = int(re.search(r"#(\d+)", out).group(1))
    n_f6 = ro(db, "SELECT COUNT(*) FROM verdict_events WHERE action LIKE 'f6_%'")[0]
    out = srv.set_quorum(tid_coldq, "coldq", token=tok_cold, add=["gx", "gx"],
                         wake_infra=fx.wi("coldq", "gx"))
    check("G4 F-2 rejection after the cold-start gate", "duplicate names" in out, out)
    check("G5 rejected call leaves no f6_* audit row (sixth field)",
          ro(db, "SELECT COUNT(*) FROM verdict_events WHERE action LIKE 'f6_%'")[0] == n_f6)
    check("G6 rejected call leaves f6_coldstart_count unset",
          (meta(db, "coldq") or {}).get("f6_coldstart_count") is None,
          meta(db, "coldq"))
    out = srv.set_quorum(tid_coldq, "coldq", token=tok_cold, add=["gx", "gy"],
                         wake_infra=fx.wi("coldq", "gx", "gy"))
    check("G7 cold-start free→quorum succeeds", "Quorum updated" in out, out)
    mcold = meta(db, "coldq")
    check("G8 success path persists f6_coldstart_count == 1",
          bool(mcold) and mcold.get("f6_coldstart_count") == 1, mcold)
    check("G9 success path writes exactly one new f6_* audit row",
          ro(db, "SELECT COUNT(*) FROM verdict_events WHERE action LIKE 'f6_%'")[0] == n_f6 + 1)

    # ================= H. set_quorum: 3 negative cells + F-2 + success
    print("\n=== H. set_quorum rejects (already / not-in-remove / len<2 / F-2)")
    backdate(db, "a")
    base, cnt = ls(db, "a"), n_rows(db)
    out = srv.set_quorum(tid_q, "a", token=tok["a"], add=["b"], wake_infra=fx.wi("a", "b"))
    check("H1 'already in quorum' rejects", "already in quorum" in out, out)
    check("H2 last_seen byte-identical", ls(db, "a") == base)
    check("H3 no row registered", n_rows(db) == cnt)

    backdate(db, "a")
    base = ls(db, "a")
    out = srv.set_quorum(tid_q, "a", token=tok["a"], remove=["zzz"])
    check("H4 'not in current quorum' rejects", "not in current quorum" in out, out)
    check("H5 last_seen byte-identical", ls(db, "a") == base)

    backdate(db, "a")
    base = ls(db, "a")
    out = srv.set_quorum(tid_q, "a", token=tok["a"], remove=["b", "inv1"])
    check("H6 shrinking below 2 rejects", "self-judging floor" in out, out)
    check("H7 last_seen byte-identical", ls(db, "a") == base)

    out = srv.create_thread(title="free4f2", author="a")
    tid_free = int(re.search(r"#(\d+)", out).group(1))
    backdate(db, "a")
    base, cnt = ls(db, "a"), n_rows(db)
    out = srv.set_quorum(tid_free, "a", token=tok["a"], add=["x", "x"],
                         wake_infra=fx.wi("a", "x"))
    check("H8 F-2 duplicate resulting quorum rejects", "duplicate names" in out, out)
    check("H9 F-2 reject leaves last_seen byte-identical", ls(db, "a") == base)
    check("H10 F-2 reject registers no invitee", n_rows(db) == cnt and not has_row(db, "x"))

    backdate(db, "a")
    base = ls(db, "a")
    out = srv.set_quorum(tid_free, "a", token=tok["a"], add=["d", "e"],
                         wake_infra=fx.wi("a", "d", "e"))
    check("H11 valid amendment succeeds", "Quorum updated" in out, out)
    check("H12 valid amendment ADVANCES last_seen", ls(db, "a") > base)
    check("H13 valid amendment registers invitees", has_row(db, "d") and has_row(db, "e"))

    # ================= I. the other moved governance sites
    print("\n=== I. set_verdict / bump_revision / request_form_b / register_meta")
    cases = [
        ("I1 set_verdict bogus token", "b", lambda: srv.set_verdict(tid_q, "pass", "b", None, "0" * 32)),
        ("I2 set_verdict nonexistent thread", "b", lambda: srv.set_verdict(999999, "pass", "b", None, tok["b"])),
        ("I3 bump_revision bogus token", "a", lambda: srv.bump_revision(tid_q, "a", "0" * 32)),
        ("I4 set_quorum bogus token", "a", lambda: srv.set_quorum(tid_q, "a", token="0" * 32)),
        ("I5 request_form_b bogus token", "a", lambda: srv.request_form_b(tid_q, "a", ["f"], "0" * 32)),
        ("I6 register_meta bogus token", "b", lambda: srv.register_meta("b", "a", None, "0" * 32)),
    ]
    for label, who, fn in cases:
        backdate(db, who)
        base, cnt = ls(db, who), n_rows(db)
        out = fn()
        check(label + " rejects", "ERROR" in out, out)
        check(label + " leaves last_seen byte-identical", ls(db, who) == base, (base, ls(db, who)))
        check(label + " creates no row", n_rows(db) == cnt)

    backdate(db, "b")
    base = ls(db, "b")
    out = srv.set_verdict(tid_q, "pass", "b", None, tok["b"])
    check("I7 valid set_verdict succeeds", "Verdict recorded" in out, out)
    check("I8 valid set_verdict ADVANCES last_seen", ls(db, "b") > base)

    backdate(db, "a")
    base = ls(db, "a")
    out = srv.bump_revision(tid_q, "a", tok["a"])
    check("I9 valid bump_revision succeeds", "Revision bumped" in out or "rev" in out, out)
    check("I10 valid bump_revision ADVANCES last_seen", ls(db, "a") > base)

    backdate(db, "b")
    base = ls(db, "b")
    out = srv.list_comments_since(author="b", since="now")
    check("I11 poll still advances last_seen (site unchanged)", ls(db, "b") > base)

    # ================= K. 1.1b C2: invited != present
    print("\n=== K. 1.1b C2: invite registration is not presence")
    out = srv.create_thread(title="c2inv", author="a", quorum=["a", "k1", "k2"],
                            context=fx.wi("a", "k1", "k2"))
    check("K1 quorum create succeeds", "Created thread" in out, out)
    tid_c2 = int(re.search(r"#(\d+)", out).group(1))
    created = ro(db, "SELECT created_at FROM threads WHERE id=?", (tid_c2,))[0]
    k1 = ro(db, "SELECT first_seen, last_seen, last_read, meta FROM participants WHERE author='k1'")
    k1m = json.loads(k1[3] or "{}") if k1 else {}
    check("K2 invitee row exists", bool(k1))
    check("K3 invitee last_seen IS NULL (not stamped by the invite)", bool(k1) and k1[1] is None, k1)
    check("K4 invitee first_seen is set", bool(k1) and bool(k1[0]), k1)
    check("K5 invitee first_seen == threads.created_at (synthetic baseline shape)",
          bool(k1) and k1[0] == created, (k1[0], created))
    check("K6 invitee last_seen != threads.created_at (old C2 failure gone)",
          bool(k1) and k1[1] != created, (k1[1], created))
    check("K7 meta.invited_at recorded", k1m.get("invited_at") is not None, k1m)
    check("K8 meta.invited_thread recorded", k1m.get("invited_thread") == tid_c2, k1m)
    check("K9 invitee last_read IS NULL (first-wake semantics kept)",
          bool(k1) and k1[2] is None, k1)
    with srv.db() as conn:
        active = srv._active_quorum(conn, ["a", "k1", "k2"])
    check("K10 invitee is NOT counted active before showing up", "k1" not in active and "k2" not in active, active)

    backdate(db, "b")
    base, cnt = ls(db, "b"), n_rows(db)
    out = srv.set_quorum(tid_c2, "a", token=tok["a"], add=["b"],
                         wake_infra=fx.wi("a", "b"))
    check("K11 set_quorum add succeeds", "Quorum updated" in out, out)
    bm = meta(db, "b")
    check("K12 existing member's last_seen byte-identical when invited", ls(db, "b") == base)
    check("K13 existing member got meta.invited_at (no new row)", n_rows(db) == cnt and bm.get("invited_at") is not None, bm)
    check("K14 existing member's invited_thread recorded", bm.get("invited_thread") == tid_c2, bm)

    out = srv.set_quorum(tid_c2, "a", token=tok["a"], add=["k3"],
                         wake_infra=fx.wi("a", "k3"))
    k3 = ro(db, "SELECT first_seen, last_seen, last_read, meta FROM participants WHERE author='k3'")
    k3m = json.loads(k3[3] or "{}") if k3 else {}
    check("K15 set_quorum invite succeeds", "Quorum updated" in out, out)
    check("K16 set_quorum invitee last_seen IS NULL / first_seen set",
          bool(k3) and k3[1] is None and bool(k3[0]), k3)
    check("K17 set_quorum invitee meta.invited_at + invited_thread",
          k3m.get("invited_at") is not None and k3m.get("invited_thread") == tid_c2, k3m)

    srv.list_comments_since(author="k1", since="now")
    check("K18 presence is earned: first poll sets last_seen", ls(db, "k1") is not None, ls(db, "k1"))
    with srv.db() as conn:
        active2 = srv._active_quorum(conn, ["a", "k1", "k2"])
    check("K19 polled invitee becomes active (k2 still not)", "k1" in active2 and "k2" not in active2, active2)

    # ---- read faces: three states, not two (dsh #1293 §二) ----
    # A never-present invitee must never be rendered as C1-suspended / “watcher
    # broken”: the distinction is last_seen IS NULL ∧ meta.invited_at, and it is
    # pinned here on the three read faces that used to fold it away.
    roster = srv.list_participants()
    check("K20 roster labels invitee 受邀未到场 (not ⚠️停摆)",
          "k2 [受邀未到场]" in roster and "k2 [⚠️停摆]" not in roster, roster)
    seats = srv.get_thread(tid_c2)
    k2_lines = [l for l in seats.splitlines() if "k2" in l]
    check("K21 seat list labels invitee invited(未到场), not suspended",
          "[invited(未到场)]" in seats and "k2: — never voted [suspended]" not in seats,
          k2_lines)
    prof = json.loads(srv.reliability_profile("k2"))["participation"]["heartbeat"]
    check("K22 profile: invited_now=True, suspended_now=False",
          prof.get("invited_now") is True and prof.get("suspended_now") is False, prof)
    check("K23 polled invitee reads 活跃, unbudged invitee still 受邀未到场",
          "k1 [活跃]" in roster and "k2 [受邀未到场]" in roster, roster)

    # dsh #1296 §二: the FOURTH read face is the `/board` HTML strip, which K20-K23
    # never touched ("0 test calls to _board_html").  An invitee used to render as
    # "⚠️ k2 从未(受邀未到场)" — badge says "watcher broken", text says "never shown
    # up". Pin the third state on the rendered markup, not on the helper.
    board = srv._board_html()
    check("K25 board HTML: invitee badge is 📨, never ⚠️ (fourth read face)",
          "<span class='p'>📨 k2 从未(受邀未到场)" in board and "⚠️ k2" not in board,
          [l for l in board.splitlines() if "k2" in l and "<span class='p'>" in l])

    # dsh #1293 §五1: an unparsable meta belongs to someone else (token_hash /
    # probe / hb_read).  Inviting must SKIP the mark, never "repair" the JSON by
    # overwriting it — that would let an inviter destroy another member's token.
    rw(db, "UPDATE participants SET meta='{broken' WHERE author='k2'")
    with srv.db() as conn:
        srv._invite(conn, "k2", tid_c2)
    check("K24 invite over unparsable meta is skipped, not overwritten",
          ro(db, "SELECT meta FROM participants WHERE author='k2'")[0] == "{broken",
          ro(db, "SELECT meta FROM participants WHERE author='k2'"))

    # ================= J. mechanical gate
    print("\n=== J. mechanical gate (numbers generated by tools/check_touch_sites.py)")
    r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                    "tools", "check_touch_sites.py")],
                       capture_output=True, text=True)
    check("J1 AST gate passes (16 sites / 13 green / 3 documented red; check-5 invite split)", r.returncode == 0,
          (r.stdout + r.stderr)[-600:])
    r2 = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                     "tools", "check_touch_sites.py"), "--self-test"],
                        capture_output=True, text=True)
    check("J2 gate-4 self-test proves the check can go red", r2.returncode == 0,
          (r2.stdout + r2.stderr)[-400:])

    print("\n" + "=" * 60)
    print(f"v2.12.0 1.1a: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
