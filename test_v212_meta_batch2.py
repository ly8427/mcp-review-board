#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.12.0 票 B 第二批回归：坏行下的读面 / 门 / 轮询。

约束来源：#1424/#1425/#1429（4 处半守卫 + _get_meta 两条热路径）、
#1432（按路不按站；同一行一次调用只解析一次）、#1433（A5 取舍）。

A5 取舍（本笔实现，明写）：
  - 读路径（_ring_append/_beat/_probe）在坏行上 **跳过不写**，字节原样；
  - 诊断面（list_participants / _profile_numbers / /board / attention）
    点名 state，不静默当成 {}；
  - F6 冷启动对坏行 **fail-closed**（读不到心跳 ≠ 没发生心跳），出口
    reset_token(human_override=True)，**不**引导 normal issue path。

Run: .venv/bin/python test_v212_meta_batch2.py
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
BAD = [("invalid_json", '{"token_hash": '),
       ("non_object_falsy", "[]"),
       ("non_object_truthy", "5")]


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f" — {detail}" if detail and not cond else ""))


def conn():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def seed(author, raw, last_seen="2026-01-01 00:00:00"):
    c = conn()
    try:
        c.execute("DELETE FROM participants WHERE author=?", (author,))
        c.execute("DELETE FROM verdict_events WHERE author=?", (author,))
        c.execute("INSERT INTO participants(author, first_seen, last_seen, last_read, meta)"
                  " VALUES(?,?,?,NULL,?)", (author, last_seen, last_seen, raw))
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


def audits(author, action=None):
    c = conn()
    try:
        if action:
            return [r["note"] for r in c.execute(
                "SELECT note FROM verdict_events WHERE author=? AND action=? ORDER BY id",
                (author, action))]
        return [dict(r) for r in c.execute(
            "SELECT action, note FROM verdict_events WHERE author=? ORDER BY id", (author,))]
    finally:
        c.close()


