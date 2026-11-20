#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DEBT #25 负例验收——DATA_DIR 缺失时 form_b nonce 交付面的行为钉死（board #102/dsh #1644 §五）。

钉死条款：
  ① fail-open 为有意行为：DATA_DIR 指向不存在目录 ⇒ request_form_b 两模式
     （add / initiate）仍返回 "nonce requested"、verdict_events 审计行仍在
     （form_b_requested）；绝不因文件交付面问题拒绝业务动作。
  ② 交付面降级必须可见：写点失败 ⇒ stderr 落
     "[form_b] WARNING: nonce file delivery failed (...)"（→ server.log），
     与 stdout 的 nonce 行同屏可查——旧实现静默 except: pass。
  正控制：DATA_DIR 恢复后同线程再请求 ⇒ 文件真实落盘（负例确实归因于 DATA_DIR）。

Run: python test_v213_formb_nonce_negative.py
"""
import contextlib
import hashlib
import io
import json
import os
import pathlib
import re
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_dbfd, DB = tempfile.mkstemp(prefix="t213-formb-neg-", suffix=".db")
os.close(_dbfd)
os.environ["REVIEWBOARD_DB"] = DB
import test_fixture as fx

fx.stub_env(DB)
import server as SRV

SRV.init_db()

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else " ❌ ") + name + (f" — {detail}" if detail and not cond else ""))


def conn():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def seed(author, meta):
    c = conn()
    try:
        c.execute("INSERT OR REPLACE INTO participants(author, first_seen, last_seen, meta)"
                  " VALUES(?,?,?,?)", (author, "2026-11-21 00:00:00", "2026-11-21 00:00:00", meta))
        c.commit()
    finally:
        c.close()


def audits(author, action):
    c = conn()
    try:
        return [r["note"] for r in c.execute(
            "SELECT note FROM verdict_events WHERE author=? AND action=? ORDER BY id",
            (author, action))]
    finally:
        c.close()


TOKEN = "deadbeef"
CREATOR = "fb-neg"
BAD_DIR = pathlib.Path(tempfile.gettempdir()) / "no-such-dir-formb-negative"

# setup: valid-token creator + open thread
seed(CREATOR, json.dumps({"token_hash": hashlib.sha256(TOKEN.encode()).hexdigest()}))
fx.prime_beats(DB, [CREATOR])
out = SRV.create_thread(title="form-b-negative", author=CREATOR)
tid = int(re.search(r"#(\d+)", out).group(1)) if "Created thread" in out else None
check("setup: thread created", tid is not None, out)

saved_data_dir = SRV.DATA_DIR
try:
    # ---- 负例：DATA_DIR 指向不存在目录（add 模式） ----
    SRV.DATA_DIR = BAD_DIR
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        out_add = SRV.request_form_b(tid, CREATOR, ["fb-p1"], token=TOKEN)
    check("① add: 工具仍返 nonce requested（fail-open 钉死）",
          "form_b nonce requested" in out_add, out_add)
    check("① add: 审计行仍在（form_b_requested）",
          len(audits(CREATOR, "form_b_requested")) >= 1, audits(CREATOR, "form_b_requested"))
    check("② add: stderr 落 WARNING（降级可见）",
          "WARNING: nonce file delivery failed" in err.getvalue(), err.getvalue())
    check("② add: nonce 绝不进返回体（交付面契约不变）",
          re.search(r"nonce=[0-9a-f]{32}", out_add) is None, out_add)

    # ---- 负例：initiate 模式（第二个写点） ----
    err2 = io.StringIO()
    with contextlib.redirect_stderr(err2):
        out_init = SRV.request_form_b(None, CREATOR, ["fb-i1"], token=TOKEN,
                                      purpose="initiate", title="form-b-negative-title")
    check("① initiate: 工具仍返 nonce requested（第二个写点同钉）",
          "form_b initiate nonce requested" in out_init, out_init)
    check("① initiate: 审计行仍在（form_b_initiated——initiate 用独立枚举）",
          len(audits(CREATOR, "form_b_initiated")) >= 1, audits(CREATOR, "form_b_initiated"))
    check("② initiate: stderr 落 WARNING",
          "WARNING: nonce file delivery failed" in err2.getvalue(), err2.getvalue())

    # ---- 正控制：DATA_DIR 恢复 ⇒ 文件真实落盘（负例确因 DATA_DIR） ----
    SRV.DATA_DIR = saved_data_dir
    out_ok = SRV.request_form_b(tid, CREATOR, ["fb-p2"], token=TOKEN)
    delivered = (saved_data_dir / "form_b_nonce.txt")
    check("正控制: DATA_DIR 恢复后文件真实落盘",
          delivered.exists() and "[form_b]" in delivered.read_text(encoding="utf-8"),
          str(delivered))
finally:
    SRV.DATA_DIR = saved_data_dir

print(f"\nPASS={len(PASS)} FAIL={len(FAIL)}")
sys.exit(0 if not FAIL else 1)
