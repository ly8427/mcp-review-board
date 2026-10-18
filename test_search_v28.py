#!/usr/bin/env python3
"""v2.8 search read face — acceptance matrix (threads #39 v2 §D5 + #40 A-group).

Numbering map (dsh #422 Q4-c: merged, no duplicate ids):
  M1        = v2 D5-1   trigger-sync falsification (INSERT/UPDATE/DELETE)
  M2        = v2 D5-2   C3 read-only invariant (whole-db before/after compare)
  M3-①..⑧  = v2 D5-3   Chinese hit/zero-hit matrix ①–⑧
  M4        = v2 D5-4   is_self
  M5        = v2 D5-5 + GPT-E(强化)  migration & mid-failure rollback
  (v2 D5-6  = release chain — belongs to the release checklist, not this batch)
  GPT-A     mixed-AND exact result-set assertion
  GPT-B     literal double-quote false-zero reversal
  GPT-C     timeout falsification (covered by M3-⑧)
  GPT-D     index corruption -> startup detects & repairs
  A2        status/search_complete/results_truncated quadrants + total_count
  A5        differential: python reference impl vs FULL result sets
  A12       unicode (emoji / ZWJ / NFC-NFD) pinned behavior
  A8        limit cap / truncation semantics
All tests run on throwaway DBs under a temp dir — the live board db is never
opened (2026-09-27 discipline).
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import server  # noqa: E402


def _use_db(path: Path) -> None:
    server.DB_PATH = path
    server.BOARD_INSTANCE_ID = None


def search(terms, **kw) -> dict:
    return json.loads(server.search_comments(terms, **kw))


def seed(db: Path, threads: list[tuple[str, str]], comments: list[tuple[int, str, str, str]]) -> None:
    """threads: [(title, status)]; comments: [(thread_no, author, body, created_at)]"""
    c = sqlite3.connect(db)
    for i, (title, status) in enumerate(threads, 1):
        c.execute("INSERT INTO threads(title, status, author) VALUES(?,?,?)",
                  (title, status, "seeder"))
    for tid, author, body, ts in comments:
        c.execute(
            "INSERT INTO comments(thread_id, author, body, created_at) VALUES(?,?,?,?)",
            (tid, author, body, ts))
    c.commit()
    c.close()


def db_dump(db: Path) -> str:
    c = sqlite3.connect(db)
    lines = []
    for (name,) in sorted(c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()):
        rows = c.execute(f"SELECT * FROM [{name}] ORDER BY 1").fetchall()
        lines.append(f"[{name}] " + repr(rows))
    c.close()
    return "\n".join(lines)


def main() -> None:
    fails = 0

    def check(label, ok, detail=""):
        nonlocal fails
        print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail and not ok else ""))
        fails += 0 if ok else 1

    ok, reason = server._search_capability()
    if not ok:
        print(f"❌ environment lacks search capability ({reason}) — matrix cannot run")
        sys.exit(1)

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        # ---- shared corpus db ------------------------------------------------
        db = td / "corpus.db"
        _use_db(db)
        server.init_db()
        check("bootstrap: fresh db stamps v7 + search enabled",
              server.SEARCH_STATE["enabled"] is True
              and sqlite3.connect(db).execute(
                  "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "7")
        seed(db,
             [("检索试点", "open"), ("已决历史", "resolved")],
             [
                 (1, "zcode", "游标前进与心跳预算迁移", "2026-10-01 10:00:00"),
                 (1, "claude", "全量计数与超时预算已定死", "2026-10-01 10:01:00"),
                 (1, "dsh", "x a \"b c\" d y", "2026-10-01 10:02:00"),
                 (1, "pi", "100% 完成 aXb 单独一句", "2026-10-01 10:03:00"),
                 (1, "opencode", "游标* a OR b -term col:term 全是字面量", "2026-10-01 10:04:00"),
                 (1, "zcode", "只有长词 全量计数", "2026-10-01 10:05:00"),
                 (2, "claude", "历史帖里的 游标前进 记录", "2026-10-01 11:00:00"),
                 (1, "dsh", "über ÜBER casefold probe", "2026-10-01 10:06:00"),
                 (1, "pi", "café NFC 存储", "2026-10-01 10:07:00"),
                 (1, "zcode", "emoji 😀 和家庭 👨‍👩‍👧‍👦 序列", "2026-10-01 10:08:00"),
             ])

        # ---- M1 trigger-sync falsification -----------------------------------
        r = search(["游标前进"])
        check("M1a insert-visible", r["status"] == "ok" and r["total_count"] >= 1)
        c = sqlite3.connect(db)
        target = c.execute("SELECT id FROM comments WHERE body LIKE '%全量计数与超时预算%'").fetchone()[0]
        c.execute("UPDATE comments SET body='正文已改为新奇词汇' WHERE id=?", (target,))
        c.commit()
        r_old, r_new = search(["超时预算"]), search(["新奇词汇"])
        check("M1b update: old term gone / new term visible",
              r_old["total_count"] == 0 and r_new["total_count"] == 1
              and r_new["status"] == "ok")
        c.execute("DELETE FROM comments WHERE id=?", (target,))
        c.commit()
        c.close()
        check("M1c delete: hit disappears", search(["新奇词汇"])["total_count"] == 0)

        # ---- M2 C3 read-only invariant (whole-db compare) --------------------
        before = db_dump(db)
        search(["游标前进"], author="probe-c3")
        after = db_dump(db)
        check("M2 C3: whole-db bit-identical after search (author passed)",
              before == after)

        # ---- M3 matrix -------------------------------------------------------
        r = search(["游标"])
        check("M3-① two-char single hits (like_scan)",
              r["status"] == "ok" and r["mode"]["route"] == "like_scan"
              and r["total_count"] == 3 and r["search_complete"] is True)
        r = search(["游标", "前进"])
        check("M3-② two-char AND",
              r["mode"]["route"] == "like_scan" and r["total_count"] == 2)
        r_all, r_only_long = search(["心跳", "全量计数"]), None
        check("M3-③ mixed fts+like AND (only-both matches, only-long excluded)",
              r_all["mode"]["route"] == "fts+like" and r_all["total_count"] == 0)
        seed(db, [("补种", "open")],
             [(3, "dsh", "心跳 与 全量计数 同现一句", "2026-10-02 09:00:00"),
              (3, "dsh", "只有 全量计数 的那句", "2026-10-02 09:01:00")])
        r_all = search(["心跳", "全量计数"])
        ids = {h["comment_id"] for h in r_all["results"]}
        only_long = sqlite3.connect(db).execute(
            "SELECT id FROM comments WHERE body='只有 全量计数 的那句'").fetchone()[0]
        check("M3-③b exact set: FTS-narrowed then LIKE-filtered",
              r_all["total_count"] == 1 and ids == {
                  sqlite3.connect(db).execute(
                      "SELECT id FROM comments WHERE body LIKE '%同现一句%'").fetchone()[0]}
              and only_long not in ids)
        r = search(["100%"])
        check("M3-④a % literal (fts route)", r["mode"]["route"] == "fts"
              and r["total_count"] == 1)
        r = search(["0%"])   # len 2 -> LIKE route: % must be escaped, not a wildcard
        check("M3-④a2 short % term via LIKE, literal", r["mode"]["route"] == "like_scan"
              and r["total_count"] == 1)
        r = search(["*"])            # 1-codepoint literal must ride LIKE, never a silent FTS zero (pi #434)
        r2 = search(['"'])
        check("M3-④c 1-2 codepoint literals route to LIKE and hit",
              r["mode"]["route"] == "like_scan" and r["total_count"] >= 1
              and r2["mode"]["route"] == "like_scan" and r2["status"] == "ok")
        r = search(["a_b"])
        r2 = search(["aXb"])
        check("M3-④b _ not a wildcard (a_b misses aXb)",
              r["total_count"] == 0 and r2["total_count"] == 1)
        r = search(["绝对不存在的词组"])
        check("M3-⑤ true negative: 0 hits but interpretable",
              r["status"] == "ok" and r["search_complete"] is True
              and r["total_count"] == 0 and r["mode"]["route"] is not None)
        r = search(["游标*"])
        check("M3-⑥a 游标* treated as literal text", r["total_count"] == 1)
        r = search(["a OR b"])
        check("M3-⑥b 'a OR b' literal (not FTS OR)", r["total_count"] == 1)
        r = search(["col:term"])
        check("M3-⑥c col:term literal", r["total_count"] == 1)
        r = search(['"unterminated'], syntax="fts5")
        check("M3-⑥d invalid fts5 -> status=error, never silent",
              r["status"] == "error" and r["mode"]["route"] == "fts")

        # M3-⑦ disabled capability (fresh db, monkeypatched incapable runtime)
        db_dis = td / "disabled.db"
        _use_db(db_dis)
        saved_cap = server._search_capability
        server._search_capability = lambda: (False, "trigram_unavailable")
        server.init_db()
        r = search(["任意"])
        v = sqlite3.connect(db_dis).execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        audit = sqlite3.connect(db_dis).execute(
            "SELECT note FROM verdict_events WHERE action='search_disabled'").fetchall()
        check("M3-⑦ incapable runtime: core starts, v stays 6, search unavailable",
              r["status"] == "unavailable"
              and r["disabled_reason"] == "trigram_unavailable"
              and r["mode"]["route"] is None and v == "6" and len(audit) == 1)
        server._search_capability = saved_cap
        _use_db(db)
        server.init_db()  # restore SEARCH_STATE for the corpus db

        # M3-⑧ timeout (VM-level interrupt, budget 0) + limit cap
        saved_to = server.SEARCH_TIMEOUT_MS
        server.SEARCH_TIMEOUT_MS = 0
        r = search(["游标"])
        server.SEARCH_TIMEOUT_MS = saved_to
        check("M3-⑧a zero-budget -> status=timeout, complete=false, no results",
              r["status"] == "timeout" and r["search_complete"] is False
              and r["results"] == [] and r["results_truncated"] is None
              and r["mode"]["route"] == "like_scan")
        r = search(["游标"], limit=51)
        check("M3-⑧b limit>50 rejected", r["status"] == "error")
        r = search(["游标"], limit=50)
        check("M3-⑧c limit=50 accepted", r["status"] == "ok")

        # ---- GPT-A mixed AND exact sets --------------------------------------
        r = search(["游标", "全量计数"])
        both_ids = {h["comment_id"] for h in r["results"]}
        cx = sqlite3.connect(db)
        expect = {row[0] for row in cx.execute(
            "SELECT id FROM comments WHERE body LIKE '%游标%' AND body LIKE '%全量计数%'")}
        check("GPT-A AND exact set (all-short)", r["total_count"] == len(expect)
              and both_ids == expect)

        # ---- GPT-B literal double-quote false zero ---------------------------
        r = search(['a "b c" d'])
        check("GPT-B quote-bearing literal hits (double-write fix)",
              r["status"] == "ok" and r["total_count"] == 1)

        # ---- A2 quadrants / A8 truncation ------------------------------------
        db_q = td / "quad.db"
        _use_db(db_q)
        server.init_db()
        seed(db_q, [("bulk", "open")],
             [(1, "zcode", f"bulk 命中行 {i}", f"2026-10-03 10:00:{i:02d}") for i in range(60)])
        r = search(["bulk 命中"], limit=10)
        check("A2 (T,T): total known, truncated reported, returned 10",
              r["status"] == "ok" and r["search_complete"] is True
              and r["total_count"] == 60 and r["returned_count"] == 10
              and r["results_truncated"] is True)
        r = search(["bulk 命中行 7"])   # single match -> complete, not truncated
        check("A2 (T,F): complete, not truncated",
              r["results_truncated"] is False and r["returned_count"] == 1
              and r["total_count"] == 1)
        _use_db(db)
        server.init_db()

        # ---- M4 is_self -------------------------------------------------------
        r = search(["游标前进"], author="zcode")
        self_flags = {h["is_self"] for h in r["results"]}
        authors = {h["author"] for h in r["results"]}
        check("M4 is_self marks own hits, never filters",
              True in self_flags and "zcode" in authors
              and False in self_flags and len(r["results"]) == r["total_count"])

        # ---- A5 differential vs python reference ------------------------------
        corpus_bodies = sqlite3.connect(db).execute(
            "SELECT id, body FROM comments ORDER BY id").fetchall()
        _ASCII_FOLD = {ord(u): ord(l) for u, l in zip(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")}

        def ref_hit(body: str, terms: list[str]) -> bool:
            # Route-honest folding (dsh #422): >=3-char terms ride the FTS path
            # (unicode casefold); shorter terms ride LIKE (ASCII-only folding,
            # folded on BOTH sides like SQLite does); no unicode normalization.
            for t in terms:
                if len(t) >= 3:
                    if t.casefold() not in body.casefold():
                        return False
                else:
                    if t.translate(_ASCII_FOLD) not in body.translate(_ASCII_FOLD):
                        return False
            return True

        diff_queries = [
            ["游标"], ["前进", "预算"], ["心跳", "全量计数"], ["全量计数"],
            ["游标", "迁移"], ["literal"], ["casefold"], ["über"], ["ÜBER"],
            ["😀"], ["👨‍👩‍👧‍👦"], ["café"], ["x a \"b c\" d y"], ["100%"], ["0%"],
            ["绝对不存在"], ["bulk"],
        ]
        mism = []
        for q in diff_queries:
            got = {h["comment_id"] for h in search(q)["results"]}
            expect = {cid for cid, body in corpus_bodies if ref_hit(body, q)}
            if got != expect:
                mism.append((q, sorted(got), sorted(expect)))
        check("A5 differential: full result sets match reference impl", not mism,
              str(mism[:3]))

        # ---- A12 unicode pinned behavior --------------------------------------
        r = search(["😀"])          # len==1 -> LIKE route, literal match
        check("A12a single emoji via LIKE", r["mode"]["route"] == "like_scan"
              and r["total_count"] == 1)
        r = search(["👨‍👩‍👧‍👦"])      # len(str)==7 codepoints -> FTS route
        check("A12b ZWJ family (7 codepoints) routes to FTS and hits",
              r["mode"]["route"] == "fts" and r["total_count"] == 1)
        r = search(["café"])     # NFC query (U+00E9) vs NFC body
        check("A12c NFC hits NFC", r["total_count"] == 1)
        nfd = "café"            # NFD (e + U+0301) — pinned to MISS
        r = search([nfd])
        check("A12d no normalization: NFD query misses NFC body (pinned)",
              r["status"] == "ok" and r["total_count"] == 0
              and r["search_complete"] is True)
        r = search(["über"])
        check("A12e fts path folds unicode case (über -> ÜBER)",
              r["mode"]["route"] == "fts" and r["total_count"] == 1)

        # ---- context & snippet sanity -----------------------------------------
        r = search(["同现一句"])
        hit = r["results"][0]
        cids = [n["comment_id"] for n in hit["context"]]
        cx = sqlite3.connect(db)
        prev_id = cx.execute(
            "SELECT id FROM comments WHERE thread_id=? AND id<? ORDER BY id DESC LIMIT 1",
            (hit["thread_id"], hit["comment_id"])).fetchone()
        next_id = cx.execute(
            "SELECT id FROM comments WHERE thread_id=? AND id>? ORDER BY id ASC LIMIT 1",
            (hit["thread_id"], hit["comment_id"])).fetchone()
        expect_ctx = [x[0] for x in (prev_id, next_id) if x]
        check("context = 2 time-adjacent same-thread neighbors (self excluded)",
              cids == expect_ctx and hit["comment_id"] not in cids)
        long_body = "前奏" + "很长的正文" * 200 + "命中词在尾部"
        c = sqlite3.connect(db)
        c.execute("INSERT INTO comments(thread_id, author, body, created_at)"
                  " VALUES(1,'zcode',?, '2026-10-04 10:00:00')", (long_body,))
        c.commit(); c.close()
        r = search(["命中词在尾部"])
        snip = r["results"][0]["snippet"]
        check("snippet centered on late hit, capped at 200 codepoints",
              "命中词在尾部" in snip and len(snip) <= 202 and snip.startswith("…"))

        # ---- filters -----------------------------------------------------------
        r = search(["游标前进"], status="open")
        r2 = search(["游标前进"], status="resolved")
        check("status filter works", r["total_count"] == 1 and r2["total_count"] == 1
              and r["results"][0]["thread_status"] == "open")
        r = search(["游标前进"], thread_id=2)
        check("thread_id scope", r["total_count"] == 1
              and r["results"][0]["thread_id"] == 2)
        r = search(["游标前进"], before="2026-10-01 10:30:00")
        check("before window", r["total_count"] == 1)

        # ---- GPT-D corruption -> startup detects & repairs ---------------------
        c = sqlite3.connect(db)
        c.execute("DROP TRIGGER comments_fts_au")
        victim = c.execute("SELECT id FROM comments WHERE body LIKE '%emoji%'").fetchone()[0]
        c.execute("UPDATE comments SET body='腐坏后修复的新词组' WHERE id=?", (victim,))
        c.commit(); c.close()
        server.BOARD_INSTANCE_ID = None
        server.init_db()
        r = search(["腐坏后修复的新词组"])
        check("GPT-D stale index detected & repaired on restart (new term findable)",
              r["status"] == "ok" and r["total_count"] == 1)

        # ---- M5 (+GPT-E) migration from v6 & mid-failure rollback --------------
        db6 = td / "fromv6.db"
        _use_db(db6)
        server._search_capability = lambda: (False, "trigram_unavailable")
        server.init_db()                      # v6 shape, no fts objects
        seed(db6, [("老帖", "open")], [(1, "old", "v6 时代的存量评论", "2026-09-01 09:00:00")])
        server._search_capability = saved_cap
        # failure injection: broken trigger DDL as the LAST statement ->
        # apply must roll back ATOMICALLY (no fts table, no marker, stays v6)
        saved_ddl = server._SEARCH_TRIGGER_DDL
        server._SEARCH_TRIGGER_DDL = saved_ddl + ("CREATE TRIGGER broken syntax HERE",)
        server.BOARD_INSTANCE_ID = None
        server.init_db()
        cx = sqlite3.connect(db6)
        v_now = cx.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        has_fts = cx.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='comments_fts'").fetchone()[0]
        has_marker = cx.execute(
            "SELECT COUNT(*) FROM meta WHERE key='search_index_ready'").fetchone()[0]
        rows = cx.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        cx.close()
        check("M5a mid-apply failure rolls back atomically (v stays 6, no fts, no marker)",
              v_now == "6" and has_fts == 0 and has_marker == 0
              and rows == 1 and server.SEARCH_STATE["enabled"] is False)
        server._SEARCH_TRIGGER_DDL = saved_ddl
        server.BOARD_INSTANCE_ID = None
        server.init_db()
        r = search(["存量评论"])
        check("M5b retry applies cleanly: backfill hits pre-v7 rows",
              r["status"] == "ok" and r["total_count"] == 1
              and sqlite3.connect(db6).execute(
                  "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "7")

        # ---- A15 regression smoke: legacy faces intact after migration ---------
        out = server.get_thread(1)
        check("A15 legacy read face unaffected", "老帖" in out and "存量评论" in out)
        poll = server.list_comments_since(since="now", author="smoke")
        check("A15 poll face unaffected", poll.startswith("needs_attention:"))

    print()
    if fails:
        print(f"❌ SEARCH v2.8 ACCEPTANCE: {fails} failure(s)")
        sys.exit(1)
    print("✅ SEARCH v2.8 ACCEPTANCE PASSED: M1-M5 / M3①-⑧ / GPT-A/B/C/D/E / "
          "A2 quadrants / A5 differential / A12 unicode / A8 truncation")


if __name__ == "__main__":
    main()
