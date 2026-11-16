#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batch2 2.1 行为夹具套件 — #113 v9 已封验收面的可执行载体（外置回执 #117）。

来源（全部已封口径，本文件只做载体，不重开设计）：
  - 负例 A（verdicts.note 面）  : #2371 §4 / #2372 §4
  - 负例 B（last_wake_error 面）: #2371 §4
  - 负例 C（全 10 面扫面）      : #2373 §3（正例=md5 活锚形 + 40-hex commit id 原样）
  - 负例 D（threads.context 面）: #2378 §2（get_thread context 段 + _board_html）
  - 五件套                      : #2359 §2（命中/满窗掩码形态/跨窗缘无连续 hex/
                                  [5,31] 连续 hex 段仅 fixture 级/自搜照掩）
  - 两 cap 用例 + 分段表        : #2358 §1 / #2359 §1（cap=200 窗 [pos-50,pos+150)、
                                  cap=80 窗 [pos-20,pos+60)；正用例钉 pos≥cap//4 段；
                                  118/28 不写成常量判据）
  - 先掩后截断                  : #2370 §5① / #2371 §2.1（凭据起点≥150、正文>160）
  - 邻界两向                    : opencode #2410 §1 T4（hex 邻界 34-hex 连跑=直通、
                                  非 hex 邻界=照掩）
  - 静态三小项（随套件折叠, dsh #2409 §3.1-3.3, §6 口径）:
      SA1 函数域 OR IGNORE=1 / OR REPLACE=0 ∧ 全文件 DELETE FROM meta=0
          （全文件 OR REPLACE 计数只打印不进断言 — 按函数域断言、全文件=0 是假红）
      SA2 def init_db 恰 1 ∧ 行首匹配调用点恰 1（docstring 字样不计数）
      SA3 DB-read only 两段证据链: verdict_events.thread_id 可空且无外键 +
          _audit 签名 thread_id 缺省 None ∧ boot 调用不传 thread_id

