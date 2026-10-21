#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rb-notes.py — 判定理由全文读出（thread #45 rev2 配套，修"理由困在 160 字截断里"）。

server v2.7 读面把 verdict note 压平截断到 160 字（协议条款），但**全文存在库里**
（server.py 自述 "the full text lives in storage"）。本工具直取全文，不碰运行中的
服务器、不推进任何游标/心跳（C3 同款纪律）。

v2.9.2（#47 ④ H3 同 commit 条件）：零绝对路径——DB 路径取 env RB_DB，默认按本文件
位置推导（../data/reviewboard.db）；只读连接 ro 失败（WAL 无 -shm 写权限）→
immutable=1 回退，**回退是降级不是等价**：immutable 可能读到写入窗口期的陈旧快照，
工具会如实标注。

用法: python rb-notes.py <thread_id>
"""
import os
import sqlite3
import sys

DB = os.environ.get(
    "RB_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "reviewboard.db"),
)
STALE_NOTE = ("# 注意: 本读数来自 immutable=1 降级连接——服务器写入窗口期可能陈旧"
              "（dsh R6/#479;重跑一次通常即新鲜）\n")


def _connect():
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        conn.execute("PRAGMA query_only=1")
        conn.execute("SELECT 1 FROM threads LIMIT 1")
        return conn, ""
    except sqlite3.Error:
        pass
    conn = sqlite3.connect(f"file:{DB}?immutable=1", uri=True, timeout=5)
    conn.execute("PRAGMA query_only=1")
    conn.execute("SELECT 1 FROM threads LIMIT 1")
    return conn, STALE_NOTE


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    tid = int(sys.argv[1])
    conn, stale = _connect()
    try:
        rev_wanted = int(sys.argv[2]) if len(sys.argv) > 2 else None
        rows = conn.execute(
            "SELECT author, verdict, note, updated_at FROM verdicts "
            "WHERE thread_id=? ORDER BY author", (tid,)).fetchall()
        events = conn.execute(
            "SELECT author, action, from_v, to_v, revision, costed, note, created_at "
            "FROM verdict_events WHERE thread_id=? ORDER BY id", (tid,)).fetchall()
    finally:
        conn.close()
    print(f"=== thread #{tid} 当前判定（verdicts 表，全文；修订归属见轨迹） ===")
    for a, v, n, ts in rows:
        print(f"\n[{a}] {v} @ {ts}\n{n}")
    print(f"\n=== thread #{tid} 投票轨迹（verdict_events，含已清票的历史修订） ===")
    for a, act, fv, tv, r, c, n, ts in events:
        print(f"{ts} [{a}] {act} {fv}->{tv} rev{r} cost={c} note={n or ''}")

if __name__ == "__main__":
    main()
