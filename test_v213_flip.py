#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""方案 a L1 验收——set_verdict('object') 翻转条件格式强制（board #120
#2507-#2600 可行性定稿；用户 2026-11-19 批方向）。

钉死条款（dsh #2510 §5 五条款 + 评审链增量）：
  正控制（vacuous 必绿）：note 含 `flip: 当我改变看法时` 的 object 必须被接受
      ——若被拒 ⇒ 机制强于声明 ⇒ treatment 描述作废（#2510 §5-2）
  负控制（缺行必红）：note 非空但无 flip: 行 ⇒ 必拒（#2510 §5-3；今日读数=不拒，
      即 L1 未落地的机判）
  拒绝计数单列：verdict_events action=flip_condition_missing 可计数（#2578 §3）
  豁免：存量 verdicts 行从不重验（写侧检查 ⇒ 旧 objection 天然豁免）；pass 票不受影响
Run: python test_v213_flip.py
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


def make_thread(srv, db, title="flip-l1"):
    members = ("anna", "brin", "cyr")
    with srv.db() as conn:
        srv._begin_write(conn)
        for n in members:
            srv._touch(conn, n)
        conn.commit()
    fx.prime_beats(db, list(members))
    out = srv.create_thread(title=title, author="anna", quorum=list(members),
                            context=fx.wi(*members))
    assert out.startswith("Created thread"), out
    tid = int(re.search(r"#(\d+)", out).group(1))
    for n in members:
        if n not in TOK:
            m = re.search(r"[0-9a-f]{32}", srv.claim_token(author=n))
            TOK[n] = m.group(0)
            srv.ack_token(author=n, token=TOK[n])
    return tid


def flip_missing_count(db, tid):
    c = ro(db)
    try:
        return c.execute(
            "SELECT COUNT(*) FROM verdict_events WHERE thread_id=?"
            " AND action='flip_condition_missing'", (tid,)).fetchone()[0]
    finally:
        c.close()


def main():
    tmp = tempfile.mkdtemp(prefix="rb-flip-")
    db = os.path.join(tmp, "f.db")
    srv = fx.stub_env(db)
    srv.init_db()
    tid = make_thread(srv, db)

    print("\n=== 负控制：note 非空但无 flip: 行 ⇒ 必拒 + 计数单列 ===")
    r = srv.set_verdict(thread_id=tid, verdict="object", author="brin",
                        note="I disagree with this approach entirely",
                        token=TOK["brin"])
    check("N1 缺 flip: 行 ⇒ 拒绝", r.startswith("ERROR: 'object' requires a flip-condition line"),
          r[:140])
    check("N2 拒绝被审计计数（action=flip_condition_missing 恰 1 行）",
          flip_missing_count(db, tid) == 1, str(flip_missing_count(db, tid)))

    print("\n=== 正控制：vacuous flip 必绿（#2510 §5-2 treatment 逐字声明） ===")
    r = srv.set_verdict(thread_id=tid, verdict="object", author="brin",
                        note="flip: 当我改变看法时",
                        token=TOK["brin"])
    check("P1 vacuous flip 行被接受（机制强于声明即红，#2510 §5-2）",
          r.startswith("Verdict recorded"), r[:140])
    check("P2 vacuous 拒绝不重复计数（第一次 N1 已计 1 行，vacuous 接受不追加）",
          flip_missing_count(db, tid) == 1, str(flip_missing_count(db, tid)))

    print("\n=== 正控制：真条件 flip 亦绿 ===")
    r = srv.set_verdict(thread_id=tid, verdict="object", author="brin",
                        note="整体方向存疑\nflip: 若性能基准显示无回归，我改投同意",
                        token=TOK["brin"])
    check("P3 真条件 flip 被接受", r.startswith("Verdict recorded"), r[:140])

    print("\n=== pass 票不受影响 ===")
    r = srv.set_verdict(thread_id=tid, verdict="pass", author="brin",
                        note="changed my mind", token=TOK["brin"])
    check("PASS pass 票无需 flip 行", r.startswith("Verdict recorded"), r[:140])

    print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
    if FAIL:
        for name in FAIL:
            print("  FAIL:", name)
        sys.exit(1)


if __name__ == "__main__":
    main()
