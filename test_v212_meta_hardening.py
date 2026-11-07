#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.12.0 票B 加固回归（GPT P2 via #1438/#1445 + dsh #1446 T-deep）。

三条独立洞，别并成一条（dsh #1446 §三）：
  P2   — `_meta_fingerprint` 只收 str；非 UTF-8 BLOB 坏行让审计路径抛
         AttributeError ⇒ `init_db()` 仍被单条坏行打断。
  deep — `json.loads` 在超深嵌套上抛 RecursionError（⊂ RuntimeError），
         ValueError/TypeError 守卫接不住 ⇒ 启动站/读面仍炸。
  blob-ok — 合法 UTF-8 的 BLOB 必须当正常行：不审计、不崩、指纹不被调用
         （防 P2 修复过度触发，dsh #1442 §三）。

Run: python3 test_v212_meta_hardening.py
"""
import hashlib
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
BLOB_BAD = b"\xff\xfe\x00bad"
BLOB_OK = b'{"token_hash": "cafebabe"}'


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  \u2705 " if cond else "  \u274c ") + name + (f" \u2014 {detail}" if detail and not cond else ""))


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
        row = c.execute("SELECT meta, typeof(meta) AS t FROM participants WHERE author=?",
                        (author,)).fetchone()
        return (None, None) if row is None else (row["meta"], row["t"])
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
    fd, DB = tempfile.mkstemp(prefix="t12-meta-harden-", suffix=".db")
    os.close(fd)
    os.unlink(DB)
    SRV = fx.stub_env(DB)
    SRV.init_db()

    print("\nP1 P2 直接面：bytes 不再 AttributeError，指纹按原字节算")
    fp, ln = SRV._meta_fingerprint(BLOB_BAD)
    check("P1a bytes fingerprint == sha256(raw)[:16]",
          fp == hashlib.sha256(BLOB_BAD).hexdigest()[:16], fp)
    check("P1b bytes length == raw byte length", ln == len(BLOB_BAD), ln)
    fp_s, ln_s = SRV._meta_fingerprint('{"a": 1}')
    check("P1c str path unchanged",
          fp_s == hashlib.sha256('{"a": 1}'.encode()).hexdigest()[:16] and ln_s == 8,
          (fp_s, ln_s))
    fp_n, ln_n = SRV._meta_fingerprint(None)
    check("P1d None path unchanged", (fp_n, ln_n) == (hashlib.sha256(b"").hexdigest()[:16], 0))
    fp_z, ln_z = SRV._meta_fingerprint("")
    check("P1e empty-TEXT '' (str branch) fingerprint == sha256(b'')[:16], len=0 (#1622 P2；标签如实：这是 str 分支)",
          (fp_z, ln_z) == ("e3b0c44298fc1c14", 0), (fp_z, ln_z))
    fp_b, ln_b = SRV._meta_fingerprint(b"")
    check("P1f empty-BLOB b'' (bytes branch) fingerprint == sha256(b'')[:16], len=0 (dsh #1624 C3：存储类两分支分格，不再同值共格)",
          (fp_b, ln_b) == ("e3b0c44298fc1c14", 0), (fp_b, ln_b))

    print("\nP2 启动站 + 非 UTF-8 BLOB：init_db 不抛、字节原样、审计按原字节")
    seed("blob-bad", BLOB_BAD)
    before = raw_meta("blob-bad")
    try:
        SRV.init_db()
        raised = None
    except BaseException as e:  # noqa: BLE001 — the assertion is "nothing escapes"
        raised = type(e).__name__
    check("P2a init_db returns (was AttributeError)", raised is None, raised)
    after = raw_meta("blob-bad")
    check("P2b stored bytes unchanged (blob, exact)", after == before, (before, after))
    notes = audits("blob-bad")
    check("P2c exactly one audit row", len(notes) == 1, notes)
    check("P2d audit names len=original byte length", bool(notes) and f"len={len(BLOB_BAD)}" in notes[0], notes)
    check("P2e audit fingerprint is of raw bytes",
          bool(notes) and hashlib.sha256(BLOB_BAD).hexdigest()[:16] in notes[0], notes)
    SRV.init_db()
    check("P2f audit dedup on repeated init_db", len(audits("blob-bad")) == 1, audits("blob-bad"))

    print("\nP2g 空 BLOB b''：init_db 不塌缩不覆盖、指纹=空字节 sha256、len=0 (#1622 P2)")
    seed("blob-empty", b"")
    before_e = raw_meta("blob-empty")
    SRV.init_db()
    check("P2h stored bytes byte-identical (typeof blob)",
          before_e == (b"", "blob") and raw_meta("blob-empty") == before_e,
          (before_e, raw_meta("blob-empty")))
    notes_e = audits("blob-empty")
    check("P2i exactly one audit, state=invalid_json",
          len(notes_e) == 1 and "state=invalid_json" in notes_e[0], notes_e)
    check("P2j fingerprint = sha256(b'')[:16] prefix, len=0",
          bool(notes_e) and "sha256=e3b0c44298fc1c14" in notes_e[0] and "len=0" in notes_e[0],
          notes_e)
    SRV.init_db()
    check("P2k audit dedup on repeated init_db", len(audits("blob-empty")) == 1,
          audits("blob-empty"))

    print("\nP3 合法 UTF-8 BLOB：当正常行（不审计 / 不崩 / 正常回填）")
    seed("blob-ok", BLOB_OK)
    try:
        SRV.init_db()
        raised = None
    except BaseException as e:  # noqa: BLE001
        raised = type(e).__name__
    check("P3a init_db returns", raised is None, raised)
    check("P3b no meta_unparsable audit", audits("blob-ok") == [], audits("blob-ok"))
    meta_ok = json.loads(raw_meta("blob-ok")[0])
    check("P3c treated as dict and token_acked backfilled",
          meta_ok.get("token_hash") == "cafebabe" and meta_ok.get("token_acked") is True, meta_ok)

    print("\nP4 超深嵌套（depth=20000）：init_db / _meta_obj 不抛")
    try:
        json.loads(DEEP)
        deep_raises = False
    except RecursionError:
        deep_raises = True
    check("P4a fixture actually trips RecursionError (depth 20000)", deep_raises)
    seed("deep", DEEP)
    before = raw_meta("deep")
    try:
        SRV.init_db()
        raised = None
    except BaseException as e:  # noqa: BLE001
        raised = type(e).__name__
    check("P4b init_db returns (was RecursionError)", raised is None, raised)
    check("P4c stored bytes unchanged", raw_meta("deep") == before)
    notes = audits("deep")
    check("P4d one audit, state=invalid_json (no fifth state)",
          len(notes) == 1 and "state=invalid_json" in notes[0], notes)
    try:
        st, _obj = SRV._meta_obj(DEEP)
        raised = None
    except BaseException as e:  # noqa: BLE001
        st, raised = None, type(e).__name__
    check("P4e _meta_obj returns invalid_json without raising",
          raised is None and st == SRV.META_INVALID_JSON, (st, raised))

    print("\nP5 完整 init_db()：5 类坏行 + 1 条好行同库，坏行全原样、好行照常")
    rows = [("mix-invalid", '{"token_hash": '), ("mix-list", "[]"), ("mix-int", "5"),
            ("mix-blob", BLOB_BAD), ("mix-empty", b""), ("mix-deep", DEEP)]
    for a, raw in rows:
        seed(a, raw)
    seed("mix-good", '{"token_hash": "deadbeef"}')
    before = {a: raw_meta(a) for a, _ in rows}
    try:
        SRV.init_db()
        raised = None
    except BaseException as e:  # noqa: BLE001
        raised = type(e).__name__
    check("P5a init_db returns on mixed bad-row DB", raised is None, raised)
    check("P5b every bad row byte-identical", all(raw_meta(a) == before[a] for a, _ in rows),
          {a: raw_meta(a) for a, _ in rows})
    check("P5c every bad row audited exactly once",
          all(len(audits(a)) == 1 for a, _ in rows),
          {a: audits(a) for a, _ in rows})
    good = json.loads(raw_meta("mix-good")[0])
    check("P5d good row still backfilled", good.get("token_acked") is True, good)

    print("\nP6 _invite 写点 fail-closed（dsh #1624 §二 (ii) 单源 `_meta_obj`：空 BLOB/坏行不被洗成健康行）")
    seed("inv-empty", b"")
    before_ie = raw_meta("inv-empty")
    _c = conn(); SRV._invite(_c, "inv-empty", 424242); _c.commit(); _c.close()
    check("P6a 空 BLOB 行 invite 后逐字节不变（b''=invalid_json，不再被 `or` 吞成健康行）",
          raw_meta("inv-empty") == before_ie == (b"", "blob"),
          (before_ie, raw_meta("inv-empty")))
    seed("inv-shape", '{"hb": "not-a-list"}')
    before_is = raw_meta("inv-shape")
    _c = conn(); SRV._invite(_c, "inv-shape", 424242); _c.commit(); _c.close()
    check("P6b value_shape（⑤态坏行）invite 后逐字节不变（行状态模型里同为 corrupt，不洗）",
          raw_meta("inv-shape") == before_is, (before_is, raw_meta("inv-shape")))
    seed("inv-ok", '{"token_hash": "cafebabe", "probe": ["x"]}')
    _c = conn(); SRV._invite(_c, "inv-ok", 424242); _c.commit(); _c.close()
    m_ok = json.loads(raw_meta("inv-ok")[0])
    check("P6c 健康行 invite 照常写 invited_at/invited_thread 且他人键保留",
          m_ok.get("invited_at") and m_ok.get("invited_thread") == 424242
          and m_ok.get("token_hash") == "cafebabe" and m_ok.get("probe") == ["x"], m_ok)
    _c = conn(); SRV._invite(_c, "inv-new", None); _c.commit(); _c.close()
    _row = conn().execute("SELECT first_seen, last_seen, last_read, meta FROM participants"
                          " WHERE author='inv-new'").fetchone()
    check("P6d 新名受邀：first_seen 有值、last_seen/last_read NULL、meta=invited_at（1.1b/C2 不变量）",
          _row["first_seen"] and _row["last_seen"] is None and _row["last_read"] is None
          and json.loads(_row["meta"]).get("invited_at"), tuple(_row))

    print(f"\n{'='*60}\n{PASS.__len__()} passed, {FAIL.__len__()} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
