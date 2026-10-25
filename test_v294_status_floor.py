#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.9.4 手动关帖补洞的回归（thread #76 事故 / 提交 04be587）。

背景（一手）：2026-10-24 thread #76 自动路在 15:27:29 正确拦下
`reason=discussion_floor: ['claude','pi','zcode']`；随后某个会话走**手动**
`set_status('resolved')`——当时手动路只校验"全量名册的票"、不看"每人是否留过话"，
直接放行；而 `set_status` 成功路径**一条审计都不写**，事后无法回答"谁关的"。

本文件把 04be587 的三条行为钉成断言（pi #777 §2 用隔离 fixture 人工验过同样的三条，
此处落成回归）：

  A. 三票齐但本修订内零评论 ⇒ 手动 `set_status('resolved')` 被拒，帖仍 open，
     拒绝文案点名缺评论的活跃成员。
  B. 每人各留一行后再手动关 ⇒ 成功，且写一条 `status_change` 审计（prev→new）。
  C. 手动关不与自动路双写：`auto_resolve` 审计行数为 0。

红绿（红先）：把 server.py 回滚到 551d41a（`git stash` 前的版本）后再跑——
A 会变红（手动关直接放行），B 会变红（没有 status_change 行）。

Run: .venv/bin/python test_v294_status_floor.py
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


def audits(db, tid, action):
    c = _ro(db)
    try:
        return [dict(r) for r in c.execute(
            "SELECT author, action, from_v, to_v, note FROM verdict_events"
            " WHERE thread_id=? AND action=? ORDER BY id", (tid, action)).fetchall()]
    finally:
        c.close()


def main():
    tmp = tempfile.mkdtemp(prefix="rb-v294-floor-")
    db = os.path.join(tmp, "f.db")
    srv = fx.stub_env(db)
    srv.init_db()

    members = ("anna", "brin", "cyr")
    with srv.db() as conn:
        srv._begin_write(conn)
        for n in members:
            srv._touch(conn, n)
        conn.commit()
    fx.prime_beats(db, list(members))
    ctx = fx.wi(*members)
    tok = {}
    for n in members:
        m = re.search(r"[0-9a-f]{32}", srv.claim_token(author=n))
        assert m, "token claim failed"
        tok[n] = m.group(0)

    # ---------- 建帖：creator=anna, quorum 含创建者（与 #76 真实形态一致） ----------
    out = srv.create_thread(title="floor-manual", author="anna",
                            quorum=["anna", "brin", "cyr"], context=ctx)
    assert out.startswith("Created thread"), out
    tid = int(re.search(r"#(\d+)", out).group(1))
    print(f"\n[setup] thread #{tid} quorum=[anna,brin,cyr] creator=anna")

    # 三票齐（无人留评论）。L1.5：创建者必须最后投。
    for n in ("brin", "cyr"):
        r = srv.set_verdict(thread_id=tid, verdict="pass", author=n, token=tok[n])
        assert r.startswith("Verdict"), (n, r)
    r = srv.set_verdict(thread_id=tid, verdict="pass", author="anna", token=tok["anna"])
    assert r.startswith("Verdict"), ("anna", r)
    check("precondition: 三票 pass 已落", len(audits(db, tid, "verdict_free")) +
          len(audits(db, tid, "verdict")) >= 2,
          "verdict audit rows missing")

    # ---------- A. 零评论 ⇒ 手动关被拒 ----------
    res = srv.set_status(thread_id=tid, status="resolved", author="anna")
    st = status_of(db, tid)
    check("A1: 零评论时手动 resolved 被拒绝", res.startswith("ERROR:"), res[:120])
    check("A2: 拒绝文案点名 'discussion floor not met'",
          "discussion floor not met" in res, res[:160])
    check("A3: 拒绝文案列出缺评论的成员",
          all(n in res for n in members), res[:160])
    check("A4: 帖仍为 open", st == "open", f"status={st}")
    check("A5: 被拒时不写 status_change 审计", len(audits(db, tid, "status_change")) == 0,
          str(audits(db, tid, "status_change")))

    # ---------- human_override 仍然可强制（有审计） ----------
    res_h = srv.set_status(thread_id=tid, status="resolved", author="anna",
                           human_override=True)
    ho = audits(db, tid, "human_override")
    check("A6: human_override=True 可强制关闭", res_h.startswith("Thread #"),
          res_h[:120])
    check("A7: 强制关闭留下 human_override 审计", len(ho) == 1,
          str([a["note"] for a in ho]))
    # 复位到 open 继续后面的用例
    before = len(audits(db, tid, "status_change"))
    res_back = srv.set_status(thread_id=tid, status="open", author="anna")
    sc_back = audits(db, tid, "status_change")
    check("A8: 真实转移 resolved→open 写 status_change（from/to 可读）",
          len(sc_back) == before + 1 and sc_back[-1]["from_v"] == "resolved"
          and sc_back[-1]["to_v"] == "open", str(sc_back[-1:]))
    # 空转（open→open）不是状态变更 ⇒ 不写审计（#779 §0-3 判：不该写）
    before2 = len(audits(db, tid, "status_change"))
    res_noop = srv.set_status(thread_id=tid, status="open", author="anna")
    check("A9: 空转 open→open 不写 status_change 审计",
          len(audits(db, tid, "status_change")) == before2,
          str(audits(db, tid, "status_change")[before2:]))
    check("A10: 空转返回体显式说 'already … no audit'",
          "already 'open'" in res_noop and "no audit" in res_noop, res_noop[:140])

    # ---------- B. 每人留一行 ⇒ 手动关成功 + 审计 ----------
    for n in members:
        r = srv.post_comment(thread_id=tid, author=n, body=f"{n} floor 一行")
        assert r.startswith("Posted"), (n, r)

    res2 = srv.set_status(thread_id=tid, status="resolved", author="anna")
    st2 = status_of(db, tid)
    sc = audits(db, tid, "status_change")
    check("B1: 地板满足后手动 resolved 成功", res2.startswith("Thread #"), res2[:120])
    check("B2: 帖变为 resolved", st2 == "resolved", f"status={st2}")
    check("B3: 写了 status_change 审计", len(sc) >= 1, str(sc))
    last = sc[-1] if sc else {}
    check("B4: 审计记了 prev→new 与作者",
          last.get("from_v") == "open" and last.get("to_v") == "resolved",
          str(last))
    check("B5: 审计 note 标明 manual 与 human_override 状态",
          "manual set_status" in (last.get("note") or "")
          and "human_override=False" in (last.get("note") or ""),
          str(last.get("note")))

    # ---------- C. 手动关不与自动路双写 ----------
    auto = audits(db, tid, "auto_resolve")
    check("C1: 手动关不产生 auto_resolve 审计（无双写）", len(auto) == 0,
          str([a["note"] for a in auto]))

    print(f"\n结果: PASS={len(PASS)} FAIL={len(FAIL)}")
    if FAIL:
        print("失败项:", FAIL)
        return 1
    print("✅ v2.9.4 status-floor regressions PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
