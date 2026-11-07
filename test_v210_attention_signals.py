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
    rows = audit_rows(db, "attention_contract")
    check("契约: attention_contract audit written ONCE with the merged enum table",
          len(rows) == 1 and "my_comment_due" in rows[0] and "token_invalid" in rows[0]
          and "awaiting_floor_comment" in rows[0], f"rows={rows}")
    srv.init_db()  # idempotence: re-init must not append
    check("契约: re-init does not append a second attention_contract row",
          len(audit_rows(db, "attention_contract")) == 1)

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

    try:
        # 格5(正面格,dsh #1637 §三.2): 真注入两条 ⇒ anna=2;已记录作者恰返
        # {anna: 2}(n 逐值相等)且 brin=3 在场(非真空,claude A)。
        _inject_error("anna", 2)
        _inject_error("brin", 3)
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
        assert "dana" not in srv._PROBE_ERRORS_BY_AUTHOR
        assert len(srv._PROBE_ERRORS_BY_AUTHOR) == 128
        _inject_error("dana", 1)
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
    finally:
        srv._PROBE_ERRORS_BY_AUTHOR.clear()  # 模块级全局,finally 还原(dsh #1639 §二)

    print(f"\n{'='*60}\nv2.10 attention signals: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
        sys.exit(1)


if __name__ == "__main__":
    main()
