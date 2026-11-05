#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.12.0 票B 第二批加固回归（承 `test_v212_meta_hardening.py`）。

本文件只钉第二批才有的两件事：
  T-deep-3 — 超深嵌套行在**热路径/读面**上不抛、字节原样（启动站已在
             第一批加固笔内钉住；`_meta_obj` 把它映射为 `invalid_json`，
             故 F6 门/读面走与 T3 完全相同的 fail-close 分支，不新增态）。
  D1a'/D1g — `_ring_append` 的**返回值**（坏行 `return False`）也要断言，不只
             "不抛"（opencode #1458 §4 留的那半格）；非 UTF-8 BLOB 行在热路径上
             同样不抛/不改字节（dsh #1457 §三：base 即绿，改指纹函数时最易弄红）。
  D4 — 事务层：坏行（depth=20000）走完整 `list_comments_since`，不抛且游标前进
             （`_beat` 抛 ⇒ 同一 with db() 回滚游标 ⇒ 永久卡在同一批未读，dsh #1461 §二）。
  note 原文 — `_beat` 删除 `:512` 重读后，`hb_read_migrate` 的 note 必须与
             重读版**逐字节相同**：`_ring_append(meta=...)` 就地改调用方
             dict（`meta[key] = ring`），所以下面的期望值就是 45f6ab6 实跑
             输出（`kept_pre_epoch=2 dropped_post_epoch=1 ring=3`）——不存在
             dsh #1437 §二/ claude #1441 ② 担心的 −1 口径变更（pi #1449 实跑）。

Run: python3 test_v212_meta_hardening_b2.py
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_fixture as fx

PASS, FAIL = [], []
DB = None
SRV = None
DEEP = "[" * 20000 + "]" * 20000
BLOB_BAD = b"\xff\xfe\x00bad"   # non-UTF-8 bytes: RecursionError's neighbour族 (dsh #1457 §三)


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  \u2705 " if cond else "  \u274c ") + name + (f" \u2014 {detail}" if detail and not cond else ""))


def conn():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def seed(author, raw, meta_row=True):
    c = conn()
    try:
        c.execute("DELETE FROM participants WHERE author=?", (author,))
        c.execute("DELETE FROM verdict_events WHERE author=?", (author,))
        c.execute("INSERT INTO participants(author, first_seen, last_seen, last_read, meta)"
                  " VALUES(?,?,?,NULL,?)", (author, "2026-01-01 00:00:00",
                                            "2026-01-01 00:00:00", raw))
        c.commit()
    finally:
        c.close()


def raw_meta(author):
    c = conn()
    try:
        return c.execute("SELECT meta FROM participants WHERE author=?", (author,)).fetchone()[0]
    finally:
        c.close()


def note_of(author, action):
    c = conn()
    try:
        return [r["note"] for r in c.execute(
            "SELECT note FROM verdict_events WHERE author=? AND action=? ORDER BY id",
            (author, action))]
    finally:
        c.close()


