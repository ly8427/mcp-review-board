#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""票6 ①②③ (dsh #759 §1 三方 pass) 验收测试——2.1 细项可机械判载体。

① threads_fts 独立索引面 + 三触发器 + 自检扩展;帖正文命中与评论命中在返回体
   可区分（hit_type 必填枚举, pi #762）；thread 命中 comment_id=null/is_self=false
② author_filter 与 author 正交（author 现语义一字不改）；severity_filter 生效
   时 thread 命中一律不返回
③ cost_tokens 只进读面（search 命中携带 + get_thread 帖面汇总；不进任何门）
隐私面: thread 命中 snippet 经 _search_snippet（凭据形态串照掩——pi #2354 结论）
自检: 删 threads_fts 表 ⇒ 启动自检修复重建（dsh #2355 §2 自检三处同改）

Run: python test_v213_search21.py
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


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else " ❌ ") + name + (f" — {detail}" if detail and not cond else ""))


def ro(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


CRED = "3f9a7c21e5b804d6a2c9f17e8b3d5042"   # 已登记凭据形（掩码判据用）


def main():
    tmp = tempfile.mkdtemp(prefix="rb-s21-")
    db = os.path.join(tmp, "s21.db")
    srv = fx.stub_env(db)
    srv.init_db()

    members = ("anna", "brin", "cyr")
    with srv.db() as conn:
        srv._begin_write(conn)
        for n in members:
            srv._touch(conn, n)
        conn.commit()
    fx.prime_beats(db, list(members))
    # 建两张帖：#1 的 context 埋一段独特文本 + 一段凭据形串；#2 备用
    out = srv.create_thread(
        title="search21-alpha uniquealpha dualfamily-7b2c context thread",
        author="anna", quorum=list(members),
        context=fx.wi(*members) + "\n\n" + (
            "The deployment note references token " + CRED +
            " inside the runbook; uniquectx-marker-9f31 also lives here."))
    assert out.startswith("Created thread"), out
    tid1 = int(re.search(r"#(\d+)", out).group(1))
    out = srv.create_thread(title="search21-beta second thread",
                            author="brin", quorum=list(members),
                            context=fx.wi(*members))
    tid2 = int(re.search(r"#(\d+)", out).group(1))
    for n in members:
        if n not in getattr(main, "TOK", {}):
            m = re.search(r"[0-9a-f]{32}", srv.claim_token(author=n))
            main.TOK = getattr(main, "TOK", {})
            main.TOK[n] = m.group(0)
            srv.ack_token(author=n, token=main.TOK[n])
    tok = main.TOK
    # 评论命中材料 + 一条带 cost_tokens 申报
    r = srv.post_comment(thread_id=tid1, author="brin",
                         body="uniquecomment-4e7a dualfamily-7b2c marker body for the search face")
    assert r.startswith("Posted"), r
    r = srv.post_comment(thread_id=tid1, author="cyr", body="declared cost row",
                         cost_tokens=1234)
    assert r.startswith("Posted"), r

    # ================= ① thread 命中可区分（hit_type） =======================
    print("\n=== ① threads_fts thread 命中 ===")
    res = json.loads(srv.search_comments(terms=["uniquealpha"], hit_type="all"))
    hits = res.get("results", [])
    th_hits = [h for h in hits if h.get("hit_type") == "thread"]
    res_cm = json.loads(srv.search_comments(terms=["uniquecomment"], hit_type="all"))
    cm_hits = res_cm.get("results", [])
    check("①a thread 命中在场且 hit_type=thread", len(th_hits) >= 1,
          json.dumps(hits)[:200])
    check("①b thread 命中 comment_id=null ∧ is_self=false",
          all(h.get("comment_id") is None and h.get("is_self") is False
              for h in th_hits))
    check("①c comment 命中 hit_type=comment",
          all(h.get("hit_type") == "comment" for h in cm_hits) and len(cm_hits) >= 1)
    check("①d thread 命中 snippet 掩码（凭据形串不出完整 32-hex）",
          all(CRED not in h.get("snippet", "") for h in th_hits),
          str([h.get("snippet", "")[:60] for h in th_hits]))
    res_t = json.loads(srv.search_comments(terms=["uniquealpha"], hit_type="thread"))
    check("①e hit_type=thread 过滤 ⇒ 只剩 thread 命中",
          all(h.get("hit_type") == "thread" for h in res_t.get("results", []))
          and len(res_t.get("results", [])) >= 1)
    res_c = json.loads(srv.search_comments(terms=["uniquecomment"], hit_type="comment"))
    check("①f hit_type=comment 过滤 ⇒ 只剩 comment 命中",
          all(h.get("hit_type") == "comment" for h in res_c.get("results", []))
          and len(res_c.get("results", [])) >= 1)
    # ①h 双族同词负例(opencode board #123 #2795 §2:①e 旧夹具单词单族=空过假绿;
    # dsh #2798 §2 第 7 钉=hit_type!="all" ⇒ 结果集 hit_type 全等)
    res_d_t = json.loads(srv.search_comments(terms=["dualfamily-7b2c"], hit_type="thread"))
    res_d_c = json.loads(srv.search_comments(terms=["dualfamily-7b2c"], hit_type="comment"))
    res_d_a = json.loads(srv.search_comments(terms=["dualfamily-7b2c"], hit_type="all"))
    check("①h-1 双族同词 hit_type=thread ⇒ 零评论命中(修复前此处混入评论)",
          len(res_d_t.get("results", [])) >= 1
          and all(h.get("hit_type") == "thread" for h in res_d_t.get("results", [])),
          str([h.get("hit_type") for h in res_d_t.get("results", [])]))
    check("①h-2 双族同词 hit_type=comment ⇒ 零线程命中",
          len(res_d_c.get("results", [])) >= 1
          and all(h.get("hit_type") == "comment" for h in res_d_c.get("results", [])),
          str([h.get("hit_type") for h in res_d_c.get("results", [])]))
    check("①h-3 双族同词 hit_type=all ⇒ 两族都在(夹具本身有效)",
          len(res_d_a.get("results", [])) >= 2
          and {h.get("hit_type") for h in res_d_a.get("results", [])} == {"thread", "comment"},
          str(sorted({h.get("hit_type") for h in res_d_a.get("results", [])})))
    check("①h-4 第 7 钉:非 all ⇒ 结果集 hit_type 全等(命中集合面,dsh #2798 §2)",
          all(h.get("hit_type") == "thread" for h in res_d_t.get("results", []))
          and all(h.get("hit_type") == "comment" for h in res_d_c.get("results", [])))
    check("①h-5 thread 请求 total_count==len(results)(无恒真假截断,dsh #2798 §3)",
          res_d_t.get("total_count") == len(res_d_t.get("results", []))
          and res_d_t.get("results_truncated") is False,
          f"total={res_d_t.get('total_count')} ret={len(res_d_t.get('results', []))}")
    # ①i 超限面(GPT 外审 PR#65): thread 命中>limit 时 total 曾=len(trows)(被 LIMIT
    # 封顶少计)且 truncated=false——修复后线程面单独精确 COUNT,total=真实命中数。
    out3 = srv.create_thread(title="overlimit-3f7d alpha third", author="anna")
    assert out3.startswith("Created"), out3
    out4 = srv.create_thread(title="overlimit-3f7d beta fourth", author="anna")
    assert out4.startswith("Created"), out4
    out5 = srv.create_thread(title="overlimit-3f7d gamma fifth", author="anna")
    assert out5.startswith("Created"), out5
    res_o = json.loads(srv.search_comments(terms=["overlimit-3f7d"], hit_type="thread",
                                           limit=2))
    check("①i-1 线程命中>limit ⇒ total_count=真实命中数 3(非 LIMIT 封顶的 2)",
          res_o.get("total_count") == 3, f"total={res_o.get('total_count')}")
    check("①i-2 超限时 truncated=True(如实告知有截断)",
          res_o.get("results_truncated") is True
          and len(res_o.get("results", [])) == 2,
          f"truncated={res_o.get('results_truncated')} ret={len(res_o.get('results', []))}")
    check("①i-3 超限面结果集仍全 thread(第 7 钉在超限下不破)",
          all(h.get("hit_type") == "thread" for h in res_o.get("results", [])))
    r = srv.search_comments(terms=["x"], hit_type="bogus")
    check("①g 非法枚举显式错", "hit_type must be all|comment|thread" in r, r[:120])

    # ================= ② 两过滤参数 ==========================================
    print("\n=== ② author_filter / severity_filter ===")
    res = json.loads(srv.search_comments(terms=["uniquecomment"], author_filter="brin"))
    check("②a author_filter 过滤 comment 作者",
          all(h.get("author") == "brin" for h in res.get("results", []))
          and len(res.get("results", [])) >= 1)
    res = json.loads(srv.search_comments(terms=["uniquealpha"], severity_filter="major"))
    check("②b severity_filter 生效 ⇒ thread 命中一律不返回",
          all(h.get("hit_type") == "comment" for h in res.get("results", [])))
    # author 现语义一字不改：is_self hint 仍工作
    res = json.loads(srv.search_comments(terms=["uniquecomment"], author="brin"))
    check("②c author 参数现语义（is_self hint）不变",
          all(h.get("is_self") is True for h in res.get("results", [])
              if h.get("author") == "brin") and len(res.get("results", [])) >= 1)

    # ================= ③ cost_tokens 只进读面 ================================
    print("\n=== ③ cost_tokens ===")
    res = json.loads(srv.search_comments(terms=["declared cost"], hit_type="comment"))
    hits = res.get("results", [])
    check("③a 命中携带申报值", any(h.get("cost_tokens") == 1234 for h in hits),
          str([(h.get("cost_tokens")) for h in hits]))
    gt = srv.get_thread(thread_id=tid1)
    check("③b get_thread 帖面汇总", "cost_tokens: 1234 (declared by 1 comment(s))" in gt,
          [ln for ln in gt.splitlines() if "cost_tokens" in ln][:1])
    r = srv.post_comment(thread_id=tid1, author="brin", body="bad cost", cost_tokens=-5)
    check("③c 负值显式错", r.startswith("ERROR: cost_tokens"), r[:100])
    # 不进任何门：该 cost 不改变预算/上限判定（used 不含它）
    with srv.db() as conn:
        used = srv._author_usage(conn, tid1, "cyr")
    check("③d 不进判定（_author_usage 不受影响）", used == 1, str(used))

    # ================= 自检修复（dsh #2355 §2 自检三处同改） ==================
    print("\n=== 自检: threads_fts 缺失 ⇒ 修复重建 ===")
    c = sqlite3.connect(db, timeout=10)
    c.execute("DROP TABLE threads_fts")
    c.execute("DROP TRIGGER threads_fts_ai")
    c.commit()
    c.close()
    srv._search_bootstrap(sqlite3.connect(db, timeout=10))
    c = ro(db)
    objs = c.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name IN"
        " ('threads_fts','threads_fts_ai','threads_fts_ad','threads_fts_au')").fetchone()[0]
    n_t = c.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    n_i = c.execute("SELECT COUNT(*) FROM threads_fts").fetchone()[0]
    c.close()
    check("S1 自检重建 4 对象齐", objs == 4, str(objs))
    check("S2 行数等式覆盖 threads 面", n_t == n_i, f"{n_t} vs {n_i}")
    res = json.loads(srv.search_comments(terms=["uniquealpha"], hit_type="thread"))
    check("S3 修复后 thread 命中恢复", len(res.get("results", [])) >= 1)

    print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
    if FAIL:
        for name in FAIL:
            print("  FAIL:", name)
        sys.exit(1)


if __name__ == "__main__":
    main()
