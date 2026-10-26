#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.11.0 回归：地板×预算闭环死结的修法（threads #80/#81 定案，F2 主路 + F4 兜缝）。

事故（#80 一手实证）：rev2 三票 pass 全落账，但 dsh/pi 评论预算 20/20 耗尽、发不出
地板要求的那条评论——义务被实现成计费发言，物理不可能履行；auto/manual 两条
resolved 路都被拦，最后靠用户 human_override 收口。

本文件把 #81 定案钉成断言：
  S1  F2 正路：预算耗尽 ∧ 本修订非空票面 note ⇒ 地板满足、auto-resolve 收口、
      floor_satisfied_via_note 审计带 via/used/cap/len 快照；my_comment_due 不再列
      该成员（提醒与拒收不再并存，dsh #934 §5 验收①②的读面半边）。
  S2  F2 取数只认事件表（dsh #934 §2.1 的复发路径）：object 带非空 note → 空 note
      翻 pass（verdicts.note 被覆盖为 NULL）⇒ 地板仍满足——票面行不可裸读。
  S2b 修订边界（GPT PR#7 P2/P3 状态转换组）：rev1 的非空 note **不满足** rev2 地板
      （F2 取数按当前 revision 圈定）；三票 pass 仍被拦，F4 兜住"rev2 投了空 note 票"
      这条缝，发完收口。
  S3  F4 正路：预算耗尽 ∧ 空 note ∧ 其余活跃票全 pass ⇒ post_comment 放行一条
      免费地板句：行级 floor_exempt=1、_author_usage 不计入（used<=cap 不变量——
      超过 cap 的边界按不变量不可达，S3e 即其断言）、原始行数口径仍计
      （THREAD_CAP/日额的计数面）、审计 via=floor_exempt+快照、发出后谓词自限。
  S3j 自身票检查（GPT PR#7 外审 P2 收窄）：我未投票时"地板是我唯一缺口"不成立，
      F4 不开（正确出口=免费首判票）；投完 pass 后 F4 才开。quorum 不含创建者——
      否则 L1.5（创建者最后投票）挡住"其余全 pass 而我未投"的形态。
  S4  F4 反路：其余成员未全 pass ⇒ 拒绝，文案指明出口何时开启（假建议分支化）。
  S5  预算拒绝文案分支：地板已满足者收到"无需再发"，不再收到
      "Conclude via set_verdict / set_status" 假建议。
  S6  set_quorum 守卫：本修订已有票行 ⇒ 无 override 拒绝，override 放行且审计记
      「评审人集合被改写」。
  S7  override 关帖审计补当时 floor_missing 名单 + revision（dsh #934 §5）。
  S8  schema：新库原生带 floor_exempt 列；旧库（无该列）经 _floor_exempt_shape 收敛。

Run: python test_v211_floor_budget.py
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


