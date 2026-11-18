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

    ticket9_initiate(srv, tmp, tid, db)

    print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
    if FAIL:
        for name in FAIL:
            print("  FAIL:", name)
        sys.exit(1)




def ticket9_initiate(srv, tmp, tid, db):
    """票7 2.2 (pi #2349 §2)：nonce initiate——建帖前发起+建帖时消费。"""
    print("\n=== ⑥ 2.2 nonce initiate ===")
    import pathlib
    srv.DATA_DIR = pathlib.Path(tmp)      # 用户侧交付文件重定向到测试目录（不污染活板 data/）
    # 反例：initiate 带 thread_id（先建帖再补 nonce）→ 逐字拒
    r = srv.request_form_b(thread_id=tid, author="anna", names=["anna", "brin"],
                           token=TOK["anna"], purpose="initiate", title="x")
    check("⑥a 先建后补逐字拒", r.startswith("ERROR: initiate mode is pre-thread only"), r[:130])
    # 正路：建帖前发起（绑定 标题哈希+名单+发起者）
    r = srv.request_form_b(thread_id=None, author="anna", names=["anna", "brin", "cyr"],
                           token=TOK["anna"], purpose="initiate", title="ticket9-initiate")
    check("⑥b initiate 发起放行", r.startswith("form_b initiate nonce requested"), r[:130])
    nonce_file = pathlib.Path(tmp) / "form_b_nonce.txt"
    m = re.search(r"nonce=([0-9a-f]{32})", nonce_file.read_text(encoding="utf-8"))
    check("⑥c nonce 用户侧文件可取", m is not None)
    nonce = m.group(1) if m else "0" * 32
    # 绑定不符：标题不对 → 拒（且不消费）
    r = srv.create_thread(title="ticket9-WRONG", author="anna",
                          quorum=["anna", "brin", "cyr"], context=fx.wi("anna", "brin", "cyr"),
                          form_b_nonce=nonce)
    check("⑥d 标题哈希不符拒收", r.startswith("ERROR: form_b_nonce rejected"), r[:130])
    # 名单不符：quorum 超出绑定名单 → 拒
    r = srv.create_thread(title="ticket9-initiate", author="anna",
                          quorum=["anna", "brin", "cyr", "dsh"], context=fx.wi("anna", "brin", "cyr"),
                          form_b_nonce=nonce)
    check("⑥e 名单超出绑定拒收", r.startswith("ERROR: form_b_nonce rejected: quorum names"), r[:130])
    # 正路消费：同标题同名单建帖 → 放行且消费
    r = srv.create_thread(title="ticket9-initiate", author="anna",
                          quorum=["anna", "brin", "cyr"], context=fx.wi("anna", "brin", "cyr"),
                          form_b_nonce=nonce)
    check("⑥f 绑定全对 → 建帖放行", r.startswith("Created thread"), r[:130])
    tid2 = int(re.search(r"#(\d+)", r).group(1))
    # 复用：已消费的 nonce 二次建帖 → 拒
    r = srv.create_thread(title="ticket9-initiate", author="anna",
                          quorum=["anna", "brin", "cyr"], context=fx.wi("anna", "brin", "cyr"),
                          form_b_nonce=nonce)
    check("⑥g 已消费复用拒收", r.startswith("ERROR: form_b_nonce rejected: initiate nonce already consumed"), r[:130])
    # TTL：直接把 expires_at 改到过去 → 拒
    r = srv.request_form_b(thread_id=None, author="anna", names=["anna", "brin", "cyr"],
                           token=TOK["anna"], purpose="initiate", title="ticket9-expired")
    m2 = re.search(r"nonce=([0-9a-f]{32})", nonce_file.read_text(encoding="utf-8"))
    c = sqlite3.connect(db, timeout=10)
    import json as _json
    th = __import__("hashlib").sha256("ticket9-expired".encode()).hexdigest()[:16]
    row = c.execute("SELECT value FROM meta WHERE key=?", (f"form_b_initiate_{th}",)).fetchone()[0]
    pend = _json.loads(row)
    pend["expires_at"] = 0
    c.execute("UPDATE meta SET value=? WHERE key=?", (_json.dumps(pend), f"form_b_initiate_{th}"))
    c.commit()
    c.close()
    r = srv.create_thread(title="ticket9-expired", author="anna",
                          quorum=["anna", "brin", "cyr"], context=fx.wi("anna", "brin", "cyr"),
                          form_b_nonce=m2.group(1))
    check("⑥h TTL 过期拒收", r.startswith("ERROR: form_b_nonce rejected: initiate nonce expired"), r[:130])


if __name__ == "__main__":
    main()
