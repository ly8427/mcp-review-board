#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.10 attention signals — threads #77 (token faces) + #79 (my_comment_due)
+ #1635 (user ruling 甲: errors_by_author narrowed to the subject; GPT round-3
P2 via host #1634/#1635, five cells per dsh #1637 / claude #1638 / dsh #1639,
two read-side no-side-effect asserts per dsh #1641 §三).

One file, one fixture (stubbed fastmcp, isolated temp DB — the dsh #496 /
pi #499 pattern). Covers both tickets because they share one return contract
and one version gate (trae #791 增量1: merge, run the gate once).

#79 (A 案, bindings from pi/dsh/claude verdict notes + trae #791):
  A1  blocking subset wakes the owing member; dissolves on their comment.
  A2  unvoted+uncommented ⇒ BOTH awaiting_my_verdict AND my_comment_due
      (families not mutually exclusive; both actionable).
  A3  bump_revision resets the floor: every member MINUS suspended owes again.
  A3b suspended member's my_comment_due stays empty (C1, claude #477).
  A5  votes not all pass ⇒ owing member's attention stays 0 (anti-nag, #53).
  D   audit dedup key includes the silent list (pi #794 §1: a shrink is a new
      fact and must leave a row).
#77 (票面 §1-③ + dsh #774 改判):
  档A tokenless roster member wakes with reason=token_invalid (highest
      priority, above new_comments), disappears after claim+ack; carved out
      of awaiting (mutually exclusive, union = old awaiting set).
  档B wrong-token governance rejection latches a READ-ONLY token_blocked face
      (attention NOT raised by it); audit token_blocked_latched deduped per
      (author, thread, revision); a correct-token call clears the latch.
Contract: header/body keys exist; PROTOCOL_VERSION bumped; attention_contract
      audit row written ONCE per version (idempotent re-init).

Run: .venv/bin/python test_v210_attention_signals.py
Red-first: on pre-v2.10 server every case fails (KeyError / missing reason).
"""
import asyncio
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_fixture as fx

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f" — {detail}" if detail and not cond else ""))


def probe(srv, who):
    class _Req:
        query_params = {"author": who}
    return asyncio.run(srv.attention_probe(_Req())).args[0]


def header(srv, who):
    out = srv.list_comments_since(author=who, since="now")
    first = out.split("\n", 1)[0]
    assert first.startswith("needs_attention: "), out
    return json.loads(first[len("needs_attention: "):])


def due_tids(face):
    return {e["thread_id"]: e["blocking"] for e in face}


def audit_rows(db, action, like="%"):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        return [r[0] for r in c.execute(
            "SELECT note FROM verdict_events WHERE action=? AND note LIKE ? ORDER BY id",
            (action, like))]
    finally:
        c.close()


def deliver(srv, who):
    srv.list_comments_since(author=who, since="now")


def main():
    tmp = tempfile.mkdtemp(prefix="rb-v210-")
    db = os.path.join(tmp, "r.db")
    srv = fx.stub_env(db)
    srv.init_db()

    # ---------- setup ----------
    crew = ("anna", "brin", "cyr", "dana")
    with srv.db() as conn:
        srv._begin_write(conn)
        for n in crew:
            srv._touch(conn, n)
        conn.commit()
    fx.prime_beats(db, list(crew))
    ctx3 = fx.wi("anna", "brin", "cyr")
    ctx4 = fx.wi("anna", "brin", "dana")
    tok = {}
    for n in ("anna", "brin", "dana"):  # cyr stays TOKENLESS for 档A
        out = srv.claim_token(author=n)
        m = re.search(r"[0-9a-f]{32}", out)
        assert m, out
        tok[n] = m.group(0)
        assert srv.ack_token(author=n, token=tok[n]).startswith("Token"), n

    # ---------- #77 档A: tokenless member wakes on token_invalid ----------
    out = srv.create_thread(title="T1", author="anna", quorum=["anna", "brin", "cyr"], context=ctx3)
    assert out.startswith("Created thread #"), out
    t1 = int(out.split("#")[1].split(":")[0])
    srv.post_comment(thread_id=t1, author="brin", body="kickoff")  # undelivered to cyr
    body = probe(srv, "cyr")
    check("#77 A: tokenless roster member wakes reason=token_invalid",
          body.get("attention") == 1 and body.get("reason") == "token_invalid"
          and t1 in body.get("threads", []),
          f"reason={body.get('reason')} threads={body.get('threads')}")
    check("#77 A: token_invalid outranks new_comments (cyr has undelivered)",
          body.get("reason") != "new_comments")
    h = header(srv, "cyr")
    check("#77 ③: header exposes token_invalid family; carved out of awaiting",
          t1 in h.get("token_invalid", []) and t1 not in h.get("awaiting_my_verdict", []),
          f"h={h.get('token_invalid')}/{h.get('awaiting_my_verdict')}")
    with srv.db() as conn:
        old_awaiting = [r["id"] for r in conn.execute(
            "SELECT t.id FROM threads t WHERE t.status='open' AND t.quorum IS NOT NULL"
            " AND ? IN (SELECT value FROM json_each(t.quorum))"
            " AND NOT EXISTS (SELECT 1 FROM verdicts v WHERE v.thread_id=t.id AND v.author=?)",
            ("cyr", "cyr"))]
        active = srv._active_quorum(conn, ["anna", "brin", "cyr"])
        act, blk = srv._awaiting_split(conn, "cyr")
    check("#77 ③: union(awaiting ∪ blocked ∪ token_invalid) = old awaiting set",
          sorted(set(act) | {b["thread_id"] for b in blk} | set(h.get("token_invalid", [])))
          == sorted(set(old_awaiting) & set([t1] if "cyr" in active else []))
          or sorted(set(act) | {b["thread_id"] for b in blk} | set(h.get("token_invalid", [])))
          == sorted(old_awaiting),
          f"act={act} blk={blk} ti={h.get('token_invalid')} old={old_awaiting}")
    # self-heal: claim + ack dissolves 档A
    out = srv.claim_token(author="cyr")
    m = re.search(r"[0-9a-f]{32}", out)
    tok["cyr"] = m.group(0)
    srv.ack_token(author="cyr", token=tok["cyr"])
    deliver(srv, "cyr")
    body = probe(srv, "cyr")
    check("#77 B(可消解): claim+ack turns token_invalid into awaiting_verdict",
          body.get("reason") != "token_invalid" and body.get("reason") == "awaiting_verdict"
          and t1 in body.get("threads", []),
          f"reason={body.get('reason')} threads={body.get('threads')}")

    # ---------- #79 A2: unvoted+uncommented ⇒ both families ----------
    h = header(srv, "cyr")
    check("#79 A2: unvoted+uncommented member in awaiting AND my_comment_due",
          t1 in h.get("awaiting_my_verdict", []) and t1 in due_tids(h.get("my_comment_due", [])),
          f"h={h}")
    check("#79 A2/priority: reason stays awaiting_verdict (floor ranks below)",
          probe(srv, "cyr").get("reason") == "awaiting_verdict")

    # ---------- #79 A5: votes not all pass ⇒ no wake on the comment debt ----------
    out = srv.create_thread(title="T2", author="anna", quorum=["anna", "brin", "dana"], context=ctx4)
    t2 = int(out.split("#")[1].split(":")[0])
    srv.set_verdict(thread_id=t1, verdict="pass", author="brin", token=tok["brin"])
    srv.set_verdict(thread_id=t1, verdict="pass", author="cyr", token=tok["cyr"])
    srv.set_verdict(thread_id=t1, verdict="pass", author="anna", token=tok["anna"])  # creator last
    srv.set_verdict(thread_id=t2, verdict="pass", author="brin", token=tok["brin"])
    deliver(srv, "brin")
    body = probe(srv, "brin")
    # brin already satisfied the T1 floor with the kickoff comment; his live
    # debt is T2 (votes not all pass ⇒ non-blocking, must NOT wake him).
    check("#79 A5: owing comment with votes-not-all-pass ⇒ attention=0 (anti-nag)",
          body.get("attention") == 0
          and due_tids(body.get("my_comment_due", [])).get(t2) is False,
          f"att={body.get('attention')} reason={body.get('reason')} due={body.get('my_comment_due')}")
    check("#79 A5: listed-but-nonblocking entries carry blocking=false",
          all(e.get("blocking") is False for e in body.get("my_comment_due", [])))

    # ---------- #79 D: dedup key includes the silent list ----------
    # all-pass on T1 with floor missing [anna, cyr] (brin's kickoff counts)
    rows = audit_rows(db, "auto_resolve_blocked", "reason=discussion_floor%")
    check("#79 D: all-pass floor block leaves an audit row",
          any(re.search(r"\['anna', 'cyr'\]", r) for r in rows), f"rows={rows}")

    # ---------- #79 A1: blocking subset wakes the owing member, dissolves ----------
    body = probe(srv, "cyr")
    check("#79 A1: all-others-pass ⇒ my_comment_due blocking=true and reason=awaiting_floor_comment",
          body.get("reason") == "awaiting_floor_comment" and t1 in body.get("threads", [])
          and due_tids(body.get("my_comment_due", [])).get(t1) is True,
          f"reason={body.get('reason')} due={body.get('my_comment_due')}")
    srv.post_comment(thread_id=t1, author="cyr", body="+")  # the one-line floor comment
    body = probe(srv, "cyr")
    check("#79 A1(可消解): own comment drops the thread from my_comment_due",
          t1 not in due_tids(body.get("my_comment_due", [])),
          f"due={body.get('my_comment_due')}")
    rows = audit_rows(db, "auto_resolve_blocked", "reason=discussion_floor%")
    check("#79 D: silent-list SHRINK is a new fact (second row, different list)",
          any(re.search(r"\['anna'\]", r) for r in rows), f"rows={rows}")

    # ---------- #79 A6 (GPT PR#6): blocking flips when a pass flips to object ----------
    # anna is the only floor debtor here (brin's kickoff + cyr's "+" counted);
    # her debt IS blocking (brin+cyr both pass). cyr flips pass→object ⇒ closing
    # no longer waits on anna's line ⇒ blocking must drop to false and the floor
    # wake must disappear (attention back to 0, votes not unanimous).
    deliver(srv, "anna")
    body = probe(srv, "anna")
    check("#79 A6 setup: anna's T1 debt IS blocking while others' votes are all pass",
          due_tids(body.get("my_comment_due", [])).get(t1) is True,
          f"due={body.get('my_comment_due')}")
    out = srv.set_verdict(thread_id=t1, verdict="object", author="cyr",
                          token=tok["cyr"], note="A6 probe: flip to object")
    assert out.startswith("Verdict recorded"), out
    body = probe(srv, "anna")
    check("#79 A6: pass→object flip demotes the debt to non-blocking and kills the wake",
          due_tids(body.get("my_comment_due", [])).get(t1) is False
          and body.get("attention") == 0
          and body.get("reason") != "awaiting_floor_comment",
          f"att={body.get('attention')} reason={body.get('reason')} due={body.get('my_comment_due')}")
    srv.set_verdict(thread_id=t1, verdict="pass", author="cyr", token=tok["cyr"])  # restore (convergence flip, free)

    # ---------- #79 A3: bump resets the floor ----------
    srv.bump_revision(thread_id=t1, author="anna", token=tok["anna"])
    h = header(srv, "brin")  # brin commented pre-bump — must owe AGAIN
    check("#79 A3: bump_revision makes every active member owe again (pre-bump comment exempt)",
          t1 in due_tids(h.get("my_comment_due", [])),
          f"due={h.get('my_comment_due')}")

    # ---------- #77 档B: wrong token latches a read-only face ----------
    deliver(srv, "anna")
    body = probe(srv, "anna")
    check("#77 pre-B: anna otherwise idle (creator blocked votes don't wake)",
          body.get("attention") == 0, f"reason={body.get('reason')} threads={body.get('threads')}")
    out = srv.set_verdict(thread_id=t2, verdict="pass", author="anna", token="deadbeef" * 4)
    check("#77 C: wrong token still returns the token error",
          out.startswith("ERROR") and "governance token" in out, out)
    body = probe(srv, "anna")
    tb = body.get("token_blocked", {})
    check("#77 C: token_blocked latch appears, attention NOT raised by it",
          body.get("attention") == 0 and str(t2) in tb and tb[str(t2)].get("tier") == "manual"
          and "since" in tb[str(t2)] and "exit" in tb[str(t2)],
          f"att={body.get('attention')} tb={tb}")
    # #77 档B扩展(batch3, #1552 §五→#1553 §三/dsh #1664 §三①): tier 在「知道成因」的
    # 写入点落盘(可读 hash ∧ 出示失配 ⇒ manual),读面原样取用、不按 since/revision 反推。
    with srv._connect() as conn:
        _mlatch = srv._get_meta(conn, "anna").get("token_blocked") or {}
    check("#77 档B扩展: tier 在写入点落盘(存项自带 tier=manual,非读面硬编码)",
          _mlatch.get(str(t2), {}).get("tier") == "manual", f"stored={_mlatch}")
    n1 = len(audit_rows(db, "token_blocked_latched"))
    srv.set_verdict(thread_id=t2, verdict="pass", author="anna", token="deadbeef" * 4)
    check("#77 D: repeat rejection does not add a second latched row",
          len(audit_rows(db, "token_blocked_latched")) == n1,
          f"rows={audit_rows(db, 'token_blocked_latched')}")
    out = srv.set_verdict(thread_id=t2, verdict="pass", author="anna", token=tok["anna"])
    check("#77 D(可消解): correct token clears the latch (L1.5 may still reject — that is fine)",
          "governance token" not in out and probe(srv, "anna").get("token_blocked", {}) == {},
          f"out={out} tb={probe(srv, 'anna').get('token_blocked')}")
    # GPT PR#6: the latch clear must NOT clobber other token state — possession
    # proof clears only the latch; hash/ack survive for immediate reuse.
    with srv._connect() as conn:
        m = srv._get_meta(conn, "anna")
    check("#77 D(副作用边界): latch clear preserves token_hash/acked",
          bool(m.get("token_hash")) and bool(m.get("token_acked")),
          f"meta-keys={sorted(m)}")

    # ---------- #77 档B扩展(batch3, #1552 §五→#1553 §三): corrupt 行的 break_glass 家 ----------
    # corrupt 行字节不可读也不可写(reset 是唯一授权覆写,P2=A 边界)⇒ 其条目由
    # _meta_state 派生(同 meta_unparsable 面,dsh #1432 §三): 成因即状态本身,哨兵键
    # corrupt 不与 str(tid) 相撞;轮询侧白名单=仅 break_glass 不禁(manual 及未知 tier
    # 默认禁,claude #1666)。face 始终只读:零写、零审计、不动 attention。
    with srv._connect() as conn:
        _anna_raw = conn.execute(
            "SELECT meta FROM participants WHERE author='anna'").fetchone()[0]
    n_tb_latched = len(audit_rows(db, "token_blocked_latched"))
    with srv.db() as conn:
        srv._begin_write(conn)
        conn.execute("UPDATE participants SET meta=? WHERE author='anna'",
                     (b"corrupt-bytes #77B-extension",))
    with srv._connect() as conn:
        tb = srv._token_blocked_face(conn, "anna")
        _raw_after = conn.execute(
            "SELECT meta FROM participants WHERE author='anna'").fetchone()[0]
    _body = probe(srv, "anna")
    check("#77 档B扩展: corrupt 行得 break_glass 只读家(哨兵键 corrupt,tier=break_glass)",
          set(tb) == {"corrupt"} and tb["corrupt"].get("tier") == "break_glass"
          and "reset_token" in tb["corrupt"].get("exit", ""), f"tb={tb}")
    check("#77 档B扩展: face 只读 —— corrupt 字节零改动、零 latch 审计(不置 attention 不计失败)",
          _raw_after == b"corrupt-bytes #77B-extension"
          and len(audit_rows(db, "token_blocked_latched")) == n_tb_latched,
          f"raw={_raw_after!r} audits={audit_rows(db, 'token_blocked_latched')}")
    check("#77 档B扩展(claude #1666 白名单): break_glass 面不含任何 manual 档(脚本字面 tier=manual 不误触)",
          all(e.get("tier") != "manual" for e in tb.values()), f"tb={tb}")
    # P2-1 强测试(GPT 第四轮;host #1730 定稿①;dsh #1727 §一 两格/#1733 §二):
    # 弱式 `!= "token_invalid"` 已废 —— 它对 awaiting_verdict 同样满足,不钉实际值
    # (#853 族可能恒真)。三钉各钉实际值:
    # ① 此 fixture 点 anna 无欠票 ⇒ corrupt 行 wire 真值 = idle ∧ meta_unparsable
    #   诊断在场(host #1726 核实②「可落 awaiting_verdict」是欠票时的分支,见②);
    # ② 欠票格:open quorum 在场 ⇒ corrupt 行真落 awaiting_verdict(GPT 推演上 wire);
    #   变异方向 = 走 carve 支路(META_CORRUPT 行被 tokenless 分支清 actionable)
    #   ⇒ ② 必红 = 反例方向;③ 字节腿:文案含 meta_unparsable ∧ 恰 1 处 Exit:;
    # 语义腿「发一行等人工 reset」不钉 wire(不在返回文案里,dsh #1733 §二 三探针),
    # 落 PR 描述过渡语义句 + 验收报告行(F4 同批下一批首件)。
    check("#77 档B扩展(P2-1 强测试①): corrupt 行唤醒钉实际 wire 值 = reason=idle ∧ meta_unparsable 诊断在(此点无欠票;弱式 !=token_invalid 废弃,不钉实际值=恒真假绿风险)",
          _body.get("reason") == "idle" and _body.get("meta_unparsable") is not None,
          f"reason={_body.get('reason')} mu={_body.get('meta_unparsable')}")
    _out_wake = srv.create_thread(title="T-corrupt-wake", author="brin",
                                  quorum=["brin", "anna"],
                                  context=fx.wi("brin", "anna"))  # L1 wake_infra 申报门(#45)
    assert _out_wake.startswith("Created thread #"), _out_wake
    _body_owed = probe(srv, "anna")
    check("#77 档B扩展(P2-1 强测试②): corrupt 行 ∧ 欠票(open quorum 在场) ⇒ 钉实际 reason=awaiting_verdict ∧ mu 诊断在(host #1726 (i) 措辞上 wire;变异方向=走 carve 支路 ⇒ actionable 被清 ⇒ 本格必红)",
          _body_owed.get("reason") == "awaiting_verdict"
          and _body_owed.get("meta_unparsable") is not None,
          f"reason={_body_owed.get('reason')} mu={_body_owed.get('meta_unparsable')}")
    with srv.db() as conn:  # 零残留:格后即关帖,不污染后续格的线程/等待面
        srv._begin_write(conn)
        conn.execute("UPDATE threads SET status='wontfix' WHERE title='T-corrupt-wake'")
    _mu_text = srv._meta_unparsable_error("meta_corrupt", "v210-pin")
    check("#77 档B扩展(P2-1 强测试②字节腿): 拒绝文案含 meta_unparsable ∧ 恰 1 处 Exit: ∧ 唯一出口点名 reset_token(author, human_override=True)",
          "meta_unparsable" in _mu_text and _mu_text.count("Exit:") == 1
          and "reset_token(author, human_override=True)" in _mu_text,
          f"mu={_mu_text!r}")
    # tier 原样取用两格((甲) 归一 #1702;期望已翻,翻格理由就地写明 = 简报 #1699 ④):
    # 原注记「未知 tier 原样透传(读面不改写,fail-safe 归轮询侧白名单)」已作废 ——
    # 三载体判据按字面 tier=manual 匹配,未知值透传后不匹配 = 不禁 = fail-open
    # (#1691/#1692/#1694 推翻原设计理由)。归一=成员判定(dsh #1694 §一): 已知值
    # manual/break_glass 原样上 wire;未知值/显式 null/缺键一律落迁移默认 manual。
    # 无 tier 存项=前代写入 ⇒ 同默认(前代单写者事实,非反推)。
    with srv.db() as conn:
        srv._begin_write(conn)
        conn.execute("UPDATE participants SET meta=? WHERE author='anna'",
                     (json.dumps({"token_hash": "h", "token_blocked": {
                         "91": {"since": "2026-11-08 00:00:00", "revision": 1},
                         "92": {"since": "2026-11-08 00:00:00", "revision": 2,
                                "tier": "future_tier"},
                         "93": {"since": "2026-11-08 00:00:00", "revision": 3,
                                "tier": None}}}),))
    with srv._connect() as conn:
        tb = srv._token_blocked_face(conn, "anna")
    check("#77 档B扩展: 无 tier 存项迁移默认 manual ∧ 未知 tier 落迁移默认 manual((甲) 归一,期望已翻,理由见上注)",
          tb.get("91", {}).get("tier") == "manual" and tb.get("92", {}).get("tier") == "manual",
          f"tb={tb}")
    check("#99 (jia) acceptance 3: explicit null tier lands on the migration default manual",
          tb.get("93", {}).get("tier") == "manual", f"tb={tb}")
    with srv.db() as conn:
        srv._begin_write(conn)
        conn.execute("UPDATE participants SET meta=? WHERE author='anna'", (_anna_raw,))

    # ---------- #77 ②(batch3, #1552 §五→#1553 §三): _awaiting_split 排除按 state ----------
    # 弃 ti 集合差(同一判据的第二份算术): carve 前置(dict 行 ∧ 无 hash)恰是
    # _token_invalid_tids 早返守卫的取反,且帖循环谓词与 actionable 相同 ⇒ 旧差集恒等
    # 全族。早退判据同站校正 in META_CORRUPT(注释写 Corrupt、判据却是 != 的错位,
    # pi #1659 §二.1③): MISSING 行不再被顺吞 —— 无行成员=无 hash=tokenless,按 state
    # 整族收 carve(DDL meta TEXT NOT NULL ⇒ 今天不可达,本格钉判据面语义,旧码此格红)。
    with srv.db() as conn:
        srv._begin_write(conn)
        srv._touch(conn, "ghost")
    out = srv.create_thread(title="T-ghost", author="anna",
                            quorum=["anna", "ghost"], context=fx.wi("anna", "ghost"))
    assert out.startswith("Created thread #"), out
    t_ghost = int(out.split("#")[1].split(":")[0])
    with srv.db() as conn:
        srv._begin_write(conn)
        conn.execute("DELETE FROM participants WHERE author='ghost'")
    with srv._connect() as conn:
        _act, _blk = srv._awaiting_split(conn, "ghost")
        _ti_ghost = srv._token_invalid_tids(conn, "ghost")
    check("#77 ②(batch3): MISSING 行按 state 收 carve — actionable 整族让位,并集不变",
          _act == [] and _blk == [] and _ti_ghost == [t_ghost],
          f"actionable={_act} blocked={_blk} ti={_ti_ghost}")

    # ---------- #79 A3b/A4 (deferred so dana stays active through 档B) ----------
    # Suspend dana NOW: age her heartbeat past 24h on the TEST db. A suspended
    # member by definition does NOT poll — polling IS presence (heartbeat
    # semantics) — so the C1 exclusion is asserted on the face function
    # directly, WITHOUT any touch-refreshing side effect.
    c = sqlite3.connect(db, timeout=10)
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("UPDATE participants SET last_seen=? WHERE author='dana'", (old,))
    c.commit()
    c.close()
    with srv._connect() as conn:
        d_due = srv._comment_due_face(conn, "dana")
    check("#79 A3b/A4: C1-suspended member never enters my_comment_due",
          t2 not in due_tids(d_due), f"due={d_due}")

    # ---------- contract: version gate + header/body keys ----------
    # v2.11.0 (thread #81) bumped the family further (floor×budget exits); the
    # gate keeps asserting "at least the 2.10 attention contract" — a regression
    # to 2.9.x still trips it.
    # v2.12.0 (thread #88) bumped the family again; parse instead of enumerating
    # prefixes so every future bump keeps the contract test meaningful.
    _pv = tuple(int(x) for x in re.findall(r"\d+", srv.PROTOCOL_VERSION)[:2])
    check("契约: PROTOCOL_VERSION >= 2.10 (v2.12.0 thread #88 再 bump)",
          _pv >= (2, 10), srv.PROTOCOL_VERSION)
    h = header(srv, "brin")
    check("契约: needs_attention carries the three new keys",
          all(k in h for k in ("my_comment_due", "token_invalid", "token_blocked")),
          f"keys={sorted(h)}")
    body = probe(srv, "brin")
    check("契约: /attention body carries my_comment_due + token_blocked",
          "my_comment_due" in body and "token_blocked" in body, f"keys={sorted(body)}")
    # P2-2 契约闸(GPT P2-2 原案;host #1730 定稿②;dsh #1731 §一 G1/G2/G3 三格):
    # 版本号已提前为合并前置(2.12.1)⇒ 写入方向有语义,闸测试必须能翻红。
    # 旧断言只数行数不断版本串 ⇒ bump 与否不可分(dsh #1731 §一 指认的那格)。
    rows = audit_rows(db, "attention_contract")
    check("契约G1(新鲜库): attention_contract 审计恰 1 行 ∧ version=2.12.1 ∧ token_blocked.value=manual|break_glass 段在场 ∧ 合并枚举表键在(once per version)",
          len(rows) == 1 and "version=2.12.1" in rows[0]
          and "token_blocked.value=manual|break_glass" in rows[0]
          and "my_comment_due" in rows[0] and "token_invalid" in rows[0]
          and "awaiting_floor_comment" in rows[0], f"rows={rows}")
    srv.init_db()  # idempotence: re-init must not append
    check("契约G3a(新鲜库 re-init): 不追加第二行(同版本重复初始化不重复写)",
          len(audit_rows(db, "attention_contract")) == 1)
    # G2/G3b(GPT P2-2 原案;dsh #1731 §一 G2/G3 成对): 独立新鲜库承载预置格,
    # 不与上方 fixture 的审计行数耦合 —— 预置后库内恰 1 行(= dsh G2 形状)。
    # stub_env 是单例(二次调用不 reload,直接返回已 import 的 server)⇒ 用
    # DB_PATH 换指承载库,finally 还原;旧行用 server 自己的写入器 _audit 造
    # (字节即逐字节不动基准),新鲜 2.12.1 行清场。红证只能来自这张预置格:
    # 新鲜库 fixture 上 G1/G3a 不担红。
    import pathlib as _pl
    _db2 = os.path.join(tempfile.mkdtemp(prefix="rb-contract-g2-"), "g2.db")
    _db_main = str(srv.DB_PATH)
    try:
        srv.DB_PATH = _pl.Path(_db2)  # 换指(连接工厂每次读模块全局,server.py:1199)
        srv.init_db()
        with srv.db() as conn:
            srv._begin_write(conn)
            conn.execute("INSERT OR REPLACE INTO meta(key, value)"
                         " VALUES('attention_contract_ver', '2.12.0')")
            srv._audit(conn, "system", "attention_contract",
                       note="version=2.12.0 preset(旧行逐字节不动基准)")
            conn.execute("DELETE FROM verdict_events WHERE action='attention_contract'"
                         " AND note LIKE 'version=2.12.1%'")
        srv.init_db()  # gate: acv=2.12.0 != 2.12.1 ⇒ 恰追加一行
        rows2 = audit_rows(_db2, "attention_contract")
        check("契约G2(预置 2.12.0 库): 重初始化恰新增 1 行 version=2.12.1 ∧ token_blocked 段在场,旧行逐字节不动",
              len(rows2) == 2 and rows2[0] == "version=2.12.0 preset(旧行逐字节不动基准)"
              and "version=2.12.1" in rows2[1]
              and "token_blocked.value=manual|break_glass" in rows2[1], f"rows={rows2}")
        srv.init_db()  # G3b: 同版本(2.12.1)再 init ⇒ 仍 2 行
        check("契约G3b(预置库同版本 re-init): 仍 2 行(once per version;G2/G3b 成对防『每次启动都写』假绿)",
              len(audit_rows(_db2, "attention_contract")) == 2)
    finally:
        srv.DB_PATH = _pl.Path(_db_main)  # 还原,零残留

    # ---------- GPT PR#6: negative mutations must trip the static assertions ----------
    import importlib.util as _ilu
    _root = os.path.dirname(os.path.abspath(__file__))
    _spec = _ilu.spec_from_file_location("ch", os.path.join(_root, "configs", "check-hooks.py"))
    ch = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(ch)
    healthy = {n: open(os.path.join(_root, "configs", n), encoding="utf-8").read()
               for n in ch.WATCHERS}
    check("负向: healthy carrier texts pass the static assertions",
          ch.check_watcher_texts(healthy) == 0)
    claude = healthy["claude-polling-prompt.txt"]
    mut = dict(healthy)
    mut["claude-polling-prompt.txt"] = claude.replace("且该项 blocking=true", "且含该帖")
    check("负向: dropping the blocking=true narrowing trips the assertion",
          ch.check_watcher_texts(mut) > 0)
    mut = dict(healthy)
    mut["claude-polling-prompt.txt"] = claude.replace(
        ch._PRIORITY_CHAIN,
        "token_invalid > awaiting_my_verdict > awaiting_floor_comment"
        " > mentions_me > new_comments(v2.10)")  # GPT 找到的漏 verdict_stale 原状
    check("负向: priority chain missing verdict_stale trips the assertion",
          ch.check_watcher_texts(mut) > 0)
    mut = dict(healthy)
    mut["claude-polling-prompt.txt"] = claude.replace(ch._PRIORITY_CHAIN,
                                                      ch._PRIORITY_CHAIN + "\n" + ch._PRIORITY_CHAIN)
    check("负向: duplicated priority chain trips the assertion",
          ch.check_watcher_texts(mut) > 0)
    mut = dict(healthy)
    mut["claude-polling-prompt.txt"] = claude.replace(
        ch._PRIORITY_CHAIN, "优先级 awaiting_my_verdict > mentions_me > new_comments")
    check("负向: old v2.9 priority line (pre-v2.10 form) trips the assertion",
          ch.check_watcher_texts(mut) > 0)

    # ---------- #1635 (user ruling 甲): errors_by_author narrowed to subject ----------
    # GPT 第三轮 P2(host #1634/#1635 转达)+ dsh #1637 六条验收前置 + claude #1638
    # A/B 增补 + dsh #1639 三条实现陷阱 —— 五格全部函数面直调(probe harness),
    # 不进 fastmcp 缺席组(claude B)。夹具纪律:
    #   - dsh #1639 §一: author 校验在 try 之前(空→400),格5/格128 不能由输入触发
    #     ⇒ monkeypatch 最靠前的 heartbeat_and_recompute 注入故障;except 尾是
    #     re-raise ⇒ harness 自吞,否则一次未捕获会中断整份文件(#859 同型)。
    #   - dsh #1639 §二: 全局 map 全文件无复位点 ⇒ 整块 try/finally clear()
    #     (不 importlib.reload,那会废掉夹具);128 格排最后;播种省 128 次注入,
    #     但每格的「记一条」必须真注入,否则写入分支从未被执行。
    #   - claude A: 夹具双作者异值(anna=2/brin=3,真注入非播种),防「其他名缺席」
    #     退化成真空断言。
    #   - dsh #1641 §三: 读点无副作用——读后 map 逐值原样不消费(§三.1)、零异常
    #     作者读后不建条目(§三.2,防 setdefault 0 值同形绿);§三.3(128 格主语用
    #     不在册新串 dana,与播种集字符串不等)由 GPT4 的 not-in assert 自证。
    _orig_hb = srv.heartbeat_and_recompute

    def _hb_raises(conn, who):
        raise RuntimeError("injected fault (#1635 narrowing fixture)")

    def _inject_error(subject, times=1):
        srv.heartbeat_and_recompute = _hb_raises
        try:
            for _ in range(times):
                try:
                    probe(srv, subject)
                except Exception:
                    pass  # except 尾 re-raise;harness 自吞(dsh #1639 §一)
        finally:
            srv.heartbeat_and_recompute = _orig_hb
        return times

    # #29(DEBT, dsh #1675 §三 变体 A): 标量快照取在 try 之前;复原写 finally;
    # 复原断言放块外(finally 内「先断言后复原」易写成半绿)。
    _saved_scalar = srv._PROBE_ERRORS
    _injected_total = 0
    try:
        # 格5(正面格,dsh #1637 §三.2): 真注入两条 ⇒ anna=2;已记录作者恰返
        # {anna: 2}(n 逐值相等)且 brin=3 在场(非真空,claude A)。brin=3 载荷
        # 不得删(dsh #1644 §二.2/#1650 §三/DEBT #30): brin 与 anna 异值才让
        # 「返回整表」与「只返主语」可分 —— 同值(或只注 anna)时两态都等于
        # {anna: 2},该格假绿;删 brin 注入即退化为真空格,后人不得为省一次
        # 注入删此载荷。
        _injected_total += _inject_error("anna", 2)
        _injected_total += _inject_error("brin", 3)
        body = probe(srv, "anna")
        check("#1635 格5(+GPT1): 已记录作者恰返 {anna: 2}(真注入非播种,n 逐值相等)",
              body["errors_by_author"] == {"anna": 2},
              f"got={body.get('errors_by_author')} map={srv._PROBE_ERRORS_BY_AUTHOR}")
        # dsh #1641 §三.1: 读点不消费/不复位 —— anna 读后 map 逐值原样(anna=2
        # ∧ brin=3),防「pop 完再返回」或「先 clear 再构造」的写法(那类写法下
        # 格5/GPT2 也可能绿)。
        check("#1641 §三.1: 读点不消费/不复位(读后 map 逐值与长度原样)",
              srv._PROBE_ERRORS_BY_AUTHOR == {"anna": 2, "brin": 3},
              f"map={srv._PROBE_ERRORS_BY_AUTHOR}")
        # GPT2: 请求 B 不可见 A —— brin 只见自己(相异计数使「anna 缺席」非真空)。
        body = probe(srv, "brin")
        check("#1635 GPT2: 请求 B 只见 B(=3),anna 缺席",
              body["errors_by_author"] == {"brin": 3},
              f"got={body.get('errors_by_author')}")
        # GPT3(dsh #1637 §三.3): 未记录作者 = 在册且零异常的 dana(不是缺 author
        # 的 400 路径);直接取键断言 —— 缺键即 KeyError,不用 .get 兜底(#859 指纹)。
        body = probe(srv, "dana")
        check("#1635 GPT3: 在册零异常作者返 {}(直接取键,非 .get 兜底)",
              body["errors_by_author"] == {},
              f"got={body.get('errors_by_author')}")
        # dsh #1641 §三.2: 读点不建条目 —— 零异常作者 dana 读后不得被 setdefault
        # 建 0 值条目(0 值恰与 {} 同形会使该格因错的原因绿),长度仍 2(anna/brin)。
        check("#1641 §三.2: 读点不建条目(dana 不入 map ∧ 长度不变)",
              "dana" not in srv._PROBE_ERRORS_BY_AUTHOR
              and len(srv._PROBE_ERRORS_BY_AUTHOR) == 2,
              f"map={srv._PROBE_ERRORS_BY_AUTHOR}")
        # GPT4(dsh #1637 §三.4): 128 上限 —— 播种至满(2+126=128,dana 不在册),
        # 真注入 dana 一条 ⇒ 满表拒记;主语读数返 {} ∧ 长度仍 128(不现第 129 键)。
        srv._PROBE_ERRORS_BY_AUTHOR.update({f"x{i}": 1 for i in range(126)})
        # #26(DEBT): 前置两条带名 check —— 前置不成立要留行不崩(裸 assert 抛
        # AssertionError 会越过 finally 吃掉汇总行,#859 同族;仅动这 2 条,
        # 全文件其余裸 assert 不在本项范围)。
        check("#26 GPT4 前置: 播种后 dana 不在册(前置不成立留行,不崩)",
              "dana" not in srv._PROBE_ERRORS_BY_AUTHOR)
        check("#26 GPT4 前置: 播种后 map 恰 128(2+126)",
              len(srv._PROBE_ERRORS_BY_AUTHOR) == 128)
        _injected_total += _inject_error("dana", 1)
        check("#1635 GPT4(前半): 满表拒记新作者(dana 不入 map,无第 129 键)",
              "dana" not in srv._PROBE_ERRORS_BY_AUTHOR
              and len(srv._PROBE_ERRORS_BY_AUTHOR) == 128,
              f"len={len(srv._PROBE_ERRORS_BY_AUTHOR)}")
        body = probe(srv, "dana")
        check("#1635 GPT4(后半): 满表主语读数返 {} ∧ map 仍 128",
              body["errors_by_author"] == {}
              and len(srv._PROBE_ERRORS_BY_AUTHOR) == 128,
              f"got={body.get('errors_by_author')} "
              f"len={len(srv._PROBE_ERRORS_BY_AUTHOR)}")
        # #29 派生量灯(dsh #1675 §三.2): 标量净增恰=注入调用数 —— 派生量非冻结数;
        # 将来若有人清掉标量面,只有这条红(复原灯会静默空转,同变体 C 实测)。
        # 放块尾: 覆盖全部注入(anna 2 + brin 3 + dana 1),不冻结 6。
        check("#29 派生量灯: 标量净增恰=注入调用数(delta==INJECTED,非冻结数)",
              srv._PROBE_ERRORS - _saved_scalar == _injected_total,
              f"delta={srv._PROBE_ERRORS - _saved_scalar} injected={_injected_total}")
    finally:
        srv._PROBE_ERRORS_BY_AUTHOR.clear()  # 模块级全局,finally 还原(dsh #1639 §二)
        srv._PROBE_ERRORS = _saved_scalar   # #29: 标量复原(夹具不留净泄漏,#1675 §三.1)
    # #29 复原灯(dsh #1675 §三.1): 断言放块外 —— 复原行被删时此灯红
    # (变体 B: now=泄漏值/saved=快照);标量面被清时派生量灯红(变体 C)。
    check("#29 标量复原(块进出相等)", srv._PROBE_ERRORS == _saved_scalar,
          f"now={srv._PROBE_ERRORS} saved={_saved_scalar}")

    print(f"\n{'='*60}\nv2.10 attention signals: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
        sys.exit(1)


if __name__ == "__main__":
    main()
