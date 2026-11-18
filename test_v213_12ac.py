#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1.2a preflight（票5 绑定：participants.meta 逐字节不变）+ 1.2c get_thread
分页（#1049 §四②：默认=现行为，watcher/SDK 读面零变化）验收测试。

Run: python test_v213_12ac.py
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


def meta_blob(db):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        return c.execute(
            "SELECT author, meta FROM participants ORDER BY author").fetchall()
    finally:
        c.close()


def main():
    tmp = tempfile.mkdtemp(prefix="rb-12ac-")
    db = os.path.join(tmp, "d.db")
    srv = fx.stub_env(db)
    srv.init_db()
    members = ("anna", "brin", "cyr")
    with srv.db() as conn:
        srv._begin_write(conn)
        for n in members:
            srv._touch(conn, n)
        conn.commit()
    fx.prime_beats(db, list(members))
    out = srv.create_thread(title="12ac", author="anna", quorum=list(members),
                            context=fx.wi(*members))
    assert out.startswith("Created thread"), out
    tid = int(re.search(r"#(\d+)", out).group(1))
    for i in range(5):                     # 5 条顶层 + 回复若干
        r = srv.post_comment(thread_id=tid, author="brin", body=f"top {i}")
        cid = int(re.search(r"#(\d+)", r).group(1))
        srv.reply_comment(comment_id=cid, author="cyr", body=f"re {i}")

    # ================= 1.2a preflight（票5 绑定） ============================
    print("\n=== 1.2a preflight ===")
    before = meta_blob(db)
    r = srv.preflight(author="anna")
    after = meta_blob(db)
    check("A1 三列读数在返回体", "last_seen=" in r and "last_read=" in r
          and "last_delivered_id=" in r, r[:140])
    check("A2 participants.meta 逐字节不变（票5 绑定）", before == after)
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    n_audit_after = c.execute("SELECT COUNT(*) FROM verdict_events").fetchone()[0]
    c.close()
    r2 = srv.preflight(author="anna")
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    n_audit_after2 = c.execute("SELECT COUNT(*) FROM verdict_events").fetchone()[0]
    c.close()
    check("A3 纯读：verdict_events 行数不变", n_audit_after == n_audit_after2)
    r = srv.preflight(author="nobody")
    check("A4 未注册 → 显式报错", r.startswith("ERROR: participant"), r[:100])

    # ================= 1.2c get_thread 分页 ==================================
    print("\n=== 1.2c get_thread 分页 ===")
    full = srv.get_thread(thread_id=tid)
    check("C1 默认（page=None）= 全量现行为",
          full.count("top ") == 5 and "page:" not in full, f"tops={full.count('top ')}")
    p1 = srv.get_thread(thread_id=tid, page=1, per_page=2)
    p2 = srv.get_thread(thread_id=tid, page=2, per_page=2)
    p3 = srv.get_thread(thread_id=tid, page=3, per_page=2)
    check("C2 page 标记行在场", "page: 1/3" in p1 and "page: 2/3" in p2 and "page: 3/3" in p3)
    tops1 = len(re.findall(r"^- #\d+ ", p1, re.M))
    check("C3 每页顶层评论数=per_page", tops1 == 2, str(tops1))
    check("C4 回复随父评论同页（不被分页孤化）", "re 0" in p1 and "re 1" in p1)
    ids1 = re.findall(r"^- #(\d+)", p1, re.M)
    ids2 = re.findall(r"^- #(\d+)", p2, re.M)
    check("C5 页间无重叠", not set(ids1) & set(ids2), f"{ids1} ∩ {ids2}")
    all_ids = set()
    for p in (p1, p2, p3):
        all_ids |= set(re.findall(r"^- #(\d+)", p, re.M))
    check("C6 三页并集 = 默认全量顶层（无丢失）", len(all_ids) == 5, str(sorted(all_ids)))
    r = srv.get_thread(thread_id=tid, page=9, per_page=2)
    check("C7 越界显式报错", r.startswith("ERROR: page 9 out of range"), r[:100])
    r = srv.get_thread(thread_id=tid, page=0)
    check("C8 page<1 显式报错", r.startswith("ERROR: page must be >= 1"), r[:100])

    print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
    if FAIL:
        for name in FAIL:
            print("  FAIL:", name)
        sys.exit(1)


if __name__ == "__main__":
    main()
