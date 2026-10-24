#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#51 两处改判条件的回归（dsh #516②③ / pi #518§2§3 / claude #517）。

纯逻辑形态（stub fastmcp/starlette + 隔离临时库）。两个修复各一组：

① 投递门判据 = 创建者实际活跃态（不再用 quorum 席位代理）：
  1a. 暂缓创建者【不在 quorum】（本板常态/#51 形态）：协作者评论+全 pass 后
      自动路放行（原缺陷=永久挂死 creator_delivery_lag），且留
      delivery_gate_skipped 审计。
  1b. 活跃创建者不在 quorum、未轮询 → 门照常挡（不因修复而失效）。
  1c. 同场景创建者轮询收齐 → 自愈放行。

② F6 与入口无关（set_quorum 的 free→quorum 迁移跑同一 _f6_creator_gate）：
  2a. 陈旧 ring（ever_beats，冷启动豁免不适用）→ set_quorum 拒（原缺陷=一步绕过）。
  2b. 新鲜 ring → set_quorum 放行。
  2c. 无 ring + 从未建过 quorum 帖 → 冷启动一次性豁免放行（meta 标志+审计）。
  2d. 直接 create_thread 带 quorum 的既有门保持（陈旧 ring 仍拒）。

Run: .venv/bin/python test_v292_flip_conditions.py
"""
import json
import os
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


def _RO(db):
    class _Ctx:
        def __enter__(self):
            self.c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
            self.c.row_factory = sqlite3.Row
            return self.c
        def __exit__(self, *a):
            self.c.close()
    return _Ctx()


def status_of(db, tid):
    with _RO(db) as conn:
        return conn.execute("SELECT status FROM threads WHERE id=?", (tid,)).fetchone()[0]


def audits(db, action, tid=None):
    with _RO(db) as conn:
        q = "SELECT note FROM verdict_events WHERE action=?"
        args = [action]
        if tid is not None:
            q += " AND thread_id=?"
            args.append(tid)
        return [r["note"] for r in conn.execute(q + " ORDER BY id", args).fetchall()]


def aged_ring(hours):
    """整环拨旧（全部过期；v2.9.3 #53 起 → 近24h无心跳 = 可恢复冷启动豁免）"""
    now = datetime.now(timezone.utc) - timedelta(hours=hours)
    return [(now - timedelta(minutes=11 * (6 - i))).strftime("%Y-%m-%d %H:%M:%S")
            for i in range(6)]


def insufficient_ring():
    """新鲜但节奏不足（6 次×1 分钟）——旧三判据与新 S1/S2/S3 都必拒，
    用于验证门仍在（而不是被冷启动豁免放行）。"""
    return fx.make_ring(n=6, gap_minutes=1)


def set_ring(db, author, ring):
    conn = sqlite3.connect(db, timeout=10)
    row = conn.execute("SELECT meta FROM participants WHERE author=?", (author,)).fetchone()
    meta = json.loads(row[0]) if row and row[0] else {}
    meta["hb"] = ring
    conn.execute("UPDATE participants SET meta=? WHERE author=?",
                 (json.dumps(meta), author))
    conn.commit(); conn.close()


def mk_free(db, srv, author):
    out = srv.create_thread(title="free→quorum", author=author, context="flip-condition fixes")
    assert out.startswith("Created thread #"), out
    return int(out.split("#")[1].split(":")[0])


