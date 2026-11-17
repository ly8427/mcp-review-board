#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batch2 1.4 行为夹具套件 — _reissue_window_end 单源契约（dsh #2351 §1 开写，
pi #2353 §1 收；外置载体 #117，接手回执 #113）。

验收面（全部符号锚，禁行号 — #2351 §1.4 绑定 4）：
  - 等式（绑定 4）：同一次采样内读面渲染的 window_ends_at == 公共函数返回值
    （单源后构造性成立；用例钉的是渲染面与函数值的逐字相等）
  - 夹具一（绑定 2）：零治理活动 ⇒ window_end=None，读面渲染
    "unlocked (no governance activity yet)"，不得兜回 ack 时刻
  - 夹具二（绑定 A 的核心判例）：ack 后又治理过 ⇒ 锚=最后治理活动，
    不得是 ack 时刻（旧读面 from-ack 锚的方向性证伪点）
  - 夹具三（绑定 1）：auto 档 ⇒ hours=AUTO_ACK_REISSUE_HOURS，
    hours 只认 token_acked_via
  - 绑定 3：last 不可解析 ⇒ 显式 "unparsable" + window_diag，段不静默消失；
    施行面经 _hours_since()==0.0 保持锁定（fail-closed 不变）
  - 单源静态核销：窗口 SELECT 在全文件恰 1 处（公共函数内）；
    claim_token/_token_blocked_face 均无第二份算术

