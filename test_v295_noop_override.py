#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.9.5 手动 set_status 的两条审计口径（#64 / dsh #784 §2a-b-c，pi #786 判）。

三件事一体，因为它们是同一条规则的两个面：

  1. **空转短路前移**：`prev == status` 在**任何门与 override 分支之前**返回
     "already … no audit row"。此前短路点在票门/终态门/地板**之后**，于是
     resolved→resolved 的重试先撞地板（dsh #784 D1b：帖明明已关，返回的却是
     "discussion floor not met"），wontfix→wontfix 先撞终态门（D2b）。
  2. **override 也短路**：此前 override 分支没有任何 prev 比较，空转每次调用都写
     一行 human_override —— 与 #577 的 open→open 同型，只是名字不同（D4d）。
  3. **override 真转移也要写 status_change**（判「写」）：审计存在的唯一理由是回答
     "谁关的"；只写 human_override 时，单查 status_change 答不出。活库唯一一处
     override 实例（#20 / ev#213 zcode wontfix）至今 status_change=0 行 —— 不是"将来
     会混进噪音"，是现在就答不出。

不回溯 #20：prev 无一手记录，回填即造假；改为在协议文本里写明 v2.9.5 前 override
路无 status_change（一处已知空洞，可查）。

红绿（红先）：把 server.py 换回 87967fd 那份再跑 —— N1/N2/N3/N4/N6 红，其余绿。

Run: .venv/bin/python test_v295_noop_override.py
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
    tmp = tempfile.mkdtemp(prefix="rb-v295-noop-")
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

    out = srv.create_thread(title="noop-override", author="anna",
                            quorum=["anna", "brin", "cyr"], context=ctx)
    assert out.startswith("Created thread"), out
    tid = int(re.search(r"#(\d+)", out).group(1))
    print(f"\n[setup] thread #{tid} quorum=[anna,brin,cyr] creator=anna")

    for n in ("brin", "cyr"):
        assert srv.set_verdict(thread_id=tid, verdict="pass", author=n,
                               token=tok[n]).startswith("Verdict")
    assert srv.set_verdict(thread_id=tid, verdict="pass", author="anna",
                           token=tok["anna"]).startswith("Verdict")

    # ---------- N1/N2: 空转短路在任何门之前 ----------
    # 三票齐、本修订零评论 ⇒ 地板未满足。resolved→resolved 应回 "already"，不是地板错误。
    sc0 = len(audits(db, tid, "status_change"))
    res = srv.set_status(thread_id=tid, status="open", author="anna")
    check("N1a: 空转 open→open（地板未满足）返回 already",
          "already 'open'" in res and "discussion floor" not in res, res[:140])
    check("N1b: 空转不写 status_change", len(audits(db, tid, "status_change")) == sc0,
          str(audits(db, tid, "status_change")))

    # ---------- N3: override 空转不写任何审计 ----------
    ho0 = len(audits(db, tid, "human_override"))
    res = srv.set_status(thread_id=tid, status="open", author="anna", human_override=True)
    check("N3a: override 空转返回 already", "already 'open'" in res, res[:140])
    check("N3b: override 空转不写 human_override 行",
          len(audits(db, tid, "human_override")) == ho0,
          str([a["note"] for a in audits(db, tid, "human_override")]))
    check("N3c: override 空转不写 status_change",
          len(audits(db, tid, "status_change")) == sc0,
          str(audits(db, tid, "status_change")))

    # ---------- N2: resolved→resolved 空转（先真关一次） ----------
    r = srv.set_status(thread_id=tid, status="resolved", author="anna", human_override=True)
    check("N4a: override 真转移成功", r.startswith("Thread #"), r[:140])
    check("N4b: override 真转移写 human_override 一行",
          len(audits(db, tid, "human_override")) == ho0 + 1,
          str([a["note"] for a in audits(db, tid, "human_override")]))
    sc = audits(db, tid, "status_change")
    check("N4c: override 真转移**也**写 status_change（§2c 判「写」）",
          len(sc) == sc0 + 1 and sc[-1]["from_v"] == "open" and sc[-1]["to_v"] == "resolved",
          str(sc[-1:]))
    check("N4d: 该行 note 标明 human_override=True",
          bool(sc) and "human_override=True" in (sc[-1]["note"] or ""), str(sc[-1:]))
    check("N4e: 帖确实 resolved", status_of(db, tid) == "resolved", status_of(db, tid))

    sc1 = len(audits(db, tid, "status_change"))
    res = srv.set_status(thread_id=tid, status="resolved", author="anna")
    check("N2a: resolved→resolved 空转返回 already（不再先撞地板）",
          "already 'resolved'" in res and "discussion floor" not in res, res[:140])
    check("N2b: resolved→resolved 空转不写 status_change",
          len(audits(db, tid, "status_change")) == sc1,
          str(audits(db, tid, "status_change")[sc1:]))

    # ---------- N5: wontfix 空转不撞终态门；override 进 wontfix 也留 status_change ----------
    r = srv.set_status(thread_id=tid, status="open", author="anna", human_override=True)
    check("N5a: override resolved→open 成功", r.startswith("Thread #"), r[:140])
    r = srv.set_status(thread_id=tid, status="wontfix", author="anna", human_override=True)
    sc = audits(db, tid, "status_change")
    check("N5b: override 进 wontfix 留 status_change",
          r.startswith("Thread #") and bool(sc) and sc[-1]["to_v"] == "wontfix"
          and sc[-1]["from_v"] == "open", str(sc[-1:]))
    res = srv.set_status(thread_id=tid, status="wontfix", author="anna")
    check("N5c: wontfix→wontfix 空转返回 already（不再先撞终态门）",
          "already 'wontfix'" in res and "terminal state" not in res, res[:140])

    # ---------- N6: 普通路真实转移仍恰写一行（无双写），note 口径不变 ----------
    r = srv.set_status(thread_id=tid, status="open", author="anna", human_override=True)
    assert r.startswith("Thread #"), r
    for n in members:  # 满足地板，走普通路
        srv.post_comment(thread_id=tid, author=n, body=f"{n} floor 一行")
    before = len(audits(db, tid, "status_change"))
    r = srv.set_status(thread_id=tid, status="resolved", author="anna")
    sc = audits(db, tid, "status_change")
    check("N6a: 普通路真关成功", r.startswith("Thread #"), r[:140])
    check("N6b: 普通路恰写一行 status_change",
          len(sc) == before + 1, str(sc[before:]))
    check("N6c: note 口径仍是 human_override=False",
          "human_override=False" in (sc[-1]["note"] or ""), str(sc[-1]))
    check("N6d: 普通路不写 human_override 行",
          not [a for a in audits(db, tid, "human_override") if a["to_v"] == "resolved"
               and "human_override=False" in (a["note"] or "")],
          str(audits(db, tid, "human_override")[-1:]))

    print(f"\n结果: PASS={len(PASS)} FAIL={len(FAIL)}")
    if FAIL:
        print("失败项:", FAIL)
        return 1
    print("✅ v2.9.5 noop/override audit regressions PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