Run: .venv/bin/python test_batch2_mask_faces.py
"""
import ast
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

# ---- fixture constants -----------------------------------------------------
CRED_A = "3f9a7c21e5b804d6a2c9f17e8b3d5042"        # 已登记 32-hex（合成凭据）
CRED_ANC = "9fd2e4aab1c3d5e7f908172635446575"      # 已登记 md5 形活锚（#843 类）
POS40 = "c34b8330db856888849f7e9f50b625c551608320"  # 未登记 40-hex（真 commit id）
UNREG32 = "0123456789abcdef0123456789abcdef"        # 未登记 md5 形（直通正例）
HEX_RUN = re.compile(r"(?<![0-9a-f])[0-9a-f]{5,31}(?![0-9a-f])")


def mk(c):
    """掩码形态：前 4 位 + 28 个星号（等长 32）。"""
    return c[:4] + "*" * 28


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f" — {detail}" if detail and not cond else ""))


def conn():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def bad_runs(s):
    return [m.group(0) for m in HEX_RUN.finditer(s)]


def no_partial_hex(s):
    """掩码后不得出现 5..31 位连续 hex 段（掩码标记=4 hex+28*；40-hex 整段直通）。"""
    return bad_runs(s)


# ---- static source assertions (dsh #2409 §3) --------------------------------

def static_checks():
    src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
    src = open(src_path, encoding="utf-8").read()
    tree = ast.parse(src)

    print("SA 静态三小项（dsh #2409 §3，函数域/行首锚/两段证据链）")
    # SA1 函数域：_cred_history_record 内 OR IGNORE=1、OR REPLACE=0
    fn_src = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_cred_history_record":
            fn_src = ast.get_source_segment(src, node)
    check("SA1a _cred_history_record located", fn_src is not None)
    if fn_src:
        # 「INTO」锚钉在 SQL 语句上——docstring 散文里的同族字样不抬计数
        # （与 SA2 行首锚同病同治：判据本身要能防散文抬高, #2409 §5 ⚠ 同类）。
        check("SA1b fn-domain INSERT OR IGNORE INTO == 1 (statement-anchored)",
              fn_src.count("INSERT OR IGNORE INTO") == 1,
              str(fn_src.count("INSERT OR IGNORE INTO")))
        check("SA1c fn-domain INSERT OR REPLACE == 0", fn_src.count("INSERT OR REPLACE") == 0,
              str(fn_src.count("INSERT OR REPLACE")))
    # SA1 反向陷阱（#2409 §3.1）：全文件 OR REPLACE 不为 0 是事实，只打印不断言
    n_replace_all = src.count("INSERT OR REPLACE")
    print(f"  ℹ informational: whole-file INSERT OR REPLACE = {n_replace_all}"
          f" (asserted by function domain only — whole-file==0 would be a false red)")
    check("SA1d whole-file 'DELETE FROM meta' == 0", src.count("DELETE FROM meta") == 0,
          str(src.count("DELETE FROM meta")))

    # SA2 行首锚：def init_db 恰 1；调用点行首匹配恰 1（:2177 类 docstring 字样不计数）
    n_def = len(re.findall(r"(?m)^\s*def init_db\(", src))
    n_call = len(re.findall(r"(?m)^\s+init_db\(\)\s*$", src))
    check("SA2a def init_db == 1", n_def == 1, str(n_def))
    check("SA2b line-anchored init_db() call == 1", n_call == 1, str(n_call))

    # SA3 两段证据链
    m = re.search(r"CREATE TABLE IF NOT EXISTS verdict_events \(.*?\);", src, re.S)
    check("SA3a verdict_events DDL located", m is not None)
    if m:
        ddl = m.group(0)
        check("SA3b verdict_events.thread_id nullable (no NOT NULL)",
              re.search(r"thread_id\s+INTEGER(?!.*NOT NULL)", ddl) is not None, ddl[:80])
        check("SA3c verdict_events has no FOREIGN KEY", "FOREIGN KEY" not in ddl)
    check("SA3d _audit signature thread_id defaults to None",
          "thread_id: int | None = None" in src)
    boot_calls = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_audit"):
            seg = ast.get_source_segment(src, node) or ""
            if "cred_mask_diag" in seg:
                boot_calls.append(node)
    check("SA3e exactly one cred_mask_diag boot _audit call", len(boot_calls) == 1,
          str(len(boot_calls)))
    for node in boot_calls:
        check("SA3f boot audit passes no thread_id (row stays NULL)",
              all(kw.arg != "thread_id" for kw in node.keywords))

    # 计数锁口径：只打印不进断言（#2381 §3「23/24 不进断言」同族）
    n_defs = len(re.findall(r"(?m)^\s*(?:async )?def ", src))
    print(f"  ℹ informational: whole-file def count = {n_defs}"
          f" (printed only, per the count-lock wording)")
    print()


# ---- behavioral fixtures ----------------------------------------------------

def main():
    global DB, SRV
    fd, DB = tempfile.mkstemp(prefix="t-batch2-mask-", suffix=".db")
    os.close(fd)
    os.unlink(DB)
    SRV = fx.stub_env(DB)
    SRV.init_db()

    static_checks()

    # ---- registration: CURRENT∪HISTORY registry members (模拟活板已登记凭据)
    c = conn()
    try:
        for author, cred in (("fxcredcur", CRED_A), ("fxcredhist", CRED_ANC)):
            h = __import__("hashlib").sha256(cred.encode()).hexdigest()
            c.execute("INSERT INTO participants(author, first_seen, last_seen, last_read, meta)"
                      " VALUES(?,?,?,NULL,?)",
                      (author, "2026-01-01 00:00:00", "2026-01-01 00:00:00",
                       json.dumps({"token_hash": h})))
        c.commit()
    finally:
        c.close()
    reg, diag = SRV._load_credential_registry(conn())
    check("R0 registry holds both seeded creds (sha256 口径)",
          __import__("hashlib").sha256(CRED_A.encode()).hexdigest() in reg
          and __import__("hashlib").sha256(CRED_ANC.encode()).hexdigest() in reg)
    check("R0b registry diag empty on clean rows", diag == [], str(diag))

    # ---- adjacency boundaries (opencode #2410 §1 T4, 两向都进夹具)
    print("ADJ 邻界两向")
    check("ADJ1 hex-adjacent 34-hex run passes through (boundary excludes)",
          SRV._mask_credential_text("0" + CRED_A + "1", reg) == "0" + CRED_A + "1")
    check("ADJ2 non-hex adjacent ('z'+cred) is masked",
          SRV._mask_credential_text("z" + CRED_A, reg) == "z" + mk(CRED_A))
    check("ADJ3 empty text guard", SRV._mask_credential_text("", reg) == "")
    check("ADJ4 empty registry guard",
          SRV._mask_credential_text(CRED_A, frozenset()) == CRED_A)
    print()

    # ---- threads / comments / verdict (fixtures 的写入面)
    # quorum 帖 ⇒ context 必带 wake_infra 申报（v2.9 硬门;创建者并入必检名单 v2.9.2）
    r1 = SRV.create_thread(title=f"fx quorum {CRED_ANC}",
                           context=fx.wi("fxcreator", "fxrev", "fxghost"),
                           author="fxcreator",
                           quorum=["fxrev", "fxghost"], per_author_budget=20)
    t1 = int(re.search(r"Created thread #(\d+)", r1).group(1))
    r2 = SRV.create_thread(title=f"fx ctx {POS40}",
                           context=f"背景 {CRED_ANC} / commit {POS40}",
                           author="fxcreator")
    t2 = int(re.search(r"Created thread #(\d+)", r2).group(1))

    tok = re.search(r"\n  ([0-9a-f]{32})\n", SRV.claim_token("fxrev")).group(1)
    NOTE_A = f"object: leaked {CRED_A} in body; " + "pad " * 45   # >160 字符
    check("V1 set_verdict(object) accepted with token",
          "ERROR" not in SRV.set_verdict(thread_id=t1, verdict="object", author="fxrev",
                                         note=NOTE_A, token=tok),
          SRV.set_verdict(thread_id=t1, verdict="object", author="fxrev",
                          note=NOTE_A, token=tok))

    SRV.post_comment(thread_id=t1, author="fxseed",
                     body=f"body leak {CRED_A} and anchor {CRED_ANC} ok")
    SRV.post_comment(thread_id=t1, author="fxseed", body="file face", file=CRED_ANC)
    SRV.post_comment(thread_id=t2, author="fxseed",
                     body="w" * 150 + CRED_A + " after")            # 起点 150、总长>160
    SRV.post_comment(thread_id=t2, author="fxseed", body=f"see commit {POS40} ok")
    SRV.post_comment(thread_id=t2, author="fxseed", body=f"md5 {UNREG32} raw")
    SRV.post_comment(thread_id=t2, author="fxseed",
                     body="needle at " + "q" * 30 + CRED_A + " tail")  # search 面级载体

    # 负例 B 载体：wake-error 带凭据（直接落 meta，模拟 watcher 失败回写）
    c = conn()
    try:
        c.execute("INSERT INTO participants(author, first_seen, last_seen, last_read, meta)"
                  " VALUES(?,?,?,NULL,?)",
                  ("fxwake", "2026-01-01 00:00:00", "2026-01-01 00:00:00",
                   json.dumps({"last_wake_error": f"watcher died on {CRED_A}"})))
        c.commit()
    finally:
        c.close()
    r_meta = SRV.register_meta(author="fxrev", contract="a",
                               entry=f"entry leaks {CRED_ANC}", token=tok)

    print("NA 负例 A：凭据入 verdicts.note ⇒ get_thread note 段无完整 32-hex；"
          "160 截断与 [n=…] 计数不变")
    g1 = SRV.get_thread(t1)
    check("NA1 no complete registered cred in note face", CRED_A not in g1)
    check("NA2 masked form visible in note face", mk(CRED_A) in g1)
    n_expected = len(" ".join(NOTE_A.split()))                    # 掩码等长 ⇒ 计数不变
    check("NA3 [n=…] count unchanged (length-preserving mask)",
          f"[n={n_expected};" in g1)
    print()

    print("ND 负例 D：凭据入 threads.context ⇒ get_thread context 段与 _board_html 均无完整 32-hex")
    g2 = SRV.get_thread(t2)
    board = SRV._board_html()
    check("ND1 get_thread context masked", CRED_ANC not in g2 and mk(CRED_ANC) in g2)
    check("ND2 _board_html masked (context/title/body faces)",
          CRED_ANC not in board and CRED_A not in board)
    check("ND3 positive: 40-hex commit id intact in board", POS40 in board)
    print()

    print("NB 负例 B：凭据入 participants.meta.last_wake_error ⇒ list_participants / "
          "reliability_profile 两处都不出现完整串")
    lp = SRV.list_participants()
    rp = SRV.reliability_profile("fxwake")
    check("NB1 list_participants masks wake-error", CRED_A not in lp and mk(CRED_A) in lp)
    check("NB2 reliability_profile masks wake-error", CRED_A not in rp and mk(CRED_A) in rp)
    print()

    print("NC 负例 C：全 10 面扫面 — 均无完整已登记 32-hex；正例原样；"
          "create_thread/register_meta 回执面照掩")
    sweep = {
        "list_threads": SRV.list_threads(),
        "get_thread(t1)": g1,
        "get_thread(t2)": g2,
        "list_comments_since": SRV.list_comments_since(since="1h", author="fxseed"),
        "search(all)": SRV.search_comments(terms=["needle"]),
        "search(thread)": SRV.search_comments(terms=["needle"], thread_id=t1),
        "search(commit)": SRV.search_comments(terms=["commit"]),
        "_board_html": board,
        "list_participants": lp,
        "reliability_profile": rp,
        "create_thread receipt t1": r1,
        "create_thread receipt t2": r2,
        "register_meta receipt": r_meta,
    }
    for label, text in sweep.items():
        check(f"NC {label}: no complete registered cred",
              CRED_A not in text and CRED_ANC not in text)
    check("NC receipts: title/entry echo masked",
          mk(CRED_ANC) in r1 and mk(CRED_ANC) in r_meta, r_meta[:120])
    check("NC positives: commit id intact (get_thread/search/board/preview)",
          all(POS40 in sweep[k] for k in
              ("get_thread(t2)", "search(commit)", "_board_html", "list_comments_since")))
    check("NC positives: unregistered md5-shape passes through",
          UNREG32 in sweep["list_comments_since"] and UNREG32 in sweep["get_thread(t2)"])
    print()

    print("NP 先掩后截断（list_comments_since preview）：凭据起点≥150、正文>160")
    prev = sweep["list_comments_since"]
    line = next((ln for ln in prev.splitlines() if mk(CRED_A)[:4] + "*" in ln), "")
    check("NP1 no complete cred in preview", CRED_A not in prev)
    check("NP2 preview line found with mask prefix", line != "")
    check("NP3 no 5..31 hex fragment on that line (tail-cut leaves no残段)",
          line == "" or no_partial_hex(line) == [], str(no_partial_hex(line)))
    print()

    print("S5 五件套 × 两 cap（_search_snippet：cap=200 窗[pos−50,+)、cap=80 窗[pos−20,+);"
          "正用例钉 pos≥cap//4 段）")
    body200 = "x" * 60 + "needle" + "y" * 44 + CRED_A + " tail"       # needle pos=60≥50
    body80 = "x" * 40 + "needle" + "y" * 14 + CRED_A + " end"         # needle pos=40≥20
    body_edge = "x" * 20 + "needle" + "y" * 34 + CRED_A + "z" * 50    # cap80 窗尾切掩码
    for cap, body, tag in ((200, body200, "cap200"), (80, body80, "cap80")):
        snip = SRV._search_snippet(body, ["needle"], cap, reg)
        check(f"S5 {tag} ① hit present", "needle" in snip)
        check(f"S5 {tag} ② masked form in window", mk(CRED_A) in snip)
        check(f"S5 {tag} ③ no complete cred", CRED_A not in snip)
        check(f"S5 {tag} ④⑤ no 5..31 hex run (跨窗缘/[5,31] fixture 级)",
              no_partial_hex(snip) == [], str(no_partial_hex(snip)))
    edge = SRV._search_snippet(body_edge, ["needle"], 80, reg)        # 凭据跨窗尾
    check("S5 edge: window-tail slice of masked cred leaks no plaintext fragment",
          CRED_A not in edge and no_partial_hex(edge) == [] and CRED_A[:4] in edge,
          str(no_partial_hex(edge)))
    selfsnip = SRV._search_snippet(body200, [CRED_A], 200, reg)       # 自搜照掩
    check("S5 self-search: pos on original text, slice on masked text",
          CRED_A not in selfsnip and mk(CRED_A) in selfsnip)
    check("S5 self-search: center preserved (needle=cred position)",
          mk(CRED_A) in selfsnip[:100])
    # search_comments 面级 snippet（走真实 face 装载路径）
    sres = json.loads(sweep["search(all)"]).get("results", [])
    s_snips = []
    for r in sres:
        s_snips.append(r.get("snippet", ""))
        s_snips += [ctx.get("snippet", "") for ctx in r.get("context", [])]
    check("S5 face-level: search snippets carry no complete cred",
          all(CRED_A not in s and CRED_ANC not in s for s in s_snips), str(s_snips)[:160])
    check("S5 face-level: snippet shows masked form",
          any(mk(CRED_A) in s for s in s_snips))
    print()

    print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
    if FAIL:
        for name in FAIL:
            print("  FAIL:", name)
        sys.exit(1)


if __name__ == "__main__":
    main()
