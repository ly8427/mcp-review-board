#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""票9 rev2 (dsh #763, 2026-10-24 收敛) 验收测试——2.3 五项的可机械判载体。

① QUORUM_OPEN_CAP=3：第 4 张 quorum 帖拒收（期望原文逐字）+ get_thread 整数字段
② verdict_overdue：帖龄>24h ∧ quorum ∧ never voted ∧ 活跃 → 名单；<24h → []（空数组）
   纯读（C3）：调用前后 verdict_events 行数不变
③ 触顶文案第二句 "Voting (set_verdict) is NOT affected by this cap."（post/reply 两处）
④ 掉线收尾：协议正文「暂缓 ≠ veto」+ (b) 告知句（ledger 闸行为由 v2.9 既有套件覆盖）
⑤ summary：仅创建者、每帖一条免费（不计 _author_usage）、THREAD_CAP 仍约束；
   非创建者拒收原文 `ERROR: summary comments are creator-only`

Run: python test_v213_ticket9.py
"""
import os
import re
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_fixture as fx

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else " ❌ ") + name + (f" — {detail}" if detail and not cond else ""))


def ro(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


TOK: dict[str, str] = {}


def make_thread(srv, db, author="anna", members=("anna", "brin", "cyr"), title="t9", budget=None,
                quorum=True):
    with srv.db() as conn:
        srv._begin_write(conn)
        for n in members:
            srv._touch(conn, n)
        conn.commit()
    fx.prime_beats(db, list(members))
    out = srv.create_thread(title=title, author=author,
                            quorum=list(members) if quorum else None,
                            context=fx.wi(*members) if quorum else None,
                            per_author_budget=budget)
    assert out.startswith("Created thread"), out
    tid = int(re.search(r"#(\d+)", out).group(1))
    for n in members:
        if n not in TOK:
            m = re.search(r"[0-9a-f]{32}", srv.claim_token(author=n))
            TOK[n] = m.group(0)
            srv.ack_token(author=n, token=TOK[n])
    return tid


def audit_count(db):
    c = ro(db)
    try:
        return c.execute("SELECT COUNT(*) FROM verdict_events").fetchone()[0]
    finally:
        c.close()


def main():
    tmp = tempfile.mkdtemp(prefix="rb-t9-")
    db = os.path.join(tmp, "t9.db")
    srv = fx.stub_env(db)
    srv.init_db()

    # ================= ① 并发上限 + open_quorum_count ========================
    print("\n=== ① QUORUM_OPEN_CAP=3 ===")
    tids = []
    for i in range(3):
        tids.append(make_thread(srv, db, title=f"t9-cap-{i}"))
    out = srv.create_thread(title="t9-cap-overflow", author="anna",
                            quorum=["anna", "brin", "cyr"], context=fx.wi("anna", "brin", "cyr"))
    expect_head = ("ERROR: open quorum threads = 3 (cap 3): "
                   f"#{tids[0]},#{tids[1]},#{tids[2]}; "
                   "resolve or conclude them first.")
    check("①a 第 4 张拒收原文逐字", out == expect_head, out[:160])
    gt = srv.get_thread(thread_id=tids[0])
    check("①b get_thread 整数字段 open_quorum_count", "open_quorum_count: 3 (cap 3)" in gt,
          [ln for ln in gt.splitlines() if "open_quorum_count" in ln][:1])

    # ================= ② verdict_overdue（纯读 C3） ===========================
    print("\n=== ② verdict_overdue ===")
    srv.list_comments_since(author="brin", since="now")   # 预热：F6 read-epoch 一次性引导
    c = ro(db)
    before = c.execute("SELECT COUNT(*) FROM verdict_events").fetchone()[0]
    c.close()
    c = sqlite3.connect(db, timeout=10)
    c.execute("UPDATE threads SET created_at='2026-11-01 00:00:00' WHERE id=?", (tids[0],))
    c.commit()
    c.close()
    hdr = srv.list_comments_since(author="brin", since="now").splitlines()[0]
    check("②a >24h 无票帖 → 名单含全部未投活跃成员",
          '"verdict_overdue": ["anna", "brin", "cyr"]' in hdr, hdr[:200])
    c = sqlite3.connect(db, timeout=10)
    c.execute("UPDATE threads SET created_at=datetime('now') WHERE id=?", (tids[0],))
    c.commit()
    c.close()
    hdr2 = srv.list_comments_since(author="brin", since="now").splitlines()[0]
    check("②b <24h → 空数组（不是缺字段）", '"verdict_overdue": []' in hdr2, hdr2[:200])
    after = audit_count(db)
    check("②c 纯读：verdict_events 行数不变", before == after, f"{before} → {after}")

    # ================= ③ 触顶文案第二句（post/reply 两处） ====================
    print("\n=== ③ 触顶文案第二句 ===")
    srv.THREAD_CAP = 3                     # 模块常量运行时收窄（仅本测试进程）
    free_tid = make_thread(srv, db, author="anna", members=("anna",),
                           title="t9-cap-text", quorum=False)   # free thread（quorum=None 不受限）
    for i in range(3):
        r = srv.post_comment(thread_id=free_tid, author="anna", body=f"fill {i}")
        assert r.startswith("Posted"), r
    r = srv.post_comment(thread_id=free_tid, author="anna", body="over cap")
    check("③a post_comment 第二句逐字",
          r.startswith("ERROR: thread #") and "locked — discussion cap reached" in r
          and r.rstrip().endswith("Voting (set_verdict) is NOT affected by this cap."), r[:200])
    v = srv.set_verdict(thread_id=free_tid, author="anna", verdict="pass", token=TOK["anna"])
    check("③b 同帖 set_verdict 不返回 locked", "locked" not in v, v[:120])
    srv.THREAD_CAP = 100                   # 还原

    # ================= ④ 协议正文两句 ========================================
    print("\n=== ④ 掉线收尾协议句 ===")
    check("④a 暂缓 ≠ veto 句在协议正文", "暂缓 ≠ veto" in srv.PROTOCOL)
    check("④b (b) 告知句在协议正文",
          "静默排除不存在" in srv.PROTOCOL and "wake_ledger" in srv.PROTOCOL)

    # ================= ⑤ summary 免费条 ======================================
    print("\n=== ⑤ summary 创建者免费条 ===")
    c = sqlite3.connect(db, timeout=10)
    c.execute("UPDATE threads SET status='resolved' WHERE id IN (?,?,?)", tids)
    c.commit()
    c.close()                               # 夹具前置：腾出 cap 名额（非被测路径）
    tid = make_thread(srv, db, author="anna", members=("anna", "brin", "cyr"), title="t9-sum",
                      budget=21)            # 预算 21：留 1 条计费余量，用于证明第二条 summary 按计费
    for i in range(20):                    # 发 20 条 → used=20，余量 1
        r = srv.post_comment(thread_id=tid, author="anna", body=f"fill {i}")
        assert r.startswith("Posted"), r
    with srv.db() as conn:
        used = srv._author_usage(conn, tid, "anna")
    check("⑤a 已用 20（余量 1）", used == 20, str(used))
    r = srv.post_comment(thread_id=tid, author="anna", body="billed post in headroom")
    check("⑤b 计费帖在余量内放行（used→21）", r.startswith("Posted"), r[:120])
    r = srv.post_comment(thread_id=tid, author="anna", body="creator closing summary",
                         summary=True)
    check("⑤c 满额(21/21)时 summary=True 旁路预算放行", r.startswith("Posted"), r[:120])
    with srv.db() as conn:
        used2 = srv._author_usage(conn, tid, "anna")
        flagged = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE thread_id=? AND author=?"
            " AND COALESCE(summary_free,0)=1", (tid, "anna")).fetchone()[0]
    check("⑤d 免费条不计 _author_usage（used 仍 21）", used2 == 21, str(used2))
    check("⑤e 行级 summary_free 打标恰 1 条", flagged == 1, str(flagged))
    r = srv.post_comment(thread_id=tid, author="brin", body="brin tries summary", summary=True)
    check("⑤f 非创建者原文拒收", r == "ERROR: summary comments are creator-only", r[:120])
    r = srv.post_comment(thread_id=tid, author="anna", body="second summary billed",
                         summary=True)
    check("⑤g 第二条 summary 按普通计费（满额被预算门拒=计费证据）",
          r.startswith("ERROR: anna reached the per-author budget (21) in thread #"), r[:130])
    with srv.db() as conn:
        used3 = srv._author_usage(conn, tid, "anna")
        flagged2 = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE thread_id=? AND author=?"
            " AND COALESCE(summary_free,0)=1", (tid, "anna")).fetchone()[0]
    check("⑤h used 仍 21 ∧ 免费打标仍 1", used3 == 21 and flagged2 == 1,
          f"used={used3} flagged={flagged2}")

    print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
    if FAIL:
        for name in FAIL:
            print("  FAIL:", name)
        sys.exit(1)


if __name__ == "__main__":
    main()
