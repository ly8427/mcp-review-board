#!/usr/bin/env python3
"""Export board history as STRUCTURE-ONLY, pseudonymized JSON (v2.5 fixtures).

Reads a database file and writes the skeleton the replay tests need:

    threads: id, status, revision, quorum (pseudos),
             comments[{id, author, ts}],
             verdict_events[{author, action, from_v, to_v, revision, ts}]

Privacy: the fixture is COMMITTED TO A PUBLIC REPO, so it carries structure
only — no titles, no bodies, no context, no notes, no creator. Author handles
map to stable pseudonyms (m1..mN, ordered by first_seen then name); the
mapping is never exported. Comment ids are kept verbatim so replay
assertions can name real positions (e.g. thread #30 must fire at #324).

Safety: run this against a BACKUP COPY, never the live WAL db (AGENTS.md
2026-09-27 「活库禁直读」). configs/backup-db.sh produces a consistent
snapshot via the SQLite online-backup API.

Usage: python tools/export_history.py <db-path> <out-json-path>
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def export(db_path: str, out_path: str) -> None:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # Stable pseudonyms: order by first_seen then name so re-exports of
        # the same history produce identical output.
        names = [
            r["author"]
            for r in conn.execute(
                "SELECT author FROM participants ORDER BY first_seen, author"
            ).fetchall()
        ]
        pseudo = {n: f"m{i + 1}" for i, n in enumerate(names)}

        threads = []
        for t in conn.execute(
            "SELECT id, status, revision, quorum FROM threads ORDER BY id"
        ).fetchall():
            quorum = json.loads(t["quorum"]) if t["quorum"] else []
            comments = [
                {"id": c["id"], "author": pseudo.get(c["author"], c["author"]),
                 "ts": c["created_at"]}
                for c in conn.execute(
                    "SELECT id, author, created_at FROM comments"
                    " WHERE thread_id=? ORDER BY created_at, id",
                    (t["id"],),
                ).fetchall()
            ]
            events = [
                {"author": pseudo.get(e["author"], e["author"]),
                 "action": e["action"], "from_v": e["from_v"], "to_v": e["to_v"],
                 "revision": e["revision"], "ts": e["created_at"]}
                for e in conn.execute(
                    "SELECT author, action, from_v, to_v, revision, created_at"
                    " FROM verdict_events WHERE thread_id=? ORDER BY created_at, id",
                    (t["id"],),
                ).fetchall()
            ]
            threads.append({
                "id": t["id"], "status": t["status"], "revision": t["revision"],
                "quorum": [pseudo.get(n, n) for n in quorum],
                "comments": comments, "verdict_events": events,
            })
    finally:
        conn.close()

    doc = {
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "pseudonym_note": "authors -> m1..mN by first_seen order; mapping not exported",
        "threads": threads,
    }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    n_comments = sum(len(t["comments"]) for t in threads)
    print(f"exported {len(threads)} threads / {n_comments} comments -> {out}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python tools/export_history.py <db-path> <out-json-path>")
    export(sys.argv[1], sys.argv[2])