Run: .venv/bin/python test_batch2_reissue_window.py
"""
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_fixture as fx

PASS, FAIL = [], []
FMT = "%Y-%m-%d %H:%M:%S"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f" — {detail}" if detail and not cond else ""))


def conn():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def seed_participant(author, acked_at, via):
    """直接往临时库写带 token ack 态 + 一条被闩帖子的参与者（夹具前置伪造，
    非绕过：读面窗口字符串挂在闩条目的 exit 上，真实形态即如此）。"""
    c = conn()
    try:
        meta = {"token_acked": True, "token_acked_at": acked_at, "token_acked_via": via,
                "token_blocked": {"12": {"tier": "manual", "since": "2026-01-01 00:00:00"}}}
        c.execute("INSERT INTO participants(author, first_seen, last_seen, last_read, meta)"
                  " VALUES(?,?,?,NULL,?)",
                  (author, "2026-01-01 00:00:00", "2026-01-01 00:00:00",
                   json.dumps(meta)))
        c.commit()
    finally:
        c.close()


def seed_governance(author, created_at, action="verdict"):
    """往临时库写一条治理活动（窗口锚的输入）。"""
    c = conn()
    try:
        c.execute("INSERT INTO verdict_events(thread_id, author, action, created_at)"
                  " VALUES(NULL, ?, ?, ?)", (author, action, created_at))
        c.commit()
    finally:
        c.close()


def main():
    global DB, SRV
    fd, DB = tempfile.mkstemp(prefix="t-batch2-reissue-", suffix=".db")
    os.close(fd)
    os.unlink(DB)
    SRV = fx.stub_env(DB)
    SRV.init_db()

    now = datetime.now(timezone.utc)
    ack_10h_ago = (now - timedelta(hours=10)).strftime(FMT)
    gov_2h_ago = (now - timedelta(hours=2)).strftime(FMT)
    gov_30m_ago = (now - timedelta(hours=0.5)).strftime(FMT)

    # ---- 夹具一（绑定 2）：零治理活动 ----------------------------------------
    print("F1 零治理活动：window_end=None（无窗口，不是未来窗口）")
    seed_participant("fx_none", ack_10h_ago, "explicit")
    c = conn()
    try:
        meta = json.loads(c.execute("SELECT meta FROM participants WHERE author='fx_none'"
                                    ).fetchone()[0])
        win = SRV._reissue_window_end(c, "fx_none", meta)
    finally:
        c.close()
    check("F1a window_end is None (no window, NOT ack-derived future)",
          win["window_end"] is None and win["reason"] == "unlocked_no_governance_activity",
          str(win))
    tb = SRV._token_blocked_face(conn(), "fx_none")
    rendered = "".join(str(v.get("exit", "")) for v in tb.values()) if tb else ""
    check("F1b read face renders unlocked-no-activity verbatim",
          "unlocked (no governance activity yet)" in rendered, rendered[:160])
    check("F1c read face does NOT fall back to ack-time window",
          (ack_10h_ago and (datetime.strptime(ack_10h_ago, FMT)
                            + timedelta(hours=24)).strftime(FMT)) not in rendered,
          rendered[:160])

    # ---- 夹具二（A 判例）：ack 后又治理过 — 锚=最后治理活动 ------------------
    print("F2 ack 后重治理：锚=最后治理活动，非 ack 时刻")
    seed_participant("fx_regov", ack_10h_ago, "explicit")
    seed_governance("fx_regov", gov_2h_ago)
    c = conn()
    try:
        meta = json.loads(c.execute("SELECT meta FROM participants WHERE author='fx_regov'"
                                    ).fetchone()[0])
        win = SRV._reissue_window_end(c, "fx_regov", meta)
        tb = SRV._token_blocked_face(c, "fx_regov")
    finally:
        c.close()
    expected_end = (datetime.strptime(gov_2h_ago, FMT)
                    + timedelta(hours=SRV.REISSUE_AFTER_HOURS)).strftime(FMT)
    ack_derived = (datetime.strptime(ack_10h_ago, FMT)
                   + timedelta(hours=SRV.REISSUE_AFTER_HOURS)).strftime(FMT)
    check("F2a window anchor = last governance activity (+24h)",
          win["window_end"] == expected_end, f"got {win['window_end']} want {expected_end}")
    check("F2b NOT the ack-anchored window (old second clock falsified)",
          win["window_end"] != ack_derived, ack_derived)
    rendered = "".join(str(v.get("exit", "")) for v in tb.values()) if tb else ""
    check("F2c read face == function value (equality binding, one sampling)",
          f"window_ends_at={win['window_end']}" in rendered, rendered[:160])
    check("F2d enforcement keeps locked (2h < 24h since last activity)",
          SRV._hours_since(win["last"]) < win["hours"])

    # ---- 夹具三（绑定 1）：auto 档 -------------------------------------------
    print("F3 auto 档：hours 只认 token_acked_via")
    seed_participant("fx_auto", ack_10h_ago, "auto")
    seed_governance("fx_auto", gov_30m_ago)
    c = conn()
    try:
        meta = json.loads(c.execute("SELECT meta FROM participants WHERE author='fx_auto'"
                                    ).fetchone()[0])
        win = SRV._reissue_window_end(c, "fx_auto", meta)
    finally:
        c.close()
    check("F3a hours == AUTO_ACK_REISSUE_HOURS (single judgment source)",
          win["hours"] == SRV.AUTO_ACK_REISSUE_HOURS, str(win["hours"]))
    check("F3b window_end = last + auto hours (0.5h elapsed < 1h lock)",
          win["window_end"] == (datetime.strptime(gov_30m_ago, FMT)
                                + timedelta(hours=SRV.AUTO_ACK_REISSUE_HOURS)).strftime(FMT),
          str(win["window_end"]))

    # ---- 绑定 3：不可解析的 last 显式化，施行面 fail-closed 不变 --------------
    print("F4 last 不可解析：显式 unparsable + diag；施行面锁定不变")
    seed_participant("fx_bad", ack_10h_ago, "explicit")
    seed_governance("fx_bad", "not-a-timestamp")
    c = conn()
    try:
        meta = json.loads(c.execute("SELECT meta FROM participants WHERE author='fx_bad'"
                                    ).fetchone()[0])
        win = SRV._reissue_window_end(c, "fx_bad", meta)
        tb = SRV._token_blocked_face(c, "fx_bad")
    finally:
        c.close()
    check("F4a window_end == 'unparsable' sentinel (explicit, not silent)",
          win["window_end"] == "unparsable"
          and win["reason"] == "unparsable_last_activity", str(win))
    rendered = "".join(str(v.get("exit", "")) for v in tb.values()) if tb else ""
    check("F4b read face carries sentinel + diag (segment did not vanish)",
          "window_ends_at=unparsable" in rendered
          and "window_diag=unparsable_last_activity" in rendered, rendered[:160])
    check("F4c enforcement stays fail-closed (_hours_since==0.0 ⇒ locked)",
          SRV._hours_since(win["last"]) == 0.0
          and not (win["window_end"] is None or SRV._hours_since(win["last"]) >= win["hours"]))

    # ---- 单源静态核销（绑定 4 的符号锚纪律）----------------------------------
    print("SA 单源静态：窗口 SELECT 全文件恰 1 处（公共函数内）")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py"),
               encoding="utf-8").read()
    # 单行特征片段（SELECT 跨三行拼接，源码字面量计数会假 0 —— 数这行）
    sel_line = "AND action IN ('verdict','verdict_free','bump_revision','set_quorum')"
    check("SA1 window SELECT appears exactly once (inside the shared fn)",
          src.count(sel_line) == 1, str(src.count(sel_line)))
    ct_pos = src.find("def claim_token")
    tb_pos = src.find("def _token_blocked_face")
    check("SA2 claim_token body has no second window SELECT",
          src[ct_pos:tb_pos].count(sel_line) == 0)
    tb_src = src[tb_pos:]
    tb_src = tb_src[:tb_src.find("\ndef ", 1)]
    check("SA4 read face no longer reads token_acked_at (anchor moved)",
          "token_acked_at" not in tb_src)
    check("SA5 read face calls the shared function",
          "_reissue_window_end(conn, author, meta)" in tb_src)

    print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
    if FAIL:
        for name in FAIL:
            print("  FAIL:", name)
        sys.exit(1)


if __name__ == "__main__":
    main()
