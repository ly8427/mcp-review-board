#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.9.2 gate regressions (threads #48/#49 + GPT re-review items).

Pure-logic form (stubbed fastmcp/starlette, isolated temp DB — the dsh #496 /
pi #499 pattern). Covers, with CONSTRUCTED timestamps and ids:

  1.  #49(5) 同秒夹具: comment → bump → comment 全部同秒，旧码(created_at 坐标)
      必红、新码(水位线) 必绿 —— 地板只算 bump 后那条。
  2.  #49 B: rev1 合法 ledger 放行不了 rev2 的惰性关门（跨修订圈定）。
  3.  #49 A1: 正文中段提及 wake_ledger 的散文评论不遮蔽行首锚点台账；
      行首锚点但无条目 = ledger_unparsable；过滤后无锚点 = no_ledger。
  4.  #49 (4)/(6): EPOCH 后缺 floor_wm= 的事件硬挡(floor_wm_missing)；
      EPOCH 首启取 max(常量, 本库 MAX)，meta 只升不降。
  5.  #48 F6: _beat_ok 三条件（次数/间隔/新鲜）；探针与邀请不喂 ring
      + v2.9.3 票3 (#53): S1 探针、S2/S3 只认交付轮询、通道二、可恢复冷启动。
      （P1 结构：/attention 路径不动 meta.hb）。
  6.  #48 §3/P2: 投递门——创建者欠收他人评论时拒关(reason=creator_delivery_lag)，
      本人评论不计欠账，轮询后自愈。
  7.  #48 ②: author=None + quorum 判死；创建者缺申报即拒。
  8.  #47 ①: nonce 流——request(返回体无 nonce)→confirm(一次性)→set_quorum(b) 放行；
      未确认时 set_quorum(b) 拒。
  9.  GPT 三边界: 非创建者投票不受 L1.5 限制；暂缓成员不计等待；bump 后末票门重算。

Run: .venv/bin/python test_v292_gates.py  (red-first variant: git stash the
server change and re-run case 1 to see it fail on the old coordinate).
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



def _sq_read(db, author):
    import sqlite3
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    r = c.execute("SELECT last_seen FROM participants WHERE author=?", (author,)).fetchone()
    c.close()
    return r[0] if r else None

def main():
    tmp = tempfile.mkdtemp(prefix="rb-v292-")
    db = os.path.join(tmp, "g.db")
    srv = fx.stub_env(db)
    srv.init_db()  # stub 模式不走启动链，schema 显式初始化

    # ---------- setup: register a/b/c with primed beats; tokens ----------
    with srv.db() as conn:
        srv._begin_write(conn)
        for n in ("anna", "brin", "cyr"):
            srv._touch(conn, n)
        conn.commit()
    fx.prime_beats(db, ["anna", "brin", "cyr"])
    ctx = fx.wi("anna", "brin", "cyr")
    tok = {}
    import re as _re
    for n in ("anna", "brin", "cyr"):
        out = srv.claim_token(author=n)
        m = _re.search(r"[0-9a-f]{32}", out)
        assert m, out
        tok[n] = m.group(0)

    # ---------- 7. author=None+quorum 判死 / 创建者缺申报拒 ----------
    out = srv.create_thread(title="x", author=None, quorum=["anna", "brin"], context=ctx)
    check("author=None+quorum rejected", out.startswith("ERROR") and "creator" in out)
    out = srv.create_thread(title="x", author="anna", quorum=["brin", "cyr"], context=None)
    check("creator self-declaration required", out.startswith("ERROR") and "wake_infra" in out)
    out = srv.create_thread(title="T1", author="anna", quorum=["anna", "brin", "cyr"], context=ctx)
    check("compliant creation passes (F6 primed)", out.startswith("Created thread #1"))
    tid = 1

    # ---------- 1. 同秒夹具（构造 created_at 相等） ----------
    ts = "2026-10-21 10:00:00"
    c_pre = post_id(srv, tid, "brin", "pre-bump comment (same second)")
    set_created(db, "comments", c_pre, ts)
    srv.bump_revision(thread_id=tid, author="anna", token=tok["anna"])
    set_created(db, "verdict_events", latest_id(db, "verdict_events"), ts)
    c_post = post_id(srv, tid, "cyr", "post-bump comment (same second)")
    set_created(db, "comments", c_post, ts)
    with _RO(db) as conn:
        wm, ok, why = srv._revision_floor(conn, tid)
    check("watermark parsed from bump note", ok and wm == c_pre, f"wm={wm} c_pre={c_pre} why={why}")
    with _RO(db) as conn:
        floor_authors = {r[0] for r in conn.execute(
            "SELECT DISTINCT author FROM comments WHERE thread_id=? AND id>?", (tid, wm))}
    check("same-second pre-bump comment EXCLUDED from new-rev floor",
          "brin" not in floor_authors and "cyr" in floor_authors, f"got {floor_authors}")

    # ---------- 9. L1.5 边界: 非创建者不受限 / bump 后重算 ----------
    out = srv.set_verdict(thread_id=tid, verdict="pass", author="brin", token=tok["brin"])
    check("non-creator votes freely (L1.5 creator-only)", out.startswith("Verdict recorded"))
    out = srv.set_verdict(thread_id=tid, verdict="pass", author="anna", token=tok["anna"])
    check("creator blocked until others vote (bump cleared, cyr silent)",
          out.startswith("ERROR") and "L1.5" in out)

    # ---------- 3+6. 地板/投递的 blocked 枚举链 ----------
    # 活跃全员评论+投票（brin 已投、cyr 补评论+票），anna 补评论但【未轮询】
    # （欠收 brin/cyr 的 rev2 评论）→ anna 末票触发 recompute → 投递门拒关
    post_id(srv, tid, "cyr", "cyr review rev2")
    post_id(srv, tid, "brin", "brin review rev2")
    post_id(srv, tid, "anna", "anna opening (no poll yet)")
    out = srv.set_verdict(thread_id=tid, verdict="pass", author="cyr", token=tok["cyr"])
    assert out.startswith("Verdict recorded"), out
    out = srv.set_verdict(thread_id=tid, verdict="pass", author="anna", token=tok["anna"])
    check("creator last vote accepted (others voted+commented)", out.startswith("Verdict recorded"))
    with _RO(db) as conn:
        note = latest_blocked(conn, tid)
        st = conn.execute("SELECT status FROM threads WHERE id=?", (tid,)).fetchone()[0]
    check("delivery gate blocks (creator has not received others' comments)",
          note and note.startswith("reason=creator_delivery_lag") and st == "open",
          f"note={note} status={st}")
    # anna 轮询(推水位+喂 beat)。v2.9.3 (#55①, dsh #640)：游标先写、复算后跑 ⇒
    # **这一次**轮询即自愈（旧序里 recompute 读到的是轮询前的旧游标，这一拍会先撞自己
    # 一墙、须再等一拍）。第二次轮询只验幂等（不产生第二条 auto_resolve）。
    srv.list_comments_since(since="1h", author="anna")
    with _RO(db) as conn:
        st = conn.execute("SELECT status FROM threads WHERE id=?", (tid,)).fetchone()[0]
    check("delivery self-heals on the creator's NEXT poll (#55① cursor-before-recompute)",
          st == "resolved", f"status={st}")
    srv.list_comments_since(since="1h", author="anna")   # idempotence probe
    with _RO(db) as conn:
        n_auto = conn.execute(
            "SELECT COUNT(*) FROM verdict_events WHERE thread_id=? AND action='auto_resolve'",
            (tid,)).fetchone()[0]
        lag_rows = conn.execute(
            "SELECT COUNT(*) FROM verdict_events WHERE thread_id=?"
            " AND action='auto_resolve_blocked' AND note LIKE 'reason=creator_delivery_lag%'",
            (tid,)).fetchone()[0]
    check("second poll is idempotent (1 auto_resolve, 1 lag row) — #55③",
          n_auto == 1 and lag_rows == 1, f"auto={n_auto} lag_rows={lag_rows}")

    # ---------- 2. 跨修订 ledger 圈定 ----------
    # 三人帖，brin 拨老暂缓；活跃 anna+cyr 全评论，anna 轮询收齐后投 → rev1 ledger 放行
    tid2 = mk_thread(srv, "anna", ["anna", "brin", "cyr"], ctx, tok)
    post_id(srv, tid2, "brin", "b rev1")
    age(db, "brin", days=2)  # brin 暂缓
    post_id(srv, tid2, "cyr", "cyr rev1")
    post_id(srv, tid2, "anna", "wake_ledger:\n- name: brin | result: timeout")
    srv.list_comments_since(since="1h", author="anna")  # 投递收齐(喂 beat)
    out = srv.set_verdict(thread_id=tid2, verdict="pass", author="cyr", token=tok["cyr"])
    assert out.startswith("Verdict recorded"), out
    out = srv.set_verdict(thread_id=tid2, verdict="pass", author="anna", token=tok["anna"])
    assert out.startswith("Verdict recorded"), out
    with _RO(db) as conn:
        st = conn.execute("SELECT status FROM threads WHERE id=?", (tid2,)).fetchone()[0]
    check("rev1 ledger satisfied rev1 lazy-resolve", st == "resolved", f"status={st}")
    # rev2: bump 后不补台账；brin 的散文【中段】提及不算锚点 → no_ledger 挡
    srv.bump_revision(thread_id=tid2, author="anna", token=tok["anna"])
    post_id(srv, tid2, "cyr", "we discussed wake_ledger mid-text — must not anchor")  # 作者用 cyr:brin 发帖会重新激活其心跳,L1.5 又会等它
    post_id(srv, tid2, "cyr", "cyr rev2")
    post_id(srv, tid2, "anna", "anna rev2 opening (floor + delivery feed)")  # pi #512:ledger 族夹具先喂饱地板
    srv.list_comments_since(since="1h", author="anna")
    srv.list_comments_since(since="1h", author="anna")
    out = srv.set_verdict(thread_id=tid2, verdict="pass", author="cyr", token=tok["cyr"])
    assert out.startswith("Verdict recorded"), out
    out = srv.set_verdict(thread_id=tid2, verdict="pass", author="anna", token=tok["anna"])
    assert out.startswith("Verdict recorded"), out
    with _RO(db) as conn:
        note = latest_blocked(conn, tid2)
    check("A1: mid-text mention does not anchor; no_ledger blocks",
          note and note.startswith("reason=no_ledger"), f"note={note}")
    # 行首锚点但无条目 → ledger_unparsable（anna 补帖后用 cyr 轮询触发 recompute）
    post_id(srv, tid2, "anna", "wake_ledger:\n(prose, no entries)")
    srv.list_comments_since(since="1h", author="cyr")
    with _RO(db) as conn:
        note = latest_blocked(conn, tid2)
    check("anchored-but-empty ledger = ledger_unparsable",
          note and note.startswith("reason=ledger_unparsable"), f"note={note}")
    # 补结构化 → 过（cyr 轮询触发 recompute）
    post_id(srv, tid2, "anna", "wake_ledger:\n- name: brin | result: error")
    srv.list_comments_since(since="1h", author="cyr")
    with _RO(db) as conn:
        st = conn.execute("SELECT status FROM threads WHERE id=?", (tid2,)).fetchone()[0]
    check("structured ledger releases rev2", st == "resolved", f"status={st}")

    # ---------- 4. EPOCH: 后纪元事件缺 token 硬挡 ----------
    with _RO(db) as conn:
        pass
    epoch = with_rw(db, lambda c: srv._floor_wm_epoch(c))
    # 手插一条"未来"的无 token bump 事件（模拟纪元后新事件缺 floor_wm=）
    with_rw(db, lambda c: c.execute(
        "INSERT INTO verdict_events(thread_id, author, action, revision, costed, note, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (tid2, "anna", "bump_revision", 99, 0, "synthetic missing-token bump", "2026-10-21 10:00:00")))
    with _RO(db) as conn:
        wm, ok, why = srv._revision_floor(conn, tid2)
    check("post-epoch missing-token bump → floor_wm_missing (no silent fallback)",
          ok is False and "缺 floor_wm" in why, f"ok={ok} why={why}")
    check("epoch bootstrap took local max", epoch >= 1, f"epoch={epoch}")

    # ---------- 5. F6: _beat_ok 三条件 + 探针不喂 ring ----------
    good = {"hb": fx.make_ring()}
    ok, _ = srv._beat_ok(good)
    check("_beat_ok accepts 6×11min ring", ok)
    bad1 = {"hb": fx.make_ring(n=3)}
    ok, why = srv._beat_ok(bad1)
    check("_beat_ok rejects 3 beats", not ok)
    bad2 = {"hb": fx.make_ring(n=6, gap_minutes=1)}
    ok, why = srv._beat_ok(bad2)
    check("_beat_ok rejects tight gaps", not ok)
    bad3 = {"hb": [ (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(fx.FMT) ]}
    ok, why = srv._beat_ok(bad3)
    check("_beat_ok rejects stale newest", not ok)
    # 探针路径(heartbeat_and_recompute = /attention 共享路径)不写 ring
    before = ring_of(db, "cyr")
    with_rw(db, lambda c: srv.heartbeat_and_recompute(c, "cyr"))
    check("probe path does NOT feed the ring (P1)", ring_of(db, "cyr") == before)

    # ---------- 5b. 票3 (#53): 判据拆开 + 通道二 + 可恢复冷启动 ----------
    now_utc = datetime.now(timezone.utc)
    # S1: 新鲜探针单独满足新鲜度（读交付轮询仍是满环）
    with_probe = {"hb_read": fx.make_ring(), "probe": [now_utc.strftime(fx.FMT)]}
    ok, why = srv._beat_ok(with_probe)
    check("票3 S1: a fresh zero-cost probe satisfies freshness", ok, why)
    # S2/S3: 一旦有 hb_read，旧的 hb（含空转轮询）不再计入次数
    idle_only = {"hb": fx.make_ring(), "hb_read": fx.make_ring(n=3)}
    ok, why = srv._beat_ok(idle_only)
    check("票3 S2/S3: idle polls do not count once hb_read exists",
          (not ok) and "3/6" in why, why)

    # 真实路径: 只有“读到他人未读评论”的轮询喂 hb_read；随后空转不喂
    srv.post_comment(thread_id=tid, author="brin", body="delivery for poller")
    srv.list_comments_since(since="1h", author="cyr")
    n_read1 = len(meta_of(db, "cyr").get("hb_read") or [])
    srv.list_comments_since(since="now", author="cyr")
    srv.list_comments_since(since="now", author="cyr")
    n_read2 = len(meta_of(db, "cyr").get("hb_read") or [])
    check("票3 S2: a real read delivery feeds hb_read", n_read1 >= 1, f"n={n_read1}")
    check("票3 S2: idle polls do not append hb_read", n_read2 == n_read1,
          f"{n_read1} -> {n_read2}")

    # 通道二: 探针 ≥6/24h + 无未读积压 → 免 S2/S3 建帖
    with srv.db() as conn:
        srv._begin_write(conn)
        srv._touch(conn, "probeonly")
        conn.commit()
    with_rw(db, lambda c: srv._set_meta(c, "probeonly", {"probe": fx.make_ring()}))
    with_rw(db, lambda c: c.execute(
        "UPDATE participants SET last_delivered_id=(SELECT COALESCE(MAX(id),0) FROM comments)"
        " WHERE author='probeonly'"))
    p_ctx = fx.wi("probeonly", "anna", "brin")
    out = srv.create_thread(title="channel2", author="probeonly",
                            quorum=["anna", "brin"], context=p_ctx)
    check("票3 通道二: probe≥6/24h + no backlog bypasses S2/S3",
          out.startswith("Created thread #"), out[:160])
    with _RO(db) as conn:
        chan2 = conn.execute(
            "SELECT COUNT(*) FROM verdict_events WHERE action='f6_channel2'").fetchone()[0]
    check("票3 通道二: bypass audited", chan2 >= 1, f"rows={chan2}")

    # 通道二缺口: 有积压 → 拒，且文案是结构化缺口（不含装 watcher/cron 话术）
    with_rw(db, lambda c: c.execute(
        "UPDATE participants SET last_delivered_id=0 WHERE author='probeonly'"))
    out = srv.create_thread(title="channel2-backlog", author="probeonly",
                            quorum=["anna", "brin"], context=p_ctx)
    check("票3 通道二: unread backlog still blocks",
          out.startswith("ERROR") and "未读积压" in out, out[:160])
    check("票3 拒门文案: structured gaps, no watcher/cron install advice",
          ("缺口" in out and "最早可过" in out
           and "watcher(20min)" not in out and "cron(15min)" not in out), out[:200])

    # 冷启动可恢复: 有历史 beat 但近 24h 无心跳 → 放行（不再被 ever_beats 永锁）
    with srv.db() as conn:
        srv._begin_write(conn)
        srv._touch(conn, "returning")
        conn.commit()
    old_ring = [(now_utc - timedelta(hours=48) - timedelta(minutes=11 * (6 - i))).strftime(fx.FMT)
                for i in range(6)]
    with_rw(db, lambda c: srv._set_meta(
        c, "returning", {"hb": old_ring, "f6_coldstart_count": 1,
                          "hb_read": old_ring}))
    out = srv.create_thread(title="cold-return", author="returning",
                            quorum=["anna", "brin"],
                            context=fx.wi("returning", "anna", "brin"))
    check("票3 冷启动: >24h silence is exempt again (recoverable, ever_beats ignored)",
          out.startswith("Created thread #"), out[:160])
    with _RO(db) as conn:
        cctx = conn.execute("SELECT context FROM threads WHERE author='returning'"
                            " ORDER BY id DESC LIMIT 1").fetchone()[0]
    check("票3 冷启动: thread face carries f6_coldstart annotation",
          "f6_coldstart" in (cctx or ""))

    # ---------- 8. nonce 流 ----------
    tid3 = mk_thread(srv, "anna", ["anna", "brin"], ctx, tok)
    out = srv.request_form_b(thread_id=tid3, author="anna", names=["trae"], token=tok["anna"])
    nonce = None
    try:
        with open(os.path.join(os.path.dirname(db), "..", "data", "form_b_nonce.txt"), encoding="utf-8") as fh:
            pass
    except OSError:
        pass
    # stub 环境下 DATA_DIR 指向真实 data/ —— nonce 文件写在仓库 data/ 下,读回
    p = srv.DATA_DIR / "form_b_nonce.txt"
    nonce = p.read_text(encoding="utf-8").split("nonce=")[1].split(" ")[0] if p.exists() else None
    check("request returns NO nonce in tool body", "nonce=" not in out and "pending" in out)
    check("nonce delivered user-side file", bool(nonce))
    bdecl = "- name: anna | form: (a) | entry: t | alive: y\n- name: trae | form: (b) | entry: user | alive: y"
    out = srv.set_quorum(thread_id=tid3, author="anna", token=tok["anna"], add=["trae"], wake_infra=bdecl)
    check("set_quorum (b) rejected before user confirm", out.startswith("ERROR") and "未获用户确认" in out)
    out = srv.confirm_form_b(thread_id=tid3, nonce=nonce)
    check("user confirm consumes nonce", out.startswith("Confirmed"))
    out = srv.set_quorum(thread_id=tid3, author="anna", token=tok["anna"], add=["trae"], wake_infra=bdecl)
    check("set_quorum (b) passes after confirm", out.startswith("Quorum updated"))
    out = srv.confirm_form_b(thread_id=tid3, nonce=nonce)
    check("nonce is one-time", out.startswith("ERROR") and "consumed" in out)

    print(f"\n{'='*60}\nv2.9.2 gates: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
        sys.exit(1)


def _RO(db):
    class _Ctx:
        def __enter__(self):
            self.c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
            self.c.row_factory = sqlite3.Row
            return self.c
        def __exit__(self, *a):
            self.c.close()
    return _Ctx()


def with_rw(db, fn):
    conn = sqlite3.connect(db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        r = fn(conn)
        conn.commit()
        return r
    finally:
        conn.close()


def set_created(db, table, row_id, ts):
    with_rw(db, lambda c: c.execute(f"UPDATE {table} SET created_at=? WHERE id=?", (ts, row_id)))


def latest_id(db, table):
    with _RO(db) as conn:
        return conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0]


def post_id(srv, tid, author, body):
    out = srv.post_comment(thread_id=tid, author=author, body=body)
    assert out.startswith("Posted"), out
    return int(out.split("#")[1].split(" ")[0])


def json_id(x):
    return "0"


def latest_blocked(conn, tid):
    r = conn.execute(
        "SELECT note FROM verdict_events WHERE thread_id=? AND action='auto_resolve_blocked'"
        " ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    return r[0] if r else None

def meta_of(db, author):
    with _RO(db) as conn:
        r = conn.execute("SELECT meta FROM participants WHERE author=?", (author,)).fetchone()
    return json.loads(r[0]) if r and r[0] else {}

def ring_of(db, author):
    with _RO(db) as conn:
        r = conn.execute("SELECT meta FROM participants WHERE author=?", (author,)).fetchone()
    return json.loads(r[0]).get("hb") if r and r[0] else None

def age(db, author, days):
    import sqlite3
    conn = sqlite3.connect(db, timeout=10)
    conn.execute("UPDATE participants SET last_seen=datetime('now', ?) WHERE author=?",
                 (f"-{days} days", author))
    conn.commit()
    conn.close()

def mk_thread(srv, creator, quorum, ctx, tok):
    out = srv.create_thread(title="T", author=creator, quorum=quorum, context=ctx)
    assert out.startswith("Created thread #"), out
    return int(out.split("#")[1].split(":")[0])

def vote_all(srv, db, tid, voters, tok):
    for n in voters:
        out = srv.set_verdict(thread_id=tid, verdict="pass", author=n, token=tok[n])
        assert out.startswith("Verdict recorded") or out.startswith("ERROR"), out


if __name__ == "__main__":
    main()