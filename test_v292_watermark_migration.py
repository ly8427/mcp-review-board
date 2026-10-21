#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.9.2 水位线迁移边界回归（GPT round-2 §2 on 7f06517 的四类要求）。

纯逻辑形态（stub fastmcp/starlette + 临时库，直接对 _revision_floor /
_floor_wm_epoch 断言），覆盖：

  A. 旧版升级: 纪元前 bump（无 floor_wm= token，id ≤ epoch）经时间戳回退
     放行（ok=True），留 floor_wm_legacy_fallback 审计。
  B. token 优先: id ≤ epoch 但带合法 floor_wm= 的事件按 token 解析，
     不回落时间戳坐标（两个坐标刻意给不同值以区分）。
  C. 恢复/导入后 id-epoch 倒挂: 纪元后导入的无 token bump 硬挡
     （floor_wm_missing，禁静默回落）；随后补发带 token 的新 bump = 出口。
  D. 迁移中断后重启幂等: epoch meta 丢失 → 重启重取 max(CODE, 本库 MAX)；
     恢复动作本身不挡 token 事件、不产生误挡。
  E. meta 只升不降: meta 高于代码常量不动；低于则抬升（防回落）。
  F. 畸形事件格式: note=NULL 与非数字 token（floor_wm=abc）都不被 token
     正则命中——纪元后硬挡同缺 token；纪元前按时间戳安全回退（不崩、不误 0）。

合成 bump 用稀疏显式 id（100 起、步进 100），与 epoch 引导/回退审计的
自增行天然隔离（引导审计本身也是 verdict_events 行）。

Run: .venv/bin/python test_v292_watermark_migration.py
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


