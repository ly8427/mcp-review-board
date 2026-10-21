#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_fixture.py — v2.9.2 测试夹具（thread #48/#49 + dsh C4 + GPT 复审的回归前置）。

提供：
- wi(*names): 生成 wake_infra 申报块（L1 门要求的 context）。
- prime_beats(db_path, authors): 直接往临时库的 participants.meta 写合成心跳环
  （6 条、相邻间隔 11min、最新=现在）——测试无法真实等待 75 分钟预热，夹具
  如实伪造"存在轮询机制"这一前置条件（F6 门的判定对象）。
- stub_env(db_path): fastmcp/starlette stub 环境 + REVIEWBOARD_DB 指向临时库，
  供直接 import server 跑纯逻辑测试（dsh #496 / pi #499 已验证的形态）。

用法见 test_v292_gates.py 与各 stage 测试头部。
"""
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone

FMT = "%Y-%m-%d %H:%M:%S"


def wi(*names):
    """wake_infra declaration block for the given (a)-form names."""
    return "\n".join(f"- name: {n} | form: (a) | entry: test-fixture | alive: primed"
                     for n in names)


def prime_beats(db_path, authors):
    """Inject a synthetic F6-passing heartbeat ring for each author (direct
    sqlite on the TEST database — fixture precondition fabrication, not a
    server bypass: the gate still runs its real predicate on this ring)."""
    import sqlite3
    now = datetime.now(timezone.utc)
    ring = [(now - timedelta(minutes=11 * (6 - i))).strftime(FMT) for i in range(6)]
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        for a in authors:
            row = conn.execute("SELECT meta FROM participants WHERE author=?", (a,)).fetchone()
            meta = json.loads(row[0]) if row and row[0] else {}
            meta["hb"] = ring
            if row:
                conn.execute("UPDATE participants SET meta=? WHERE author=?",
                             (json.dumps(meta), a))
            else:
                conn.execute(
                    "INSERT INTO participants(author, first_seen, last_seen, last_read, meta)"
                    " VALUES(?,?,?,NULL,?)", (a, ring[0], ring[-1], json.dumps(meta)))
        conn.commit()
    finally:
        conn.close()


def make_ring(n=6, gap_minutes=11):
    now = datetime.now(timezone.utc)
    return [(now - timedelta(minutes=gap_minutes * (n - i))).strftime(FMT) for i in range(n)]


def stub_env(db_path):
    """Stub fastmcp/starlette so `import server` runs pure logic without the
    HTTP stack (the dsh/pi isolated-DB pattern). Registers server module in
    sys.modules under the name 'server'."""
    for waiting in ("fastmcp", "starlette.requests", "starlette.responses"):
        if waiting in sys.modules:
            return sys.modules.get("server")
    fm = types.ModuleType("fastmcp")

    class _Tool:
        def __call__(self, fn):
            return fn

    class _FastMCP:
        def __init__(self, *a, **k):
            pass

        def tool(self, *a, **k):
            if len(a) == 1 and callable(a[0]) and not k:
                return a[0]  # bare @mcp.tool form

            def deco(fn):
                return fn
            return deco

        def custom_route(self, *a, **k):
            def deco(fn):
                return fn
            return deco

        def run(self, *a, **k):
            raise RuntimeError("stubbed: no HTTP in pure-logic tests")

    fm.FastMCP = _FastMCP
    sr = types.ModuleType("starlette.requests")
    sr.Request = object
    sp = types.ModuleType("starlette.responses")

    class _Resp:
        def __init__(self, *a, **k):
            self.args, self.kw = a, k

    sp.HTMLResponse = _Resp
    sp.JSONResponse = _Resp
    star = types.ModuleType("starlette")
    star.requests = sr
    star.responses = sp
    sys.modules.update({"fastmcp": fm, "starlette": star,
                        "starlette.requests": sr, "starlette.responses": sp})
    os.environ["REVIEWBOARD_DB"] = db_path
    import importlib
    import server
    importlib.reload(server)
    return server
