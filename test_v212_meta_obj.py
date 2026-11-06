#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.12.0 票 B 第一批回归：坏 meta 行（启动站 + 恢复三路）。

冻结面（#1307 §二/§四/§五、#1315 §二、#1406 开工令；β 修复批更新处注明）：
  - 状态 reader（行不存在 / 非法 JSON / 合法非对象 / dict；β 批加第五态
    value_shape = dict 但 hb/hb_read/probe 环值非字符串列表，#1550 §二）；
  - 启动站 `_v21_token_backfill` 逐行 fail-soft + 审计，坏行字节原样；
  - 恢复路（P2 = 方案A，#1478/#1479）：`claim_token` 对坏行**一律拒绝**（先审计
    指纹、字节原样、不 _touch；四类 CORRUPT 含 value_shape，#1556 四走法）、
    `ack_token` 报真因且只指 reset、`reset_token(human_override=True)` 是
    **repair**：R1–R5（#1556 §四 + pi #1557 §四 微补；S9）；
  - 审计只落 author + sha256 前缀 + 长度（value_shape 另带 key=，#1550 §二），
    不落原文，且按 note 去重；
  - P3 漏斗收口（#1562 §二，取代 batch1「非对象原样返回」冻结）：`_get_meta`
    **永不返回非 dict**；P3：`_token_gate` 的判定与文案共用同一次解析（坏行
    路径不再第三次读 meta）。

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
    # β 批 第五态 (#1550 §二两格夹具)：dict 但环值形状坏——top-level 四态看不见
    ("value_shape_str", '{"hb":"abc"}'),          # 腿A：今天被静默重建写回
    ("value_shape_list", '{"hb":[{"a":1}]}'),      # 腿B：今天 TypeError ≠ ValueError
    ("value_shape_probe", '{"probe":5}'),             # ④b：另两环同判（探针环）
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


def seen(author):
    c = conn()
    try:
        row = c.execute("SELECT last_seen FROM participants WHERE author=?", (author,)).fetchone()
        return None if row is None else row["last_seen"]
    finally:
        c.close()


