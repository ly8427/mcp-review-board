#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rb-notes.py — 判定理由全文读出（thread #45 rev2 配套，修"理由困在 160 字截断里"）。

server v2.7 读面把 verdict note 压平截断到 160 字（协议条款），但**全文存在库里**
（server.py L264 自述 "the full text lives in storage"）。本脚本走只读连接直取全文，
不碰运行中的服务器、不推进任何游标/心跳（C3 同款纪律）。

用法: python rb-notes.py <thread_id> [revision]
"""
import json
import sqlite3
import sys

DB = r"C:\Users\liu\ZCodeProject\mcp-review-board\data\reviewboard.db"

def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    tid = int(sys.argv[1])
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    conn.execute("PRAGMA query_only=1")
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
