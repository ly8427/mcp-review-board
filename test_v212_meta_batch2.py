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
       ("non_object_truthy", "5"),
       ("blob-empty", b"")]  # 空 BLOB 归一化 (#1622 P2): falsy b"" 不塌缩


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
        # batch3(#1552 §五→#1553 §三): corrupt 行的 token_blocked 面不再是空 dict ——
        # 它得到 break_glass 只读家(哨兵键 corrupt、tier=break_glass;坏行字节不可写,
        # reset 是唯一授权覆写,条目由 _meta_state 派生)。
        check(f"T1e corrupt row gets the break_glass home, no raise ({label})",
              set(tb) == {"corrupt"} and tb["corrupt"].get("tier") == "break_glass"
              and "reset_token" in tb["corrupt"].get("exit", ""), tb)
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
            c.commit()  # 审计是写面：先提交再验（修复批 #1545 §三-3）
        finally:
            c.close()
        check(f"T2a _beat/_probe/_ring_append never raise ({label})", True)
        check(f"T2b _ring_append returns False (skip) ({label})", ra is False)
        check(f"T2c bytes byte-identical ({label})", raw_meta(a) == before,
              repr(raw_meta(a)))
        check(f"T2d no hb_read_migrate audit ({label})",
              audits(a, "hb_read_migrate") == [])
        # β 修复批 (#1545 §三-3)：读路径每站一条去重指纹审计 —— _beat 一条 +
        # _ring_append 一条（_probe 与尾部直呼复用同站去重），重复轮询不增长。
        mu = audits(a, "meta_unparsable")
        wheres = sorted(n.split(" ")[0].split("=")[1] for n in mu)
        check(f"T2e per-station deduped meta_unparsable audit ({label})",
              wheres == ["_beat", "_ring_append"]
              and all("state=" in n and "sha256=" in n for n in mu), (wheres, mu))

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
    # #1622 P2 空 BLOB 读面：点名 meta_unparsable/invalid_json、行仍列出；
    # 反真空由 face-good（T6d null）与 T11i/j（好行环照涨）承担
    seed("face-empty", b"")
    lp_e = SRV.list_participants()
    check("T6e empty-BLOB row named meta_unparsable, still listed (#1622 P2)",
          "meta_unparsable" in lp_e and "face-empty" in lp_e, lp_e)
    prof_e = json.loads(SRV.reliability_profile("face-empty"))
    check("T6f empty-BLOB reliability_profile names invalid_json (#1622 P2)",
          prof_e.get("meta_unparsable") == "invalid_json", prof_e)
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
    t7n = audits(a, "meta_unparsable")[0]
    check("T7c 单一写者面：note 带 where=f6_record_exemption + 指纹 sha256=/len=（dsh #1595 ④）",
          t7n.startswith("where=f6_record_exemption state=")
          and "sha256=" in t7n and "len=" in t7n, t7n)
    check("T7d 分歧旧形无残留（skipped (bytes kept) 已废）",
          "skipped (bytes kept)" not in t7n, t7n)

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

    print("T8 legacy shell: P3 收口后的 _get_meta（dict-total，#1562 §二）")
    seed("legacy", "[]")
    c = conn()
    try:
        legacy = SRV._get_meta(c, "legacy")
    finally:
        c.close()
    check("T8a _get_meta never returns a non-dict (P3 收口，取代 batch1 非对象透传)",
          isinstance(legacy, dict) and legacy == {}, legacy)

    print("T11 第五态 value_shape（dsh #1550 §二）：热路径不抛、不写、审计带 key=")
    for label, raw in [("vs-str", '{"hb":"abc"}'),
                       ("vs-obj", '{"hb":[{"a":1}]}'),
                       ("vs-probe", '{"probe":5}'),
                       ("vs-falsy", '{"hb":0}')]:
        a = "v5-" + label
        seed(a, raw)
        before = raw_meta(a)
        c = conn()
        try:
            SRV._beat(c, a, 0)
            SRV._beat(c, a, 1)
            SRV._probe(c, a)
            ra = SRV._ring_append(c, a, "hb")
            ok = SRV._token_ok(c, a, "deadbeef")
            gate = SRV._token_gate(c, a, "deadbeef", thread_id=1)
            ti = SRV._token_invalid_tids(c, a)
            aw = SRV._awaiting_split(c, a)
            fok, ferr, fex = SRV._f6_creator_gate(c, a)
            c.commit()
        finally:
            c.close()
        check(f"T11a hot paths no raise ({label})", True)
        check(f"T11b _ring_append False ({label})", ra is False, ra)
        check(f"T11c bytes byte-identical ({label})", raw_meta(a) == before,
              repr(raw_meta(a)))
        mu = audits(a, "meta_unparsable")
        wheres = sorted(n.split(" ")[0].split("=")[1] for n in mu)
        badkey = "probe" if "probe" in raw else "hb"
        check(f"T11d audit names state=value_shape + key= ({label})",
              wheres == ["_beat", "_ring_append"]
              and all("state=value_shape" in n and f"key={badkey}" in n for n in mu),
              (wheres, mu))
        check(f"T11e token faces reject with named state ({label})",
              ok is False and "meta_unparsable" in (gate or "")
              and "value_shape" in (gate or "") and ti == [], (ok, gate, ti))
        check(f"T11f awaiting_split intact ({label})",
              isinstance(aw, tuple) and len(aw) == 2, aw)
        check(f"T11g F6 fail-closed, no cold-start ({label})",
              fok is False and fex is None and "value_shape" in (ferr or ""),
              (fok, ferr, fex))
        check(f"T11h read faces name value_shape ({label})",
              "value_shape" in SRV.list_participants()
              and json.loads(SRV.reliability_profile(a)).get("meta_unparsable") == "value_shape",
              "")

    print("T11 反真空 + 送达：好行环照涨、审计 0 行；坏行轮询不抛且游标前进")
    seed("v5-good", json.dumps({"token_hash": "a" * 64}))
    before = raw_meta("v5-good")
    c = conn()
    try:
        SRV._beat(c, "v5-good", 0)
        c.commit()
    finally:
        c.close()
    check("T11i good row ring advances exactly +1",
          len(json.loads(raw_meta("v5-good")).get("hb", []))
          == len(json.loads(before).get("hb", [])) + 1,
          (before, raw_meta("v5-good")))
    check("T11j good row: zero meta_unparsable audits",
          audits("v5-good", "meta_unparsable") == [], audits("v5-good"))
    seed("v5-poll", '{"hb":"abc"}')
    c = conn()
    try:
        c.execute("INSERT INTO threads(title, author, created_at)"
                  " VALUES(?,?,datetime('now'))", ("v5-txn", "otherposter"))
        tid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO comments(thread_id, author, body)"
                  " VALUES(?,?,?)", (tid, "otherposter", "ping @v5-poll"))
        maxid = c.execute("SELECT COALESCE(MAX(id), 0) FROM comments").fetchone()[0]
        c.commit()
    finally:
        c.close()
    vbefore = raw_meta("v5-poll")
    poll_err = None
    try:
        SRV.list_comments_since(author="v5-poll", since="1h")
    except BaseException as e:  # noqa: BLE001
        poll_err = e
    c = conn()
    try:
        prow = c.execute("SELECT last_delivered_id FROM participants"
                         " WHERE author='v5-poll'").fetchone()
    finally:
        c.close()
    check("T11k value_shape row: full poll no raise", poll_err is None,
          type(poll_err).__name__ if poll_err else "")
    check("T11l cursor advanced on value_shape row",
          prow is not None and (prow["last_delivered_id"] or 0) >= maxid,
          (prow[:] if prow else None, maxid))
    check("T11m bytes untouched by the poll", raw_meta("v5-poll") == vbefore,
          repr(raw_meta("v5-poll")))

    # --- #99 ②(#1690 §3 登记,#1700 §二 三根针;#1707 §三 补落 RET2/KEEP3 两腿
    # =定稿 #1682 §一「RET1+RET2 两条」/#1686 §四 验收集全落;方向=交付态口径) ---
    # 五串均为 server.py 字节精确字面;含反引号串按源码拼写(#1703 拼写课: 拼错即不绑定)。
    # 交付态计数: RET1=0 / RET2=0 / POS=1 / KEEP3=1 / KEEP4=1 ⇒ 本闸钉住交付态;
    # #28 注记回卷到 master 措辞 ⇒ RET1/POS 翻红 = 回归签名、必须红(#1707 §二.2 改判:
    # 非中间态);② 定性=已落状态的回卷锁,不验证 (甲) 改动。收窄主张见 server.py #1635 块。
    _srv_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "server.py"), encoding="utf-8").read()
    check("② RET1 退役针: roster-wide face 措辞不得回魂(同行含 unauthenticated, so 亦不得回魂)",
          "roster-wide face would let" not in _srv_src)
    check("② POS 正针: errors_by_author 收窄主张在位",
          "errors_by_author returns ONE name per" in _srv_src)
    check("② KEEP4 保留针(源码拼写带反引号): scalar errors 句在位",
          "the scalar `errors` face is out of scope" in _srv_src)
    check("② RET2 退役针(#1682 §一定稿腿、#1707 §三补落): causal-inversion 连接词不得回魂",
          "unauthenticated, so" not in _srv_src)
    check("② KEEP3 保留针(#1686 §四 KEEP3==1、#1707 §三补落): version-gate carrier 句在位",
          "Key name is the version-gate carrier and must not change" in _srv_src)
    print("=" * 60)
    print(f"v2.12.0 batch2: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