def events(author):
    c = conn()
    try:
        return [dict(r) for r in c.execute(
            "SELECT action, note FROM verdict_events WHERE author=? ORDER BY id", (author,))]
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
        # P3 漏斗收口 (#1562 §二，取代 batch1 的「非对象原样返回」冻结):
        # _get_meta NEVER returns a non-dict — dict-total funnel.
        check("S1g _get_meta returns {} (never a non-dict) for corrupt rows",
              SRV._get_meta(c, "s1f") == {})
        check("S1h _get_meta missing row -> {}",
              SRV._get_meta(c, "s1h-nobody") == {})
    finally:
        c.close()

    print("S2 启动站：逐行 fail-soft + 审计 + 字节原样")
    for i, (label, raw) in enumerate(BAD_INPUTS):
        seed(f"start-bad-{i}", raw)
    seed("start-good", json.dumps({"token_hash": "a" * 64}))
    c = conn()
    try:
        SRV._v21_token_backfill(c)
        c.commit()
    finally:
        c.close()
    check("S2a startup survives all bad rows", True)
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

    print("S3 claim_token：坏行拒绝（P2=A），先审计指纹、字节原样、不发 token")
    for label, raw in BAD_INPUTS:
        author = f"claim-{label}"
        seed(author, raw)
        before_seen = seen(author)
        out = SRV.claim_token(author)
        after = raw_meta(author)
        check(f"S3a claim rejects with named state ({label})",
              "meta_unparsable" in out and "Token issued" not in out, out)
        check(f"S3b bytes untouched ({label})", after == raw, repr(after))
        check(f"S3c no issued hash appeared ({label})",
              re.search(r"[0-9a-f]{64}", after or "") is None, repr(after))
        check(f"S3d bad-row audit present ({label})",
              any("where=claim_token" in n for n in audits(author)), repr(audits(author)))
        check(f"S3e no token_issue audit ({label})",
              not any("token_issue" == r["action"] for r in events(author)), repr(events(author)))
        check(f"S3f rejected claim does not renew last_seen ({label})",
              seen(author) == before_seen, f"{before_seen!r} -> {seen(author)!r}")

    print("S4 ack_token：报真因、不写、不误诊")
    for label, raw in BAD_INPUTS:
        author = f"ack-{label}"
        seed(author, raw)
        out = SRV.ack_token(author, "0" * 32)
        check(f"S4a ack reports meta_unparsable ({label})",
              "meta_unparsable" in out and "no token issued" not in out, out)
        check(f"S4a2 ack names ONLY the reset exit ({label})",
              "claim_token first" not in out and "self-heal" not in out
              and "reset_token" in out, out)
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

    print("S8 P2=A 一致性 + P3 解析复用")
    for label, raw in BAD_INPUTS:
        author = f"consist-{label}"
        seed(author, raw)
        c = conn()
        try:
            gate = SRV._token_gate(c, author, "0" * 32)
        finally:
            c.close()
        out = SRV.claim_token(author)
        check(f"S8a gate and claim name the same corrupt state ({label})",
              "meta_unparsable" in gate and "meta_unparsable" in out, (gate, out))
        check(f"S8b gate disables token issuance and names reset as exit ({label})",
              "Token issuance is disabled" in gate and "reset_token" in gate, gate)
        check(f"S8c no claim substring at all for a corrupt row ({label})",
              "claim_token" not in gate, gate)
    # P3: _token_gate's reject path parses participants.meta exactly twice
    # (_token_ok + one decision parse); a third parse would mean decision and
    # text came from different reads.
    calls = {"n": 0}
    real = SRV._meta_state
    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)
    SRV._meta_state = counting
    try:
        author = "p3-count"
        seed(author, BAD_INPUTS[0][1])
        c = conn()
        try:
            SRV._token_gate(c, author, "0" * 32)
        finally:
            c.close()
        check("S8d P3: corrupt reject path does 2 parses, not 3", calls["n"] == 2, repr(calls))
    finally:
        SRV._meta_state = real

    # --- β 修复批：R1–R5 reset repair 契约（dsh #1556 §四 + pi #1557 §四 微补）---
    print("S9 R1 repair→claim→register_meta 全链（含 ④ value_shape 与 (i) 申报键移植）")
    REPAIR_INPUTS = BAD_INPUTS + [
        ("shape_with_decl", '{"hb":"abc","contract":"a","entry":"e1",'
                            '"f6_read_epoch":123,"token_blocked":{"t":1}}')]
    for label, raw in REPAIR_INPUTS:
        author = f"r1-{label}"
        seed(author, raw)
        c = conn()
        try:
            prow0 = c.execute("SELECT first_seen, last_read FROM participants"
                              " WHERE author=?", (author,)).fetchone()
        finally:
            c.close()
        out = SRV.reset_token(author, human_override=True)
        after = raw_meta(author)
        try:
            parsed = json.loads(after)
        except (ValueError, TypeError):
            parsed = None
        check(f"R1a repair rewrites to a dict, no lie ({label})",
              "unknown author" not in out and isinstance(parsed, dict), (out, after))
        check(f"R1b no token_hash after repair ({label})",
              isinstance(parsed, dict) and "token_hash" not in parsed, repr(parsed))
        check(f"R1c bad ring key does NOT revive ({label})",
              isinstance(parsed, dict) and "hb" not in parsed
              and "probe" not in parsed and "hb_read" not in parsed, repr(parsed))
        check(f"R1d declaration keys preserved only if legal str ({label})",
              isinstance(parsed, dict)
              and parsed.get("contract") == ("a" if label == "shape_with_decl" else None)
              and parsed.get("entry") == ("e1" if label == "shape_with_decl" else None),
              repr(parsed))
        out2 = SRV.claim_token(author)
        tok = re.search(r"[0-9a-f]{32}", out2)
        check(f"R1e claim succeeds with 32-hex token ({label})",
              "Token issued" in out2 and tok is not None, out2)
        if tok:
            out3 = SRV.register_meta(author, "a", entry="e1", token=tok.group(0))
            check(f"R1f register_meta post-step succeeds ({label})",
                  "ERROR" not in out3, out3)
        c = conn()
        try:
            prow = c.execute("SELECT first_seen, last_read FROM participants"
                             " WHERE author=?", (author,)).fetchone()
        finally:
            c.close()
        check(f"R1g first_seen/last_read untouched by repair ({label})",
              prow["first_seen"] == prow0["first_seen"] and prow["last_read"] is None,
              (prow0[:], prow[:]))

    print("S9 R2/R3/R4/R5 reset 既有面与留痕")
    out = SRV.reset_token("r2-nobody", human_override=True)
    check("R2a truly absent still unknown author", "unknown author" in out, out)
    check("R2b absent reset creates NO row", raw_meta("r2-nobody") is None)
    seed("r3-empty", "{}")
    out = SRV.reset_token("r3-empty", human_override=True)
    check("R3a meta='{}' row is NOT unknown author",
          "unknown author" not in out and "had none" in out, out)
    out4 = SRV.reset_token("r3-empty", human_override=True)
    check("R3b repeat reset on clean row still had none", "had none" in out4, out4)
    seed("r4-good", json.dumps({"token_hash": "a" * 64, "token_acked": True,
                                "hb": ["2026-01-02 00:00:00"], "contract": "b"}))
    out = SRV.reset_token("r4-good", human_override=True)
    expect = ("Token cleared for 'r4-good' (had one). The identity's next "
              "claim_token issues a fresh token.")
    check("R4a good-row message byte-identical", out == expect, (out, expect))
    notes = [r["note"] for r in events("r4-good") if r["action"] == "token_reset_human"]
    check("R4b good-row audit note byte-identical",
          notes == ["human_override reset; next claim issues fresh"], notes)
    check("R4c pop exactly five token keys, ring/contract survive",
          json.loads(raw_meta("r4-good")) == {"hb": ["2026-01-02 00:00:00"],
                                               "contract": "b"},
          raw_meta("r4-good"))
    for label, raw in BAD_INPUTS:
        author = f"r5-{label}"
        seed(author, raw)
        SRV.reset_token(author, human_override=True)
        mu = [r["note"] for r in events(author) if r["action"] == "meta_unparsable"]
        check(f"R5a meta_unparsable audit with state+fingerprint ({label})",
              len(mu) == 1 and "state=" in mu[0]
              and re.search(r"sha256=[0-9a-f]{16}", mu[0])
              and f"len={len(raw.encode())}" in mu[0], mu)
        if label.startswith("value_shape"):
            bad_key = "probe" if "probe" in raw else "hb"
            check(f"R5b value_shape audit names key= ({label})",
                  f"key={bad_key}" in mu[0], mu)
        trh = [r["note"] for r in events(author) if r["action"] == "token_reset_human"]
        check(f"R5c repair audit note carries state= and key names ({label})",
              len(trh) == 1 and f"state=" in trh[0]
              and "dropped=[" in trh[0] and "preserved=[" in trh[0], trh)
        check(f"R5d no raw payload in any audit ({label})",
              all((raw[:12] not in n) for n in mu + trh) if len(raw) >= 12 else True,
              (raw, mu + trh))

    print("S10 P3 漏斗收口：_get_meta 永不返回非 dict（#1562 §二）")
    for label, raw in BAD_INPUTS + [("empty", "{}"), ("nulltext", "null")]:
        author = f"funnel-{label}"
        seed(author, raw)
        c = conn()
        try:
            got = SRV._get_meta(c, author)
        finally:
            c.close()
        check(f"S10a _get_meta returns dict only ({label})", isinstance(got, dict), repr(got))

    os.unlink(DB)
    print(f"\n{PASS.__len__()} passed, {FAIL.__len__()} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
        sys.exit(1)
    print("✅ META_OBJ TESTS PASSED: state reader (+value_shape 第五态), startup fail-soft, "
          "claim REJECTS corrupt rows (P2=A), ack truthful rejection, reset repair "
          "R1–R5, fingerprint-only audit, P3 dict-total funnel + single decision parse.")


if __name__ == "__main__":
    main()
