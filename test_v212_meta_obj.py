#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.12.0 票 B 第一批回归：坏 meta 行（启动站 + 恢复三路）。

冻结面（#1307 §二/§四/§五、#1315 §二、#1406 开工令）：
  - 4 态 reader（行不存在 / 非法 JSON / 合法非对象 / dict）；
  - 启动站 `_v21_token_backfill` 逐行 fail-soft + 审计，坏行字节原样；
  - 恢复三路：`claim_token` 自愈重建（先审计指纹）、`ack_token` 报真因不误诊、
    `reset_token(human_override=True)` 在坏行上必须跑通；
  - 审计只落 author + sha256 前缀 + 长度，不落原文，且按 note 去重；
  - `_get_meta` 旧返回形状不变（非对象原样返回），K26b / `_invite` 不在本笔。

Run: .venv/bin/python test_v212_meta_obj.py
"""
import json
import os
import re
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_fixture as fx

PASS, FAIL = [], []
DB = None
SRV = None
BAD_INPUTS = [
    ("invalid_json", '{"token_hash": '),
    ("non_object_falsy", "[]"),
    ("non_object_truthy", "5"),
]


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f" — {detail}" if detail and not cond else ""))


def conn():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def seed(author, raw):
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
        row = c.execute("SELECT meta FROM participants WHERE author=?", (author,)).fetchone()
        return None if row is None else row["meta"]
    finally:
        c.close()


def audits(author, action="meta_unparsable"):
    c = conn()
    try:
        return [r["note"] for r in c.execute(
            "SELECT note FROM verdict_events WHERE author=? AND action=? ORDER BY id",
            (author, action))]
    finally:
        c.close()


def main():
    global DB, SRV
    fd, DB = tempfile.mkstemp(prefix="t12-meta-obj-", suffix=".db")
    os.close(fd)
    os.unlink(DB)
    SRV = fx.stub_env(DB)
    SRV.init_db()

    print("S1 四态 reader")
    check("S1a row-missing", SRV._meta_obj(None) == (SRV.META_MISSING, {}))
    check("S1b invalid JSON", SRV._meta_obj('{"x": ') == (SRV.META_INVALID_JSON, {}))
    check("S1c falsy non-object keeps payload",
          SRV._meta_obj("[]") == (SRV.META_NON_OBJECT, []))
    check("S1d truthy non-object keeps payload",
          SRV._meta_obj("5") == (SRV.META_NON_OBJECT, 5))
    check("S1e dict", SRV._meta_obj('{"a": 1}') == (SRV.META_DICT, {"a": 1}))
    check("S1f empty text is an empty dict", SRV._meta_obj("") == (SRV.META_DICT, {}))
    seed("s1f", "[]")
    c = conn()
    try:
        check("S1g _get_meta keeps legacy non-object return",
              SRV._get_meta(c, "s1f") == [])
        check("S1h _get_meta missing row -> {}",
              SRV._get_meta(c, "s1h-nobody") == {})
    finally:
        c.close()

    print("S2 启动站：逐行 fail-soft + 审计 + 字节原样")
    seed("start-bad-0", BAD_INPUTS[0][1])
    seed("start-bad-1", BAD_INPUTS[1][1])
    seed("start-bad-2", BAD_INPUTS[2][1])
    seed("start-good", json.dumps({"token_hash": "a" * 64}))
    c = conn()
    try:
        SRV._v21_token_backfill(c)
        c.commit()
    finally:
        c.close()
    check("S2a startup survives all three bad rows", True)
    for i, (label, raw) in enumerate(BAD_INPUTS):
        author = f"start-bad-{i}"
        check(f"S2b bad row bytes untouched ({label})", raw_meta(author) == raw,
              repr(raw_meta(author)))
        notes = audits(author)
        check(f"S2c audited once ({label})", len(notes) == 1, repr(notes))
        check(f"S2d audit names the site ({label})",
              bool(notes) and "where=init_db" in notes[0], repr(notes))
    good = json.loads(raw_meta("start-good"))
    check("S2e good row still backfilled",
          good.get("token_acked") is True and "token_acked_at" in good, repr(good))
    c = conn()
    try:
        SRV._v21_token_backfill(c)
        c.commit()
    finally:
        c.close()
    check("S2f restart does not duplicate the audit",
          len(audits("start-bad-1")) == 1, repr(audits("start-bad-1")))

    print("S3 claim_token：坏行自愈，先审计指纹再重建")
    for label, raw in BAD_INPUTS:
        author = f"claim-{label}"
        seed(author, raw)
        out = SRV.claim_token(author)
        after = raw_meta(author)
        try:
            parsed = json.loads(after)
        except ValueError:
            parsed = None
        check(f"S3a claim succeeds ({label})", "Token issued" in out, out)
        check(f"S3b meta rebuilt to dict ({label})", isinstance(parsed, dict), repr(after))
        check(f"S3c rebuilt row has token_hash ({label})",
              isinstance(parsed, dict) and bool(parsed.get("token_hash")), repr(after))
        check(f"S3d bad-row audit present ({label})",
              any("where=claim_token" in n for n in audits(author)), repr(audits(author)))

    print("S4 ack_token：报真因、不写、不误诊")
    for label, raw in BAD_INPUTS:
        author = f"ack-{label}"
        seed(author, raw)
        out = SRV.ack_token(author, "0" * 32)
        check(f"S4a ack reports meta_unparsable ({label})",
              "meta_unparsable" in out and "no token issued" not in out, out)
        check(f"S4b ack leaves bytes untouched ({label})", raw_meta(author) == raw,
              repr(raw_meta(author)))
        check(f"S4c ack audit present ({label})",
              any("where=ack_token" in n for n in audits(author)), repr(audits(author)))

    print("S5 reset_token：坏行 break-glass 必须跑通")
    for label, raw in BAD_INPUTS:
        author = f"reset-{label}"
        seed(author, raw)
        out = SRV.reset_token(author, human_override=True)
        after = raw_meta(author)
        check(f"S5a reset does not lie 'unknown author' ({label})",
              "unknown author" not in out and "Token cleared" in out, out)
        check(f"S5b reset rebuilds a clean dict ({label})",
              after is not None and json.loads(after) == {}, repr(after))
        check(f"S5c reset audit present ({label})",
              any("where=reset_token" in n for n in audits(author)), repr(audits(author)))
        out2 = SRV.claim_token(author)
        check(f"S5d claim works after reset ({label})", "Token issued" in out2, out2)

    print("S6 reset_token 的既有门不变")
    seed("reset-gated", "[]")
    out = SRV.reset_token("reset-gated", human_override=False)
    check("S6a missing human_override still rejected",
          out.startswith("ERROR: reset_token requires human_override=True"), out)
    check("S6b rejected reset does not rewrite bytes", raw_meta("reset-gated") == "[]",
          repr(raw_meta("reset-gated")))
    out = SRV.reset_token("reset-nobody", human_override=True)
    check("S6c truly unknown author still rejected", "unknown author" in out, out)

    print("S7 审计不含原文（专用哨兵，避免单字符 payload 误判）")
    for i, raw in enumerate(['{"secret":"LEAK-7f3",', '["LEAK-7f3"]', '"LEAK-7f3"']):
        author = f"fp-{i}"
        seed(author, raw)
        SRV.reset_token(author, human_override=True)
        notes = audits(author)
        check(f"S7a no raw payload in audit ({i})",
              all("LEAK-7f3" not in n for n in notes), repr(notes))
        check(f"S7b fingerprint prefix present ({i})",
              bool(notes) and all(re.search(r"sha256=[0-9a-f]{16}", n) for n in notes),
              repr(notes))
        check(f"S7c length recorded ({i})",
              bool(notes) and any(f"len={len(raw.encode())}" in n for n in notes),
              repr(notes))

    os.unlink(DB)
    print(f"\n{PASS.__len__()} passed, {FAIL.__len__()} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
        sys.exit(1)
    print("✅ META_OBJ TESTS PASSED: four states, startup fail-soft, claim self-heal, "
          "ack truthful rejection, reset break-glass, fingerprint-only audit.")


if __name__ == "__main__":
    main()
