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
            check("d1 newer db refuses (loud)", False)
        except RuntimeError as e:
            check("d1 newer db refuses (loud)", "NEWER" in str(e) and "refusing" in str(e))

        # d2/d3) existing meta with an OLDER version → forward-migration path
        # (GPT round-3 C2): the (empty) chain runs and the version is stamped
        # UP to current — an older-meta db never falls through unstamped.
        db35 = td / "oldermeta.db"
        _use_db(db35)
        server.init_db()
        c = sqlite3.connect(db35)
        c.execute("INSERT INTO threads(title, author) VALUES ('from-v4', 'old')")
        c.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
        c.commit(); c.close()
        server.BOARD_INSTANCE_ID = None
        server.init_db()
        c = sqlite3.connect(db35)
        rows = c.execute("SELECT count(*) FROM threads WHERE title='from-v4'").fetchone()[0]
        c.close()
        check("d2 older-meta stamped up to current",
              _meta(db35).get("schema_version") == str(server.SCHEMA_VERSION))
        check("d3 older-meta data preserved", rows == 1)

        # d4) forgotten migration fails closed (GPT round-4): a declared
        # SCHEMA_VERSION with a MISSING chain step refuses to start instead
        # of stamping over an unmigrated shape.
        db36 = td / "missingstep.db"
        _use_db(db36)
        server.init_db()
        c = sqlite3.connect(db36)
        c.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
        c.commit(); c.close()
        server.BOARD_INSTANCE_ID = None
        saved = dict(server._MIGRATIONS); server._MIGRATIONS.pop(4)
        try:
            server.init_db()
            check("d4 missing migration fails closed", False)
        except RuntimeError as e:
            check("d4 missing migration fails closed", "missing migration 4 -> 5" in str(e))
        finally:
            server._MIGRATIONS.clear(); server._MIGRATIONS.update(saved)

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

        # g) v5 -> v6 (issue #3): id-cursor column added + backfill semantics.
        #    Hand-built v5 db: participants WITHOUT last_delivered_id, meta
        #    stamped '5', controlled comments around one reader's watermark.
        db5 = td / "v5cursor.db"
        c = sqlite3.connect(db5)
        c.executescript((REPO / "schema.sql").read_text())
        # rebuild participants in the v5 shape (schema.sql above created the
        # v6 shape; SQLite cannot DROP COLUMN in place portably)
        c.execute("DROP TABLE participants")
        c.execute("""CREATE TABLE participants (author TEXT PRIMARY KEY,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            last_read TEXT, meta TEXT NOT NULL DEFAULT '{}')""")
        c.execute("INSERT OR REPLACE INTO meta(key, value)"
                  " VALUES('schema_version', '5')")
        c.executescript("""
            INSERT INTO threads(title, author) VALUES ('cursor', 'h');
            INSERT INTO comments(thread_id, author, body, created_at) VALUES
                (1, 'h', 'before', '2026-01-01 00:00:01');
            INSERT INTO comments(thread_id, author, body, created_at) VALUES
                (1, 'h', 'same-sec-a', '2026-01-01 00:00:02');
            INSERT INTO comments(thread_id, author, body, created_at) VALUES
                (1, 'h', 'same-sec-b', '2026-01-01 00:00:02');
            INSERT INTO comments(thread_id, author, body, created_at) VALUES
                (1, 'h', 'after', '2026-01-01 00:00:03');
            INSERT INTO participants(author, first_seen, last_seen, last_read)
                VALUES ('reader', '2026-01-01 00:00:00', '2026-01-01 00:00:00',
                        '2026-01-01 00:00:02');
            INSERT INTO participants(author, first_seen, last_seen, last_read)
                VALUES ('newbie', '2026-01-01 00:00:00', '2026-01-01 00:00:00',
                        NULL);
        """)
        c.commit(); c.close()
        _use_db(db5)
        server.BOARD_INSTANCE_ID = None
        server.init_db()
        c = sqlite3.connect(db5)
        cols = {r[1] for r in c.execute("PRAGMA table_info(participants)")}
        got = dict(c.execute(
            "SELECT author, last_delivered_id FROM participants").fetchall())
        c.close()
        check("g1 v5->v6: column added", "last_delivered_id" in cols)
        check("g2 v5->v6: stamped", _meta(db5).get("schema_version") == str(server.SCHEMA_VERSION))
        # reader watermark 00:00:02 -> newest id STRICTLY before it = id 1
        # ('before'); the two same-second boundary comments (ids 2,3) sit
        # ABOVE the backfill and re-deliver once — the self-heal.
        check(f"g3 v5->v6: backfill strictly-before + self-heal boundary (got {got})",
              got.get("reader") == 1)
        check(f"g4 v5->v6: NULL last_read stays 0 / never-polled (got {got})",
              got.get("newbie") == 0)

    print()
    if fails:
        print(f"❌ SCHEMA MIGRATION TESTS: {fails} failure(s)")
        sys.exit(1)
    print("✅ SCHEMA MIGRATION TESTS PASSED: stamp/identify/idempotent/refuse-newer/"
          "stable-board-id/dual-schema-parity")


if __name__ == "__main__":
    main()
