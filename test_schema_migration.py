#!/usr/bin/env python3
"""Batch D (threads #26/#27): schema versioning + board identity.

Covers:
  a) fresh db  -> meta stamped (schema_version, board_instance_id)
  b) legacy no-meta db WITH data rows -> identified, shape completed, stamped,
     data intact (column-probing is now an IDENTIFIER, not the migration)
  c) idempotent re-init -> same version, same board id
  d) db NEWER than the server -> loud refusal to start
  e) /attention exposes a STABLE board_id across server restarts (HTTP level)
  f) schema.sql and the embedded _SCHEMA_FALLBACK create an IDENTICAL table
     set — the manual-sync debt is guarded by a test, not by hope
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import server  # noqa: E402


def _use_db(path: Path) -> None:
    server.DB_PATH = path
    server.BOARD_INSTANCE_ID = None


def _meta(db) -> dict:
    return dict(sqlite3.connect(db).execute("SELECT key, value FROM meta").fetchall())


def main() -> None:
    fails = 0

    def check(label, ok):
        nonlocal fails
        print(("PASS " if ok else "FAIL ") + label)
        fails += 0 if ok else 1

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        # a) fresh db
        db1 = td / "fresh.db"
        _use_db(db1)
        server.init_db()
        m = _meta(db1)
        check("a1 fresh: schema_version stamped", m.get("schema_version") == str(server.SCHEMA_VERSION))
        check("a2 fresh: board_instance_id present", len(m.get("board_instance_id", "")) >= 16)

        # b) legacy db with REAL data rows: a genuine pre-v2 threads table
        #    (no author/quorum/budget/revision) with a complete comments shape.
        #    Boundary note: an even-older comments table without parent_id is
        #    OUTSIDE the supportable range — executescript fails loudly on it
        #    (unchanged net behavior; loud, not silent).
        db2 = td / "legacy.db"
        c = sqlite3.connect(db2)
        c.executescript("""
            CREATE TABLE threads (id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL DEFAULT (datetime('now')));
            CREATE TABLE comments (id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id INTEGER NOT NULL, parent_id INTEGER, author TEXT NOT NULL,
                body TEXT NOT NULL, file TEXT, line INTEGER, severity TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL DEFAULT (datetime('now')));
            INSERT INTO threads(title) VALUES ('legacy thread');
            INSERT INTO comments(thread_id, author, body) VALUES (1, 'old-agent', 'kept?');
        """)
        c.commit(); c.close()
        _use_db(db2)
        server.init_db()
        c = sqlite3.connect(db2)
        cols = {r[1] for r in c.execute("PRAGMA table_info(threads)")}
        rows = c.execute("SELECT count(*) FROM comments").fetchone()[0]
        c.close()
        m = _meta(db2)
        check("b1 legacy: shape completed (author/quorum cols added)", {"author", "quorum"} <= cols)
        check("b2 legacy: data rows survive", rows == 1)
        check("b3 legacy: stamped to current", m.get("schema_version") == str(server.SCHEMA_VERSION))

        # c) idempotent
        bid_before = m.get("board_instance_id")
        server.BOARD_INSTANCE_ID = None
        server.init_db()
        m2 = _meta(db2)
        check("c1 idempotent: same version", m2.get("schema_version") == str(server.SCHEMA_VERSION))
        check("c2 idempotent: same board id", m2.get("board_instance_id") == bid_before)

        # d) newer db refuses to start
        db3 = td / "future.db"
        _use_db(db3)
        server.init_db()
        c = sqlite3.connect(db3)
        c.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
        c.commit(); c.close()
        server.BOARD_INSTANCE_ID = None
        try:
            server.init_db()
            check("d1 newer db refuses", False)
        except RuntimeError as e:
            check("d1 newer db refuses (loud)", "NEWER" in str(e) and "refusing" in str(e))

        # e) HTTP: stable board_id across restarts
        db4 = td / "http.db"
        env = dict(os.environ, REVIEWBOARD_PORT="18877", REVIEWBOARD_DB=str(db4))
        def probe_id():
            for _ in range(30):
                try:
                    r = json_load(urllib.request.urlopen(
                        "http://localhost:18877/attention?author=probe-x", timeout=3))
                    return r.get("board_id")
                except Exception:
                    time.sleep(1)
            return None
        import json as _json
        def json_load(resp):
            return _json.loads(resp.read().decode())
        p1 = subprocess.Popen([sys.executable, str(REPO / "server.py")], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        bid1 = probe_id()
        p1.terminate(); p1.wait(timeout=15)
        p2 = subprocess.Popen([sys.executable, str(REPO / "server.py")], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        bid2 = probe_id()
        p2.terminate(); p2.wait(timeout=15)
        check("e1 http: board_id exposed", bool(bid1))
        check("e2 http: board_id stable across restarts", bid1 == bid2 and bool(bid1))

        # f) schema.sql vs embedded fallback create identical table sets
        ta = sqlite3.connect(td / "fa.db"); ta.executescript((REPO / "schema.sql").read_text())
        tb = sqlite3.connect(td / "fb.db"); tb.executescript(server._SCHEMA_FALLBACK)
        names = lambda cx: {r[0] for r in cx.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        check("f1 schema.sql == embedded fallback (table sets)", names(ta) == names(tb))

    print()
    if fails:
        print(f"❌ SCHEMA MIGRATION TESTS: {fails} failure(s)")
        sys.exit(1)
    print("✅ SCHEMA MIGRATION TESTS PASSED: stamp/identify/idempotent/refuse-newer/"
          "stable-board-id/dual-schema-parity")


if __name__ == "__main__":
    main()