def main():
    global DB, SRV
    fd, DB = tempfile.mkstemp(prefix="t12-meta-b2-", suffix=".db")
    os.close(fd)
    os.unlink(DB)
    SRV = fx.stub_env(DB)
    SRV.init_db()

    print("T1 token 面：坏行不再 500，且给出点名词（不是 claim 指令）")
    for label, raw in BAD:
        a = "tok-" + label
        seed(a, raw)
        before = raw_meta(a)
        c = conn()
        try:
            ok = SRV._token_ok(c, a, "deadbeef")
            gate = SRV._token_gate(c, a, "deadbeef", thread_id=1)
            ti = SRV._token_invalid_tids(c, a)
            tb = SRV._token_blocked_face(c, a)
            aw = SRV._awaiting_split(c, a)
        finally:
            c.close()
        check(f"T1a _token_ok false, no raise ({label})", ok is False)
        check(f"T1b _token_gate names the state ({label})",
              isinstance(gate, str) and "meta_unparsable" in gate, gate)
        check(f"T1c gate does NOT say claim_token ({label})",
              "claim_token" not in (gate or ""), gate)
        check(f"T1d token_invalid_tids empty for corrupt row ({label})", ti == [])
        check(f"T1e token_blocked face empty, no raise ({label})", tb == {})
        check(f"T1f awaiting_split no raise ({label})", isinstance(aw, tuple) and len(aw) == 2)
        check(f"T1g bytes untouched through all token faces ({label})", raw_meta(a) == before)
        check(f"T1h no token_blocked_latched audit ({label})",
              audits(a, "token_blocked_latched") == [])

    print("T2 轮询/探针：坏行上跳过不写（A5 读路径）")
    for label, raw in BAD:
        a = "beat-" + label
        seed(a, raw)
        before = raw_meta(a)
        c = conn()
        try:
            SRV._beat(c, a, 0)
            SRV._beat(c, a, 1)
            SRV._probe(c, a)
            ra = SRV._ring_append(c, a, "hb")
        finally:
            c.close()
        check(f"T2a _beat/_probe/_ring_append never raise ({label})", True)
        check(f"T2b _ring_append returns False (skip) ({label})", ra is False)
        check(f"T2c bytes byte-identical ({label})", raw_meta(a) == before,
              repr(raw_meta(a)))
        check(f"T2d no hb_read_migrate audit ({label})",
              audits(a, "hb_read_migrate") == [])

    print("T3 F6 门：坏行 fail-closed，不给冷启动快车道")
    for label, raw in BAD:
        a = "f6-" + label
        seed(a, raw)
        before = raw_meta(a)
        c = conn()
        try:
            ok, err, exempt = SRV._f6_creator_gate(c, a)
        finally:
            c.close()
        check(f"T3a gate rejects ({label})", ok is False, (ok, err, exempt))
        check(f"T3b named state in message ({label})",
              "meta_unparsable" in err and "state=" in err, err)
        check(f"T3c no cold-start exemption ({label})", exempt is None)
        check(f"T3d reset exit, not normal issue ({label})",
              "reset_token" in err and "claim_token" not in err, err)
        check(f"T3e bytes untouched by gate ({label})", raw_meta(a) == before)

    print("T4 create_thread：坏 creator 不 500；有/无 quorum 两入口")
    a = "ct_bad"
    seed(a, "[]")
    before = raw_meta(a)
    out = SRV.create_thread(title="free-ok", author=a)
    check("T4a free thread still works (no meta parse on that path)",
          "Created thread" in out, out)
    check("T4b free thread keeps bytes", raw_meta(a) == before)
    out = SRV.create_thread(title="quorum-bad", author=a,
                            quorum=[a, "ct_p1", "ct_p2"],
                            context=fx.wi(a, "ct_p1", "ct_p2"))
    check("T4c quorum create is rejected with named state", "meta_unparsable" in out, out)
    check("T4d quorum create does NOT say claim_token", "claim_token" not in out, out)
    check("T4e bytes untouched", raw_meta(a) == before)
    check("T4f no cold-start audit/flag (fail-closed)",
          audits(a, "f6_coldstart") == [] and "f6_coldstart" not in (raw_meta(a) or ""))

    print("T5 register_meta：坏行点名词，字节原样")
    a = "rm-bad"
    seed(a, "5")
    before = raw_meta(a)
    out = SRV.register_meta(a, "a", entry="x", token="deadbeef")
    check("T5a meta_unparsable in reply", "meta_unparsable" in out, out)
    check("T5b no claim_token substring", "claim_token" not in out, out)
    check("T5c bytes untouched", raw_meta(a) == before)

    print("T6 read faces：不 500，点名 state")
    corrupt = "face-bad"
    seed(corrupt, "[]")
    seed("face-good", json.dumps({"last_wake_error": "old"}))
    lp = SRV.list_participants()
    check("T6a list_participants contains named marker", "meta_unparsable" in lp, lp)
    check("T6b list_participants still lists the row", "face-bad" in lp, lp)
    prof = json.loads(SRV.reliability_profile(corrupt))
    check("T6c reliability_profile names state", prof.get("meta_unparsable") == "non_object", prof)
    prof_good = json.loads(SRV.reliability_profile("face-good"))
    check("T6d good row meta_unparsable is null", prof_good.get("meta_unparsable") is None)
    html = SRV._board_html()
    check("T6e /board renders 200-shaped HTML with corrupt row", "<!doctype html>" in html and "face-bad" in html)
    check("T6f /board carries the marker", "meta_unparsable" in html)

    print("T7 exemption writer: 不可达防线也不重写坏行")
    a = "ex-bad"
    seed(a, "[]")
    before = raw_meta(a)
    c = conn()
    try:
        SRV._f6_record_exemption(c, a, ("f6_coldstart", "test"),
                                 meta_state=SRV.META_NON_OBJECT, pmeta=[])
        c.commit()
    finally:
        c.close()
    check("T7a bytes untouched", raw_meta(a) == before)
    check("T7b audited meta_unparsable", len(audits(a, "meta_unparsable")) == 1)

    print("T9 set_verdict: 坏行点名且不 latch（A2）")
    seed("goodc", json.dumps({"token_hash": "a" * 64}))
    fx.prime_beats(DB, ["goodc"])
    seed("v_bad", "[]")
    out = SRV.create_thread(title="verdict-surface", author="goodc",
                            quorum=["goodc", "v_bad", "v_ok"],
                            context=fx.wi("goodc", "v_bad", "v_ok"))
    tid = int(re.search(r"#(\d+)", out).group(1)) if "Created thread" in out else None
    check("T9a setup thread created", tid is not None, out)
    if tid is not None:
        before = raw_meta("v_bad")
        out = SRV.set_verdict(tid, "pass", "v_bad", token="deadbeef")
        check("T9b named meta_unparsable", "meta_unparsable" in out, out)
        check("T9c no claim_token substring", "claim_token" not in out, out)
        check("T9d bytes untouched", raw_meta("v_bad") == before)
        check("T9e no 档B latch audit", audits("v_bad", "token_blocked_latched") == [])
        check("T9f no token_blocked key written", "token_blocked" not in (raw_meta("v_bad") or ""))

    print("T10 request_form_b: 第 3 个产点也点名词（A1 覆盖 5 工具）")
    a = "fb-bad"
    seed(a, "[]")
    out = SRV.create_thread(title="form-b-surface", author=a)
    tid = int(re.search(r"#(\d+)", out).group(1)) if "Created thread" in out else None
    check("T10a setup free thread created", tid is not None, out)
    if tid is not None:
        before = raw_meta(a)
        out = SRV.request_form_b(tid, a, ["fb-p1"], token="deadbeef")
        check("T10b named meta_unparsable", "meta_unparsable" in out, out)
        check("T10c no claim_token substring (would rebuild the row)", "claim_token" not in out, out)
        check("T10d bytes untouched", raw_meta(a) == before)

    print("T8 legacy shell unchanged (batch1 冻结面)")
    seed("legacy", "[]")
    c = conn()
    try:
        legacy = SRV._get_meta(c, "legacy")
    finally:
        c.close()
    check("T8a _get_meta still returns the raw non-object (S1g preserved)", legacy == [])

    print("=" * 60)
    print(f"v2.12.0 batch2: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