def _rw_conn(db):
    conn = sqlite3.connect(db, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def with_rw(db, fn):
    conn = _rw_conn(db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        r = fn(conn)
        conn.commit()
        return r
    finally:
        conn.close()


def set_created(db, table, row_id, ts):
    with_rw(db, lambda c: c.execute(f"UPDATE {table} SET created_at=? WHERE id=?", (ts, row_id)))


def insert_bump(db, ev_id, note, ts):
    """手插 bump 审计事件（模拟旧版遗留/导入/畸形格式；id 显式给定保证可重现）"""
    with_rw(db, lambda c: c.execute(
        "INSERT INTO verdict_events(id, thread_id, author, action, revision, costed, note, created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (ev_id, 1, "z", "bump_revision", 1, 0, note, ts)))


def epoch_of(db):
    return with_rw(db, lambda conn: srv._floor_wm_epoch(conn))


def floor_of(db, tid=1):
    """_revision_floor 会写回退审计 → 必须用可写连接并提交"""
    conn = _rw_conn(db)
    try:
        wm, ok, why = srv._revision_floor(conn, tid)
        conn.commit()
        return wm, ok, why
    finally:
        conn.close()


def audits(db, action):
    conn = _rw_conn(db)
    try:
        return conn.execute(
            "SELECT note FROM verdict_events WHERE action=? ORDER BY id", (action,)).fetchall()
    finally:
        conn.close()


def max_ev_id(db):
    conn = _rw_conn(db)
    try:
        return conn.execute("SELECT COALESCE(MAX(id),0) FROM verdict_events").fetchone()[0]
    finally:
        conn.close()


def main():
    global srv
    tmp = tempfile.mkdtemp(prefix="rb-wmmig-")
    db = os.path.join(tmp, "wm.db")
    srv = fx.stub_env(db)
    srv.init_db()

    # 自由帖（无 quorum → 无 wake_infra/F6 仪式）；三条评论固定时间戳
    out = srv.create_thread(title="wm-mig", author="z", context="watermark migration edges")
    assert out.startswith("Created thread #1"), out
    cids = []
    for body in ("c1", "c2", "c3"):
        out = srv.post_comment(thread_id=1, author="z", body=body)
        assert out.startswith("Posted"), out
        cids.append(int(out.split("#")[1].split(" ")[0]))
    for cid, ts in zip(cids, ("2026-10-21 10:00:01", "2026-10-21 10:00:02", "2026-10-21 10:00:03")):
        set_created(db, "comments", cid, ts)
    c1, c2, c3 = cids

    # ---------- A. 旧版升级: 纪元前无 token bump → 时间戳回退放行 ----------
    insert_bump(db, 100, "pre-v2.9.2 bump (old server, no token)", "2026-10-21 10:00:02")
    e = epoch_of(db)
    check("A: bootstrap takes local MAX(event ids)", e == 100, f"epoch={e}")
    wm, ok, why = floor_of(db)
    fb = audits(db, "floor_wm_legacy_fallback")
    check("A: legacy bump falls back by timestamp (ok, wm=c1)",
          ok and wm == c1, f"wm={wm} ok={ok} why={why}")
    check("A: fallback audited (floor_wm_legacy_fallback, ev.id=100)",
          any(r["note"] and "ev.id=100" in r["note"] for r in fb), f"audits={[r['note'] for r in fb]}")

    # ---------- B. token 优先: 两坐标刻意背离（时间戳会给 0，token 给 2） ----------
    insert_bump(db, 200, "floor_wm=2; restored-with-token", "2026-10-21 10:00:00")
    wm, ok, why = floor_of(db)
    check("B: token coordinate wins over legacy timestamp at id<=epoch",
          ok and wm == 2, f"wm={wm} ok={ok} why={why}")

    # ---------- C. 恢复/导入后 id-epoch 倒挂: 纪元后无 token 硬挡; 补 token bump = 出口 ----------
    insert_bump(db, 300, "imported bump lacking token (restore over old epoch)", "2026-10-21 11:00:00")
    wm, ok, why = floor_of(db)
    check("C1: imported post-epoch tokenless bump hard-blocks (no silent fallback)",
          ok is False and "缺 floor_wm" in why, f"ok={ok} why={why}")
    insert_bump(db, 400, "floor_wm=3; proper re-bump after bad import", "2026-10-21 11:01:00")
    wm, ok, why = floor_of(db)
    check("C2: proper tokened re-bump is the documented exit",
          ok and wm == 3, f"wm={wm} ok={ok} why={why}")

    # ---------- D. 迁移中断后重启幂等: epoch meta 丢失 → 重取本地 MAX ----------
    with_rw(db, lambda c: c.execute("DELETE FROM meta WHERE key='floor_wm_epoch'"))
    e2 = epoch_of(db)
    check("D: fresh bootstrap after meta loss re-derives epoch=max(0,400)",
          e2 == 400, f"epoch={e2}")
    inits = audits(db, "floor_wm_epoch_init")
    check("D: re-bootstrap leaves its own floor_wm_epoch_init audit",
          len(inits) >= 2, f"inits={len(inits)}")
    wm, ok, why = floor_of(db)
    check("D: recovery does not disturb tokened latest bump",
          ok and wm == 3, f"wm={wm} ok={ok} why={why}")

    # ---------- F. 畸形事件格式（纪元拉高后：之下=安全回退；之上=硬挡） ----------
    # 回退审计也占 verdict_events 自增 id → bump id 一律动态取 max+1
    with_rw(db, lambda c: c.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('floor_wm_epoch', '1000000')"))
    insert_bump(db, max_ev_id(db) + 1, None, "2026-10-21 10:00:03")     # NULL note，id≤epoch
    wm, ok, why = floor_of(db)
    check("F1: NULL-note bump below epoch → timestamp fallback (wm=c2), no crash",
          ok and wm == c2, f"wm={wm} ok={ok} why={why}")
    insert_bump(db, max_ev_id(db) + 1, "floor_wm=abc; not-a-number", "2026-10-21 10:00:01")
    wm, ok, why = floor_of(db)
    check("F2: non-numeric floor_wm= token not parsed → legacy fallback (wm=0)",
          ok and wm == 0, f"wm={wm} ok={ok} why={why}")
    insert_bump(db, 1000000 + 100, None, "2026-10-21 12:00:00")  # id>epoch，NULL note
    wm, ok, why = floor_of(db)
    check("F3: NULL-note bump above epoch hard-blocks (same as missing)",
          ok is False and "缺 floor_wm" in why, f"ok={ok} why={why}")

    # ---------- E. meta 只升不降（放最后：会改动全局 epoch） ----------
    with_rw(db, lambda c: c.execute(
        "UPDATE meta SET value='999999' WHERE key='floor_wm_epoch'"))
    check("E1: meta above code constant is never lowered", epoch_of(db) == 999999)
    with_rw(db, lambda c: c.execute(
        "UPDATE meta SET value='-1' WHERE key='floor_wm_epoch'"))
    check("E2: meta below code constant is raised (only-increase)", epoch_of(db) == 0)

    print("=" * 60)
    print(f"watermark migration: {len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