def _ro(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def audits(db, tid, action):
    c = _ro(db)
    try:
        return [dict(r) for r in c.execute(
            "SELECT author, action, note, revision FROM verdict_events"
            " WHERE thread_id=? AND action=? ORDER BY id", (tid, action)).fetchall()]
    finally:
        c.close()


def status_of(db, tid):
    c = _ro(db)
    try:
        return c.execute("SELECT status FROM threads WHERE id=?", (tid,)).fetchone()[0]
    finally:
        c.close()


def raw_comment_rows(db, tid, author):
    c = _ro(db)
    try:
        return [dict(r) for r in c.execute(
            "SELECT id, floor_exempt FROM comments WHERE thread_id=? AND author=?"
            " ORDER BY id", (tid, author)).fetchall()]
    finally:
        c.close()


TOK: dict[str, str] = {}   # 同一测试库跨场景复用（24h 重签锁下二次 claim 只返回提示）


def make_thread(srv, db, budget, note=""):
    """公共脚手架：anna 建帖（quorum=anna/brin/cyr，budget 给定），token/心跳就绪。"""
    members = ("anna", "brin", "cyr")
    with srv.db() as conn:
        srv._begin_write(conn)
        for n in members:
            srv._touch(conn, n)
        conn.commit()
    fx.prime_beats(db, list(members))
    out = srv.create_thread(title="v211-floor-budget", author="anna",
                            quorum=["anna", "brin", "cyr"],
                            context=fx.wi(*members), per_author_budget=budget)
    assert out.startswith("Created thread"), out
    tid = int(re.search(r"#(\d+)", out).group(1))
    for n in members:
        if n not in TOK:
            m = re.search(r"[0-9a-f]{32}", srv.claim_token(author=n))
            assert m, f"token claim failed for {n}"
            TOK[n] = m.group(0)
            r = srv.ack_token(author=n, token=TOK[n])
            assert r.startswith("Token acknowledged"), r
    return tid, {n: TOK[n] for n in members}


def main():
    tmp = tempfile.mkdtemp(prefix="rb-v211-floor-")
    db = os.path.join(tmp, "f.db")
    srv = fx.stub_env(db)
    srv.init_db()

    # ================= S1: F2 正路（#80 形态复刻：耗尽+长 note ⇒ 自动收口） ========
    print("\n=== S1 F2 正路 ===")
    tid, tok = make_thread(srv, db, budget=2)
    for n in ("brin", "cyr"):                      # 耗尽预算（2 条评论）
        for i in range(2):
            r = srv.post_comment(thread_id=tid, author=n, body=f"{n} line {i}")
            assert r.startswith("Posted"), r
    # bump 把水位抬到旧评论之上 ⇒ rev2 内 brin/cyr 无评论、预算仍计旧评论
    # （#80 的真实形态：耗尽预算的评论在水位之下，发不出新的那条）
    r = srv.bump_revision(thread_id=tid, author="anna", token=tok["anna"])
    assert r.startswith("Revision bumped"), r
    r = srv.post_comment(thread_id=tid, author="anna", body="creator rev2 line")  # anna 地板
    assert r.startswith("Posted"), r
    srv.list_comments_since(author="anna", since="now")     # 创建者追平投递门
    # 投票（L1.5：创建者最后）；brin/cyr 票面带非空 note——正是 #80 里 dsh/pi 的形态
    for n, note in (("brin", "pass：三条绑定已核对"), ("cyr", "pass：无保留")):
        r = srv.set_verdict(thread_id=tid, verdict="pass", author=n, token=tok[n], note=note)
        assert r.startswith("Verdict"), (n, r)
    # 验收①：F2 已满足 ⇒ my_comment_due 不再列 brin（提醒与拒收不并存）
    with srv._connect() as conn:
        due = srv._comment_due_face(conn, "brin")
    check("S1a: 预算耗尽∧有非空 note ⇒ my_comment_due 不列该成员",
          all(e["thread_id"] != tid for e in due), str(due))
    r = srv.set_verdict(thread_id=tid, verdict="pass", author="anna", token=tok["anna"])
    assert r.startswith("Verdict"), r
    check("S1b: 三票 pass ⇒ auto-resolve 收口（#80 死结不复现）",
          status_of(db, tid) == "resolved", status_of(db, tid))
    via = audits(db, tid, "floor_satisfied_via_note")
    check("S1c: floor_satisfied_via_note 审计在（via=verdict_note）",
          len(via) == 1 and "via=verdict_note" in (via[0]["note"] or ""),
          str(via))
    check("S1d: 审计快照带 used/cap 与 len(note)",
          "used=2/cap=2" in (via[0]["note"] or "") and "len(note)=" in (via[0]["note"] or ""),
          str(via))

    # ============ S2: F2 取数只认事件表（object 带 note → 空 note 翻 pass） =======
    print("\n=== S2 F2 事件表取数（dsh #934 §2.1 复发路径）===")
    tid2, tok2 = make_thread(srv, db, budget=2)
    for i in range(2):                              # brin 耗尽（2 条评论）
        r = srv.post_comment(thread_id=tid2, author="brin", body=f"brin line {i}")
        assert r.startswith("Posted"), r
    # bump：清票 + 水位抬到已发评论之上 ⇒ rev2 内 brin 无评论、预算仍计旧评论
    r = srv.bump_revision(thread_id=tid2, author="anna", token=tok2["anna"])
    assert r.startswith("Revision bumped"), r
    for n in ("anna", "cyr"):                       # rev2：其余两人留评论（各自地板）
        r = srv.post_comment(thread_id=tid2, author=n, body=f"{n} rev2 line")
        assert r.startswith("Posted"), r
    srv.list_comments_since(author="anna", since="now")
    # brin：先 object（带 note，首判免费）→ 空 note 翻 pass（收敛翻转免费）
    r = srv.set_verdict(thread_id=tid2, verdict="object", author="brin",
                        token=tok2["brin"], note="object：等预算修好才翻")
    assert r.startswith("Verdict"), r
    r = srv.set_verdict(thread_id=tid2, verdict="pass", author="brin", token=tok2["brin"])
    assert r.startswith("Verdict"), r
    c = _ro(db)
    vnote = c.execute("SELECT note FROM verdicts WHERE thread_id=? AND author='brin'",
                      (tid2,)).fetchone()[0]
    c.close()
    check("S2a: 前置——票面行 note 已被空 note 翻票覆盖为 NULL", vnote is None, repr(vnote))
    for n in ("cyr", "anna"):
        r = srv.set_verdict(thread_id=tid2, verdict="pass", author=n, token=tok2[n])
        assert r.startswith("Verdict"), (n, r)
    check("S2b: 事件表里有旧 note ⇒ 地板仍满足、帖收口（异议刚解决那一刻不失效）",
          status_of(db, tid2) == "resolved", status_of(db, tid2))
    via2 = audits(db, tid2, "floor_satisfied_via_note")
    check("S2c: via 名单含 brin 且 len(note)>0（快照取自事件表）",
          len(via2) == 1 and "brin:used=2/cap=2" in (via2[0]["note"] or "")
          and "len(note)=" in (via2[0]["note"] or ""), str(via2))

    # ============ S2b: 修订边界（GPT PR#7 P2/P3 状态转换组） ======================
    print("\n=== S2b 旧修订 note 不带入新修订 ===")
    tid2b, tok2b = make_thread(srv, db, budget=2)
    for i in range(2):
        r = srv.post_comment(thread_id=tid2b, author="brin", body=f"brin {i}")
        assert r.startswith("Posted"), r
    # rev1：brin 投 pass 带非空 note（F2 源）
    r = srv.set_verdict(thread_id=tid2b, verdict="pass", author="brin",
                        token=tok2b["brin"], note="rev1 结论")
    assert r.startswith("Verdict"), r
    r = srv.bump_revision(thread_id=tid2b, author="anna", token=tok2b["anna"])
    assert r.startswith("Revision bumped"), r
    for n in ("anna", "cyr"):
        r = srv.post_comment(thread_id=tid2b, author=n, body=f"{n} rev2 line")
        assert r.startswith("Posted"), r
    srv.list_comments_since(author="anna", since="now")
    # rev2：brin 投空 note 首判（免费）——旧 note 不得带入
    for n in ("brin", "cyr"):
        r = srv.set_verdict(thread_id=tid2b, verdict="pass", author=n, token=tok2b[n])
        assert r.startswith("Verdict"), (n, r)
    r = srv.set_verdict(thread_id=tid2b, verdict="pass", author="anna", token=tok2b["anna"])
    assert r.startswith("Verdict"), r
    check("S2b-1: 旧修订 note 不满足新修订地板——三票 pass 仍被拦",
          status_of(db, tid2b) == "open", status_of(db, tid2b))
    blocked = audits(db, tid2b, "auto_resolve_blocked")
    check("S2b-2: 拦截审计点名 brin（reason=discussion_floor）",
          any("reason=discussion_floor" in (b["note"] or "")
              and "brin" in (b["note"] or "") for b in blocked),
          str(blocked[-1:]))
    r = srv.post_comment(thread_id=tid2b, author="brin", body="地板句：rev2 维持 pass。")
    check("S2b-3: F4 兜住这条缝（rev2 投了空 note 票）",
          r.startswith("Posted") and "free floor comment" in r, r)
    srv.list_comments_since(author="anna", since="now")
    check("S2b-4: 收口", status_of(db, tid2b) == "resolved", status_of(db, tid2b))

    # ===================== S3: F4 正路（空 note 的兜缝出口） ======================
    print("\n=== S3 F4 正路 ===")
    tid3, tok3 = make_thread(srv, db, budget=2)
    for i in range(2):                              # brin 耗尽
        r = srv.post_comment(thread_id=tid3, author="brin", body=f"brin line {i}")
        assert r.startswith("Posted"), r
    r = srv.bump_revision(thread_id=tid3, author="anna", token=tok3["anna"])
    assert r.startswith("Revision bumped"), r
    for n in ("anna", "cyr"):
        r = srv.post_comment(thread_id=tid3, author=n, body=f"{n} rev2 line")
        assert r.startswith("Posted"), r
    srv.list_comments_since(author="anna", since="now")
    # brin 票面空 note（首判免费）⇒ F2 救不了；其余两人 pass ⇒ F4 出口开
    for n in ("brin", "cyr"):
        r = srv.set_verdict(thread_id=tid3, verdict="pass", author=n, token=tok3[n])
        assert r.startswith("Verdict"), (n, r)
    r = srv.set_verdict(thread_id=tid3, verdict="pass", author="anna", token=tok3["anna"])
    assert r.startswith("Verdict"), r
    check("S3a: 前置——三票 pass 但地板欠 brin，帖仍 open（死结形态）",
          status_of(db, tid3) == "open", status_of(db, tid3))
    res = srv.set_status(thread_id=tid3, status="resolved", author="anna")
    check("S3b: 手动关被拦且文案保留 'discussion floor not met' 并指向新出口",
          res.startswith("ERROR:") and "discussion floor not met" in res
          and "free floor comment" in res, res[:200])
    r = srv.post_comment(thread_id=tid3, author="brin", body="地板句：维持 pass。")
    check("S3c: F4 放行免费地板句（返回带 free 标）",
          r.startswith("Posted") and "free floor comment" in r, r)
    rows = raw_comment_rows(db, tid3, "brin")
    check("S3d: 该行 floor_exempt=1（服务端打标）",
          rows[-1]["floor_exempt"] == 1, str(rows))
    with srv.db() as conn:
        used = srv._author_usage(conn, tid3, "brin")
    check("S3e: used<=cap 不变量不破（豁免行不计预算：仍 2/2）", used == 2, str(used))
    check("S3f: 原始行数口径仍计该行（THREAD_CAP/日额的计数面）",
          len(rows) == 3, str(rows))
    ex = audits(db, tid3, "floor_exempt")
    check("S3g: 审计 via=floor_exempt + used/cap 快照",
          len(ex) == 1 and "via=floor_exempt" in (ex[0]["note"] or "")
          and "used=2/cap=2" in (ex[0]["note"] or ""), str(ex))
    srv.list_comments_since(author="anna", since="now")   # 创建者追平投递门后复算
    check("S3h: 地板句落地（+创建者追平）⇒ 帖收口", status_of(db, tid3) == "resolved",
          status_of(db, tid3))
    r2 = srv.post_comment(thread_id=tid3, author="brin", body="再来一条")
    check("S3i: 谓词自限——第二条不再免（预算分支拒绝且非地板欠账文案）",
          r2.startswith("ERROR:") and "already satisfied" in r2, r2[:200])

    # ===== S3j: 自身票检查（GPT PR#7 外审 P2 收窄）——未投票 ⇒ F4 不开 =========
    print("\n=== S3j 未投票不开 F4（唯一缺口语义）===")
    # quorum 不含创建者：否则 L1.5（创建者最后投票）会挡住"其余全 pass 而我未投"
    # 的形态，测不到本判据。#80/#81 的真实板形正是创建者在 quorum 外。
    members_j = ("brin", "cyr", "dana")
    with srv.db() as conn:
        srv._begin_write(conn)
        for n in members_j + ("anna",):
            srv._touch(conn, n)
        conn.commit()
    fx.prime_beats(db, list(members_j) + ["anna"])
    out = srv.create_thread(title="v211-selfpass", author="anna",
                            quorum=list(members_j),
                            context=fx.wi("anna", *members_j),   # 创建者并入必检名单
                            per_author_budget=2)
    assert out.startswith("Created thread"), out
    tidj = int(re.search(r"#(\d+)", out).group(1))
    if "dana" not in TOK:
        m = re.search(r"[0-9a-f]{32}", srv.claim_token(author="dana"))
        assert m, "token claim failed for dana"
        TOK["dana"] = m.group(0)
        r = srv.ack_token(author="dana", token=TOK["dana"])
        assert r.startswith("Token acknowledged"), r
    for i in range(2):
        r = srv.post_comment(thread_id=tidj, author="brin", body=f"brin {i}")
        assert r.startswith("Posted"), r
    r = srv.bump_revision(thread_id=tidj, author="anna", token=TOK["anna"])
    assert r.startswith("Revision bumped"), r
    for n in ("cyr", "dana"):
        r = srv.post_comment(thread_id=tidj, author=n, body=f"{n} rev2 line")
        assert r.startswith("Posted"), r
    srv.list_comments_since(author="anna", since="now")
    for n in ("cyr", "dana"):                       # 其余两人全 pass（带 note）
        r = srv.set_verdict(thread_id=tidj, verdict="pass", author=n,
                            token=TOK[n], note=f"{n} pass")
        assert r.startswith("Verdict"), (n, r)
    # brin 未投票（预算耗尽、silent、无 note）——旧判据会放行，收窄后必须拒绝
    r = srv.post_comment(thread_id=tidj, author="brin", body="未投票想蹭免费地板句")
    check("S3j-1: 我未投票 ⇒ F4 不开（文案要求先投 pass）",
          r.startswith("ERROR:") and "your own verdict is pass" in r, r[:250])
    r = srv.set_verdict(thread_id=tidj, verdict="pass", author="brin", token=TOK["brin"])
    assert r.startswith("Verdict"), r
    r = srv.post_comment(thread_id=tidj, author="brin", body="地板句：维持 pass。")
    check("S3j-2: 投完 pass 后 F4 开、免费地板句放行",
          r.startswith("Posted") and "free floor comment" in r, r)
    srv.list_comments_since(author="anna", since="now")
    check("S3j-3: 地板句+创建者追平 ⇒ 收口",
          status_of(db, tidj) == "resolved", status_of(db, tidj))

    # ===================== S4: F4 反路（其余未全 pass ⇒ 出口未开） ================
    print("\n=== S4 F4 反路 ===")
    tid4, tok4 = make_thread(srv, db, budget=2)
    for i in range(2):
        r = srv.post_comment(thread_id=tid4, author="brin", body=f"brin line {i}")
        assert r.startswith("Posted"), r
    r = srv.bump_revision(thread_id=tid4, author="anna", token=tok4["anna"])
    assert r.startswith("Revision bumped"), r
    for n in ("anna", "cyr"):
        r = srv.post_comment(thread_id=tid4, author=n, body=f"{n} rev2 line")
        assert r.startswith("Posted"), r
    # anna 第二条：耗尽她自己的预算（S5 要用"地板已满足且预算耗尽"的形态）
    r = srv.post_comment(thread_id=tid4, author="anna", body="anna rev2 line 2")
    assert r.startswith("Posted"), r
    srv.list_comments_since(author="anna", since="now")
    r = srv.set_verdict(thread_id=tid4, verdict="pass", author="brin", token=tok4["brin"])
    assert r.startswith("Verdict"), r
    r = srv.set_verdict(thread_id=tid4, verdict="object", author="cyr",
                        token=tok4["cyr"], note="object：还有一问未答")
    assert r.startswith("Verdict"), r
    r = srv.post_comment(thread_id=tid4, author="brin", body="想蹭免费地板句")
    check("S4a: 其余未全 pass ⇒ F4 拒绝，文案指明出口何时开启",
          r.startswith("ERROR:") and "every OTHER active member" in r
          and "free floor-comment exit" in r, r[:250])

    # ============== S5: 预算拒绝文案不再给假建议（Conclude via...已分支化） =======
    print("\n=== S5 假建议分支化 ===")
    r = srv.post_comment(thread_id=tid4, author="anna", body="anna 也想说话")
    # anna 已有 rev2 评论且预算 2/2 耗尽 ⇒ 地板已满足分支
    check("S5a: 地板已满足者收到『无需再发』分支",
          r.startswith("ERROR:") and "already satisfied" in r, r[:250])
    check("S5b: 旧假建议文案已退役",
          "Conclude via set_verdict / set_status" not in r, r[:250])

    # ===================== S6: set_quorum 守卫（票行在 ⇒ override） ===============
    print("\n=== S6 set_quorum 守卫 ===")
    r = srv.set_quorum(thread_id=tid4, author="anna", token=tok4["anna"],
                       add=["dana"], remove=["brin"],
                       wake_infra=fx.wi("anna", "dana"))
    check("S6a: 本修订已有票行 ⇒ 无 override 拒绝",
          r.startswith("ERROR:") and "human_override" in r, r[:200])
    r = srv.set_quorum(thread_id=tid4, author="anna", token=tok4["anna"],
                       add=["dana"], remove=["brin"], wake_infra=fx.wi("anna", "dana"),
                       human_override=True)
    check("S6b: override 放行", r.startswith("Quorum updated"), r[:200])
    sq = audits(db, tid4, "set_quorum")
    check("S6c: 审计记「评审人集合被改写」",
          "quorum rewritten with standing verdicts" in (sq[-1]["note"] or ""),
          str(sq[-1:]))

    # ===================== S7: override 关帖审计补 floor_missing 名单 =============
    print("\n=== S7 override 审计补名单 ===")
    r = srv.set_status(thread_id=tid4, status="resolved", author="anna", human_override=True)
    check("S7a: override 可关", r.startswith("Thread #"), r[:120])
    ho = audits(db, tid4, "human_override")
    check("S7b: human_override 审计带 floor_missing 名单与 revision",
          len(ho) >= 1 and "floor_missing=" in (ho[-1]["note"] or "")
          and "revision=" in (ho[-1]["note"] or ""), str(ho[-1:]))

    # ===================== S8: schema（新库原生 / 旧库收敛） ======================
    print("\n=== S8 schema ===")
    c = _ro(db)
    cols = [r[1] for r in c.execute("PRAGMA table_info(comments)")]
    c.close()
    check("S8a: 新库 comments 带 floor_exempt 列", "floor_exempt" in cols, str(cols))
    # 旧库收敛按单元测（stub_env 对同一进程返回已绑定首库的模块，无法在本进程
    # 对第二个库走完整 init_db——fixup 本身是纯 conn 函数，直接验）：
    legacy = os.path.join(tmp, "legacy.db")
    lc = sqlite3.connect(legacy)
    lc.executescript(
        "CREATE TABLE comments (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " thread_id INTEGER NOT NULL, parent_id INTEGER, author TEXT NOT NULL,"
        " body TEXT NOT NULL, file TEXT, line INTEGER, severity TEXT,"
        " status TEXT NOT NULL DEFAULT 'open',"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')));"
    )
    lc.commit()
    srv._floor_exempt_shape(lc)          # 首次：补列
    lc.commit()
    lcols = [r[1] for r in lc.execute("PRAGMA table_info(comments)")]
    check("S8b: 旧库（无该列）经 _floor_exempt_shape 收敛出 floor_exempt",
          "floor_exempt" in lcols, str(lcols))
    srv._floor_exempt_shape(lc)          # 幂等：二次不炸（PRAGMA 守卫）
    lc.commit()
    lc.close()
    check("S8c: fixup 幂等（二次调用不报错）", True)

    print(f"\n{'='*60}\nv2.11.0 floor×budget: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAIL:", f)
        sys.exit(1)


if __name__ == "__main__":
    main()