def main():
    tmp = tempfile.mkdtemp(prefix="rb-flip51-")
    db = os.path.join(tmp, "f.db")
    srv = fx.stub_env(db)
    srv.init_db()

    import re as _re
    def token(n):
        out = srv.claim_token(author=n)
        m = _re.search(r"[0-9a-f]{32}", out)
        assert m, out
        return m.group(0)

    # ---------- ① 投递门：创建者不在 quorum 的三种形态 ----------
    with srv.db() as conn:
        srv._begin_write(conn)
        for n in ("cr", "v1", "v2"):
            srv._touch(conn, n)
        conn.commit()
    fx.prime_beats(db, ["cr", "v1", "v2"])
    tok = {n: token(n) for n in ("cr", "v1", "v2")}

    def mk_out_of_quorum_thread(title):
        out = srv.create_thread(title=title, author="cr",
                                quorum=["v1", "v2"],
                                context=fx.wi("cr", "v1", "v2"))
        assert out.startswith("Created thread #"), out
        return int(out.split("#")[1].split(":")[0])

    def colleagues_complete(tid):
        srv.post_comment(thread_id=tid, author="v1", body="v1 note")
        srv.post_comment(thread_id=tid, author="v2", body="v2 note")
        out = srv.set_verdict(thread_id=tid, verdict="pass", author="v1", token=tok["v1"])
        assert out.startswith("Verdict recorded"), out
        out = srv.set_verdict(thread_id=tid, verdict="pass", author="v2", token=tok["v2"])
        assert out.startswith("Verdict recorded"), out

    # 1a. 暂缓创建者不在 quorum → 放行 + skip 审计（原缺陷场景，翻正）
    tid_a = mk_out_of_quorum_thread("1a suspended creator out of quorum")
    colleagues_complete(tid_a)
    conn = sqlite3.connect(db, timeout=10)
    conn.execute("UPDATE participants SET last_seen=datetime('now','-30 hours') WHERE author='cr'")
    conn.commit(); conn.close()
    srv.list_comments_since(since="1h", author="v1")  # 触发 heartbeat recompute
    check("1a: suspended out-of-quorum creator no longer deadlocks (resolved)",
          status_of(db, tid_a) == "resolved", status_of(db, tid_a))
    skips = audits(db, "delivery_gate_skipped", tid_a)
    check("1a: delivery_gate_skipped audited (门不挂死人留痕)",
          any("cr" in (n or "") for n in skips), f"skips={skips}")

    # 1b. 活跃创建者不在 quorum、未轮询 → 门照常挡
    tid_b = mk_out_of_quorum_thread("1b active creator out of quorum, no poll")
    colleagues_complete(tid_b)
    srv.list_comments_since(since="1h", author="v1")
    blocked = [n for n in audits(db, "auto_resolve_blocked", tid_b)
               if (n or "").startswith("reason=creator_delivery_lag")]
    check("1b: active out-of-quorum creator still gated (delivery lag)",
          status_of(db, tid_b) == "open" and blocked, f"status={status_of(db, tid_b)} blocked={blocked}")

    # 1c. 同帖创建者轮询收齐 → 自愈放行
    srv.list_comments_since(since="1h", author="cr")
    srv.list_comments_since(since="1h", author="cr")
    check("1c: creator poll self-heals → resolved",
          status_of(db, tid_b) == "resolved", status_of(db, tid_b))

    # ---------- ② F6 与入口无关：set_quorum free→quorum 迁移 ----------
    with srv.db() as conn:
        srv._begin_write(conn)
        for n in ("stale", "fresh", "cold"):
            srv._touch(conn, n)
        conn.commit()
    fx.prime_beats(db, ["v1", "v2", "fresh"])
    set_ring(db, "stale", insufficient_ring())   # 新鲜但节奏不足（非冷启动）
    tok_fresh, tok_stale, tok_cold = token("fresh"), token("stale"), token("cold")

    # 2a. 节奏不足的创建者经 set_quorum 造 quorum 帖 → 拒（原缺陷场景）
    t_stale = mk_free(db, srv, "stale")
    out = srv.set_quorum(thread_id=t_stale, author="stale", token=tok_stale,
                         add=["v1", "v2"], wake_infra=fx.wi("stale", "v1", "v2"))
    check("2a: insufficient-rhythm creator blocked on free→quorum transition",
          out.startswith("ERROR") and "F6" in out, out[:120])
    with _RO(db) as conn:
        q = conn.execute("SELECT quorum FROM threads WHERE id=?", (t_stale,)).fetchone()[0]
    check("2a: quorum unchanged after F6 rejection",
          (json.loads(q) if q else []) == [], f"quorum={q}")

    # 2b. 新鲜 ring → 放行
    t_fresh = mk_free(db, srv, "fresh")
    out = srv.set_quorum(thread_id=t_fresh, author="fresh", token=tok_fresh,
                         add=["v1", "v2"], wake_infra=fx.wi("fresh", "v1", "v2"))
    check("2b: fresh-ring creator passes free→quorum transition",
          out.startswith("Quorum updated"), out[:120])

    # 2c. 无 ring + 从未建过 quorum 帖 → 冷启动一次性豁免（meta+审计）
    t_cold = mk_free(db, srv, "cold")
    out = srv.set_quorum(thread_id=t_cold, author="cold", token=tok_cold,
                         add=["v1", "v2"], wake_infra=fx.wi("cold", "v1", "v2"))
    check("2c: cold-start creator exempted once on free→quorum transition",
          out.startswith("Quorum updated"), out[:120])
    cold_audits = audits(db, "f6_coldstart")
    check("2c: cold-start exemption audited",
          len(cold_audits) >= 1, f"audits={len(cold_audits)}")

    # 2d. 既有门保持：直接 create_thread 带 quorum 的节奏不足 ring 仍拒
    set_ring(db, "stale", insufficient_ring())
    out = srv.create_thread(title="direct quorum", author="stale",
                            quorum=["v1", "v2"], context=fx.wi("stale", "v1", "v2"))
    check("2d: direct create_thread quorum still F6-gated",
          out.startswith("ERROR") and "F6" in out, out[:120])

    # 2e. v2.9.3 #53: 有历史 beat 但近 24h 无心跳 → 可恢复冷启动豁免（旧 2a 场景）
    set_ring(db, "stale", aged_ring(72))
    out = srv.create_thread(title="recovered cold start", author="stale",
                            quorum=["v1", "v2"], context=fx.wi("stale", "v1", "v2"))
    check("2e: >24h-silent creator passes via recoverable cold-start (#53)",
          out.startswith("Created thread #"), out[:120])
    with _RO(db) as conn:
        r = conn.execute("SELECT context FROM threads WHERE author='stale'"
                         " ORDER BY id DESC LIMIT 1").fetchone()
    check("2e: cold-start exemption annotated on thread face",
          "f6_coldstart" in ((r[0] if r else "") or ""))

    print("=" * 60)
    print(f"flip-condition fixes: {len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