def main():
    global DB, SRV
    fd, DB = tempfile.mkstemp(prefix="t12-meta-harden-b2-", suffix=".db")
    os.close(fd)
    os.unlink(DB)
    SRV = fx.stub_env(DB)
    SRV.init_db()

    print("\nD1 超深嵌套（depth=20000）在热路径/读面上不抛、字节原样")
    seed("deep", DEEP)
    before = raw_meta("deep")
    calls = []
    c = conn()
    try:
        calls.append(("_ring_append", lambda: SRV._ring_append(c, "deep", "hb")))
        calls.append(("_beat", lambda: SRV._beat(c, "deep", delivered=1)))
        calls.append(("_probe", lambda: SRV._probe(c, "deep")))
        calls.append(("_get_meta", lambda: SRV._get_meta(c, "deep")))
        calls.append(("_token_ok", lambda: SRV._token_ok(c, "deep", "deadbeef")))
        calls.append(("_token_invalid_tids", lambda: SRV._token_invalid_tids(c, "deep")))
        calls.append(("_awaiting_split", lambda: SRV._awaiting_split(c, "deep")))
        calls.append(("_profile_numbers", lambda: SRV._profile_numbers(c, "deep")))
        rets = {}
        for name, fn in calls:
            try:
                rets[name] = fn(); check(f"D1a {name} no raise", True)
            except BaseException as e:  # noqa: BLE001
                check(f"D1a {name} no raise", False, type(e).__name__)
        check("D1a' _ring_append returns False on deep row (self-parse branch early exit)",
              rets.get("_ring_append") is False, rets.get("_ring_append"))
        check("D1b bytes untouched by hot paths", raw_meta("deep") == before)
        check("D1c no meta_unparsable audit on read paths (C3)",
              note_of("deep", "meta_unparsable") == [], note_of("deep", "meta_unparsable"))
    finally:
        c.close()
    try:
        SRV.list_participants(); ok = True
    except BaseException as e:  # noqa: BLE001
        ok = type(e).__name__
    check("D1d list_participants no raise", ok is True, ok)
    try:
        SRV._board_html(); ok = True
    except BaseException as e:  # noqa: BLE001
        ok = type(e).__name__
    check("D1e /board render no raise", ok is True, ok)
    check("D1f bytes untouched after read faces", raw_meta("deep") == before)

    print("\nD1g 非 UTF-8 BLOB 热路径（dsh #1457 §三：base 即绿，改指纹函数最易弄红）")
    seed("blob-bad", BLOB_BAD)
    bbefore = raw_meta("blob-bad")
    c = conn()
    try:
        try:
            rb = SRV._ring_append(c, "blob-bad", "hb")
            check("D1g-1 _ring_append no raise", True)
        except BaseException as e:  # noqa: BLE001
            rb = type(e).__name__; check("D1g-1 _ring_append no raise", False, rb)
        check("D1g-2 _ring_append returns False on bad-BLOB row", rb is False, rb)
        for name, fn in (("_beat", lambda: SRV._beat(c, "blob-bad", delivered=1)),
                         ("_probe", lambda: SRV._probe(c, "blob-bad")),
                         ("_get_meta", lambda: SRV._get_meta(c, "blob-bad"))):
            try:
                fn(); check(f"D1g-3 {name} no raise on bad BLOB", True)
            except BaseException as e:  # noqa: BLE001
                check(f"D1g-3 {name} no raise on bad BLOB", False, type(e).__name__)
        check("D1g-4 bad-BLOB bytes untouched", raw_meta("blob-bad") == bbefore)
        check("D1g-5 no meta_unparsable audit on read paths (C3)",
              note_of("blob-bad", "meta_unparsable") == [], note_of("blob-bad", "meta_unparsable"))
    finally:
        c.close()

    print("\nD2 hb_read_migrate note 原文 = 重读版逐字节（无 −1 口径变更）")
    meta = {"token_hash": "x", "f6_read_epoch": 1750000000,
            "hb": ["2020-01-01 00:00:00", "2020-01-01 00:10:00"]}
    seed("mig", json.dumps(meta))
    c = conn()
    try:
        SRV._beat(c, "mig", delivered=1)
        c.commit()
    finally:
        c.close()
    notes = note_of("mig", "hb_read_migrate")
    EXPECT = "kept_pre_epoch=2 dropped_post_epoch=1 ring=3"
    check("D2a note byte-identical to 45f6ab6 output", notes == [EXPECT], notes)
    m = json.loads(raw_meta("mig"))
    check("D2b hb_read ring = kept(2)+now, hb intact (mutation in place)",
          len(m.get("hb_read") or []) == 3 and len(m.get("hb") or []) == 3, m)

    print("\nD3 去重窗分支：两次 `_beat` 同窗，note/环两版一致（不写即为不写）")
    seed("dup", json.dumps({"token_hash": "x", "f6_read_epoch": 1750000000,
                            "hb": ["2020-01-01 00:00:00"],
                            "hb_read_migrated": True,
                            "hb_read": ["2020-01-01 00:00:00"]}))
    c = conn()
    try:
        SRV._beat(c, "dup", delivered=1)
        first = raw_meta("dup")
        SRV._beat(c, "dup", delivered=1)
        second = raw_meta("dup")
        c.commit()
    finally:
        c.close()
    check("D3a second beat inside 120s window is a no-op", first == second)

    print("\nD4 事务层（dsh #1461 §二）：坏行走完整 list_comments_since —— 不抛、游标前进、字节原样")
    # 反证面：_beat 的 fail-soft 若被删，DEEP 行在 _beat 内抛 RecursionError；由于
    # list_comments_since 的「游标 UPDATE → heartbeat_and_recompute → _beat」全在
    # 同一个 with db()（只在正常返回 commit）里，异常回滚的是游标 ⇒ 该成员会永久
    # 卡在同一批未读（不是一次 500）。这一格断在事务层，D1a/D1b 看不见游标。
    seed("polldeep", DEEP)
    c = conn()
    try:
        c.execute("INSERT INTO threads(title, author, created_at)"
                  " VALUES(?,?,datetime('now'))", ("poll-txn", "otherposter"))
        tid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO comments(thread_id, author, body)"
                  " VALUES(?,?,?)", (tid, "otherposter", "fresh delivery for @polldeep"))
        maxid = c.execute("SELECT COALESCE(MAX(id), 0) FROM comments").fetchone()[0]
        c.commit()
    finally:
        c.close()
    pbefore = raw_meta("polldeep")
    poll_err = None
    try:
        out = SRV.list_comments_since(author="polldeep", since="1h")
    except BaseException as e:  # noqa: BLE001 - the failure mode under test
        out, poll_err = "", e
    check("D4a full poll path does not raise", poll_err is None,
          type(poll_err).__name__ if poll_err else "")
    c = conn()
    try:
        prow = c.execute("SELECT last_read, last_delivered_id FROM participants"
                         " WHERE author='polldeep'").fetchone()
        audits_poll = [r["action"] for r in c.execute(
            "SELECT action FROM verdict_events WHERE author='polldeep'"
            " AND action IN ('meta_unparsable','hb_read_migrate')")]
    finally:
        c.close()
    check("D4b cursor advanced (last_delivered_id >= max comment id)",
          prow is not None and (prow["last_delivered_id"] or 0) >= maxid,
          (prow["last_delivered_id"] if prow else None, maxid))
    check("D4c last_read written", prow is not None and prow["last_read"] is not None)
    check("D4d bad-row bytes untouched by the poll txn", raw_meta("polldeep") == pbefore)
    check("D4e no hb_read_migrate / meta_unparsable audit from a read path (C3)",
          audits_poll == [], audits_poll)

    print(f"\n{'='*60}\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
