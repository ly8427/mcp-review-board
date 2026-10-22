#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.9.3 resolve-order regressions (thread #55 / 票10, dsh #640).

Covers the three defects in one file, each with a CONSTRUCTED fixture
(stubbed fastmcp, isolated temp DB — the dsh #496 / pi #499 pattern):

  1. ① cursor-before-recompute: a creator who is one poll behind heals on the
     VERY NEXT poll (old order: that poll blocked itself with
     reason=creator_delivery_lag: delivered=old < max_id).
  2. ② comment triggers recompute: the last discussion-floor comment closes the
     thread without anybody polling afterwards.
  3. ③ audit dedupe: N repeated heartbeats on a stuck thread leave exactly ONE
     auto_resolve_blocked row per (thread, revision, reason).

Run: .venv/bin/python test_v293_resolve_order.py
Red-first: `git stash` server.py and re-run — case 1 and 2 go red on b8021eb.
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_fixture as fx

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f" — {detail}" if detail and not cond else ""))


def _ro(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def status_of(db, tid):
    c = _ro(db)
    try:
        return c.execute("SELECT status FROM threads WHERE id=?", (tid,)).fetchone()[0]
    finally:
        c.close()


def blocked_rows(db, tid, reason):
    """All auto_resolve_blocked rows of one reason for a thread."""
    c = _ro(db)
    try:
        return [r[0] for r in c.execute(
            "SELECT note FROM verdict_events WHERE thread_id=? AND action='auto_resolve_blocked'"
            " AND note LIKE ? ORDER BY id", (tid, f"reason={reason}%"))]
    finally:
        c.close()


def post_id(srv, tid, author, body):
    out = srv.post_comment(thread_id=tid, author=author, body=body)
    assert out.startswith("Posted"), out
    return int(out.split("#")[1].split(" ")[0])


def main():
    tmp = tempfile.mkdtemp(prefix="rb-v293-")
    db = os.path.join(tmp, "r.db")
    srv = fx.stub_env(db)
    srv.init_db()

    with srv.db() as conn:
        srv._begin_write(conn)
        for n in ("anna", "brin", "cyr"):
            srv._touch(conn, n)
        conn.commit()
    fx.prime_beats(db, ["anna", "brin", "cyr"])
    ctx = fx.wi("anna", "brin", "cyr")
    import re
    tok = {}
    for n in ("anna", "brin", "cyr"):
        m = re.search(r"[0-9a-f]{32}", srv.claim_token(author=n))
        assert m, "token claim failed"
        tok[n] = m.group(0)

    # ================= 1. 一次轮询即关门（游标先写、复算后跑） =================
    out = srv.create_thread(title="R1", author="anna",
                            quorum=["brin", "cyr"], context=ctx)
    assert out.startswith("Created thread"), out
    t1 = int(re.search(r"#(\d+)", out).group(1))

    post_id(srv, t1, "brin", "brin floor")
    post_id(srv, t1, "cyr", "cyr floor")
    assert srv.set_verdict(thread_id=t1, verdict="pass", author="brin", token=tok["brin"]).startswith("Verdict")
    out = srv.set_verdict(thread_id=t1, verdict="pass", author="cyr", token=tok["cyr"])
    assert out.startswith("Verdict"), out

    st = status_of(db, t1)
    lag = blocked_rows(db, t1, "creator_delivery_lag")
    check("precondition: last vote blocked by creator_delivery_lag (creator never polled)",
          st == "open" and len(lag) == 1, f"status={st} rows={lag}")

    # watermark moved (a new co-member comment) ⇒ new fact ⇒ a second row is
    # legitimate (rev2 判据 3: dedupe must not swallow `delivered/max_id` changes)
    post_id(srv, t1, "cyr", "cyr follow-up (max_id advances)")
    lag = blocked_rows(db, t1, "creator_delivery_lag")
    check("#55③ rev2: a moving max_id produces a NEW lag row (not swallowed)",
          len(lag) == 2, f"rows={len(lag)}")

    # exactly ONE poll: old order left this thread open (the poll itself blocked)
    srv.list_comments_since(since="1h", author="anna")
    st = status_of(db, t1)
    lag = blocked_rows(db, t1, "creator_delivery_lag")
    check("#55①: creator's NEXT poll resolves in that same poll (no second beat)",
          st == "resolved", f"status={st}")
    check("#55①: the healing poll adds no new creator_delivery_lag row",
          len(lag) == 2, f"rows={len(lag)}")

    # ================= 2. 评论触发复算（地板最后一条=创建者自己发的） =================
    out = srv.create_thread(title="R2", author="anna",
                            quorum=["anna", "brin"], context=ctx)
    assert out.startswith("Created thread"), out
    t2 = int(re.search(r"#(\d+)", out).group(1))

    post_id(srv, t2, "brin", "brin floor")
    assert srv.set_verdict(thread_id=t2, verdict="pass", author="brin", token=tok["brin"]).startswith("Verdict")
    srv.list_comments_since(since="1h", author="anna")      # creator catches up (delivery gate clear)
    out = srv.set_verdict(thread_id=t2, verdict="pass", author="anna", token=tok["anna"])
    assert out.startswith("Verdict"), out
    check("precondition: floor still missing the creator's own comment",
          status_of(db, t2) == "open")

    # ================= 3. 审计去重（同一 帖×修订×reason 只留一条） =================
    for _ in range(3):
        with srv.db() as conn:
            srv._begin_write(conn)
            srv.heartbeat_and_recompute(conn, "anna")
    rows = blocked_rows(db, t2, "discussion_floor")
    check("#55③: 3 extra heartbeats leave exactly ONE discussion_floor audit row",
          len(rows) == 1, f"rows={len(rows)}")

    # The creator's own comment is the last missing floor item; posting it must
    # close the thread with no poll afterwards (creator's own comments are not
    # owed delivery — P2).
    post_id(srv, t2, "anna", "anna floor (closing comment)")
    check("#55②: last floor comment closes the thread without any poll",
          status_of(db, t2) == "resolved", f"status={status_of(db, t2)}")

    # ================= 4. 回归：正常路径只留一条 auto_resolve =================
    c = _ro(db)
    try:
        n_res = c.execute(
            "SELECT COUNT(*) FROM verdict_events WHERE thread_id=? AND action='auto_resolve'",
            (t1,)).fetchone()[0]
    finally:
        c.close()
    check("regression: exactly one auto_resolve event on the healed thread",
          n_res == 1, f"n={n_res}")

    # ========== 5. C' 冻结对象：不得被别人的心跳掀开（#55② 引出的潜伏缺陷） ==========
    out = srv.create_thread(title="R3", author="anna",
                            quorum=["anna", "brin", "cyr"], context=ctx)
    assert out.startswith("Created thread"), out
    t3 = int(re.search(r"#(\d+)", out).group(1))
    for m in ("anna", "brin", "cyr"):
        post_id(srv, t3, m, f"{m} floor")
    assert srv.set_verdict(thread_id=t3, verdict="pass", author="brin", token=tok["brin"]).startswith("Verdict")
    assert srv.set_verdict(thread_id=t3, verdict="object", author="cyr", token=tok["cyr"],
                           note="hold").startswith("Verdict")
    assert srv.set_verdict(thread_id=t3, verdict="pass", author="anna", token=tok["anna"]).startswith("Verdict")
    check("precondition: active objector keeps the thread open",
          status_of(db, t3) == "open")

    # creator catches up on delivery (otherwise the §3 gate blocks the close)
    srv.list_comments_since(since="1h", author="anna")

    # cyr suspends (2 days); the creator's ledger lets the active pair close
    cc = sqlite3.connect(db, timeout=10)
    cc.execute("UPDATE participants SET last_seen=datetime('now','-2 days') WHERE author='cyr'")
    cc.commit()
    cc.close()
    post_id(srv, t3, "anna", "wake_ledger:\n- name: cyr | result: timeout")
    check("C': resolve over a frozen objection (ledger satisfied)",
          status_of(db, t3) == "resolved", f"status={status_of(db, t3)}")

    # other members' heartbeats must NOT reopen it (old code did — resolve↔reopen flap)
    for m in ("anna", "brin"):
        srv.list_comments_since(since="now", author=m)
    check("C': stays resolved across other members' heartbeats (no flap)",
          status_of(db, t3) == "resolved", f"status={status_of(db, t3)}")

    # the objector returns ⇒ its object re-stands ⇒ reopen (unbilled)
    srv.list_comments_since(since="now", author="cyr")
    check("C': reopens when the frozen objector actually returns",
          status_of(db, t3) == "open", f"status={status_of(db, t3)}")

    # ========== 6. 去重语义单元测：相同 note 吞；状态迁移后重置（rev2 判据 3） ==========
    def _n(_reason="unit_probe"):
        return len(blocked_rows(db, t2, _reason))

    base = _n()
    with srv.db() as conn:
        srv._begin_write(conn)
        srv._audit_blocked_once(conn, t2, 9, "unit_probe", "reason=unit_probe: same")
        srv._audit_blocked_once(conn, t2, 9, "unit_probe", "reason=unit_probe: same")
    once = _n() - base
    with srv.db() as conn:
        srv._begin_write(conn)
        srv._audit(conn, "system", "auto_reopen", thread_id=t2, revision=9, note="unit")
        srv._audit_blocked_once(conn, t2, 9, "unit_probe", "reason=unit_probe: same")
    twice = _n() - base
    check("#55③ rev2: identical note suppressed (1 row), but a state transition resets (2 rows)",
          once == 1 and twice == 2, f"once={once} after_transition={twice}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
