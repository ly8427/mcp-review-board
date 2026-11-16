#!/usr/bin/env python3
"""Batch D (threads #26/#27): schema versioning + board identity.

Covers:
  a) fresh db  -> meta stamped (schema_version, board_instance_id)
  b) legacy no-meta db WITH data rows -> identified, shape completed, stamped,
     data intact (column-probing is now an IDENTIFIER, not the migration)
  c) idempotent re-init -> same version, same board id
  d) db NEWER than the server -> loud refusal to start
  e) /attention exposes a STABLE board_id across server restarts (HTTP level)
  f) schema.sql and the embedded _SCHEMA_FALLBACK create identical schemas —
     f1 table sets, f2 columns/FK/PK, f3 indexes (index_list + index_xinfo),
     f4 AUTOINCREMENT boolean; the full coverage contract (covered faces and
     registered blind faces) lives in-suite in the f2 comment block — the
     manual-sync debt is guarded by a test, not by hope
"""
import json
import os
import re
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
    c = sqlite3.connect(db)
    try:  # close before return: an open handle kills Windows tempdir cleanup (dsh #444)
        return dict(c.execute("SELECT key, value FROM meta").fetchall())
    finally:
        c.close()


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
        cn = sqlite3.connect(db1)
        try:
            fresh_cols = {r[1]: r[3] for r in cn.execute("PRAGMA table_info(participants)")}
        finally:
            cn.close()
        check("a3 fresh (v2.12.0 1.1b/C2): participants.last_seen is nullable",
              fresh_cols.get("last_seen") == 0)

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
        # f2 (dsh #2421 §5, extended by live mutation M-A2; FK half already
        # anticipated by dsh #2423 §4 — foreign_key_list, not a mutation
        # find): column-level
        # parity. f1 only compares table NAME sets; PRAGMA table_info alone
        # still misses FKs — M-A2 (column-level REFERENCES delta on one side)
        # kept a table_info-only f2 green, because foreign keys live in
        # PRAGMA foreign_key_list, not table_info. So compare per-table
        # (name, type, notnull, dflt_value, pk) tuples AND projected
        # foreign_key_list columns across ALL tables, instead of hand-copying
        # either DDL into a second textual source.
        # Coverage contract (dsh #2425 §1 choice (a), extended per dsh
        # #2430 §2): f2 covers what the column/FK PRAGMAs expose — NOT
        # NULL, DEFAULT, FK (projected columns, see below), PK via the
        # r[5] slot (live carrier: verdicts composite PK). f3 (below)
        # wires PRAGMA index_list, covering indexes — origin='c' (live
        # carrier: idx_comments_thread/parent/created, schema.sql:34-36)
        # and origin='u' (UNIQUE; the #2427 §3.2 "not wired yet" tier is
        # now wired) — projection (name, unique, origin, partial) drops
        # seq (bookkeeping); index columns read via index_xinfo as ordered
        # (name, desc, coll) tuples over key=1 rows (dsh #2433 §2 choice
        # (a): desc/coll close the index-column COLLATE face). f4
        # (below) covers AUTOINCREMENT as a comment-stripped per-table
        # boolean (dsh #2430 §2.2 choice (a) — attribute presence only,
        # NOT a normalized full-DDL text diff, so #2421 §7 SQL-comment
        # normalization stays out; load-bearing: comments.id non-reuse
        # underpins the delivery-watermark injectivity argument
        # (server.py ~:4451) and the #944 to_v premise).
        # NOT covered / registered: CHECK — no pragma face (live carrier
        # verdicts.verdict CHECK, identical on both sides today). COLLATE:
        # the index-column face is wired via xinfo.coll (above); column-level
        # COLLATE surfaces only through an index on that column, otherwise no
        # pragma face — registered. partial is a 0/1 presence bit only — the
        # WHERE predicate text is NOT compared (dsh #2433 §3 choice (b):
        # wiring it needs a normalized sqlite_master diff, i.e. the #2421 §7
        # cost; zero carriers today — schema.sql has no WHERE). Expression
        # indexes project name=None, expression text not compared —
        # registered. WITHOUT ROWID / STRICT: NOT fully blind, correcting
        # dsh #2433 §4 (a static claim — measured M-S1): under either, the
        # table_info notnull bit reads 1 (vs 0) on PK columns that are not
        # a rowid alias and not already declared NOT NULL, so a one-side
        # mutation there goes f2-red (live: participants.author, meta.key;
        # M-S1 reproduced f2 — differing tables: ['participants']).
        # Residual blind faces, registered: STRICT/WITHOUT ROWID on
        # threads/comments/verdict_events (INTEGER PRIMARY KEY = rowid
        # alias, no bit to flip) and on verdicts (composite PK columns
        # already NOT NULL — measured all-green on a one-side STRICT, M-S2).
        # WITHOUT ROWID additionally can't coexist with AUTOINCREMENT
        # (SQLite rejects the combo), so comments/threads/verdict_events
        # carry that incidental guard; verdicts has none. Choice (b)
        # (normalized sqlite_master.sql text diff) would cover these and all
        # remaining faces but drags SQL-comment normalization (#2421 §7)
        # from low-realism to mandatory; declined for now.
        tables = names(ta) | names(tb)
        shape = lambda cx, t: (  # noqa: E731
            sorted((r[1], r[2], r[3], r[4], r[5])
                   for r in cx.execute(f"PRAGMA table_info({t})")),
            # FK half compares projected columns (table, from, to,
            # on_update, on_delete, match) — dsh #2427 §3.1. The full row
            # carries id/seq = declaration-order bookkeeping, so a
            # semantically-equal FK set written in a different order on
            # one side false-reds (live carrier: comments declares two
            # FKs; M-F1 repro'd red on the full-row shape, green here).
            # key=repr kept: 'to' may be None.
            sorted(((r[2], r[3], r[4], r[5], r[6], r[7])
                    for r in cx.execute(f"PRAGMA foreign_key_list({t})")),
                   key=repr))
        differing = sorted(t for t in tables if shape(ta, t) != shape(tb, t))
        # this suite's check() is (label, ok) only — fold the failing tables
        # into the label so a red names what to fix (batch2 suite carries a
        # detail slot; here the label is the outlet). Label names both
        # halves: table_info+pk and projected FK columns — both
        # load-bearing.
        f2_label = ("f2 schema.sql == embedded fallback "
                    "(table_info+pk + FK projected columns per table)")
        if differing:
            f2_label += f" — differing tables: {differing}"
        check(f2_label, not differing)
        # f3 (dsh #2430 §2.1, exit (i); xinfo upgrade per dsh #2433 §2
        # choice (a)): index parity — one PRAGMA closes both open families:
        # origin='c' (normal indexes; live carrier idx_comments_*,
        # schema.sql:34-36) and origin='u' (UNIQUE — the #2427 §3.2
        # "not wired yet" tier, wired here). Projection (name, unique,
        # origin, partial) drops seq (bookkeeping); columns read via
        # index_xinfo as ordered (name, desc, coll) tuples over key=1 rows
        # — desc/coll close the index-column COLLATE/DESC faces (coll
        # 'NOCASE' vs 'BINARY' projects differently), the auxiliary rowid
        # entry (key=0) stays out, and the xinfo `key` column itself is
        # not compared (auxiliary columns are not key face). partial is
        # presence-only (contract above). key=repr kept: expression-index
        # names may be None.
        idx_shape = lambda cx, t: sorted(  # noqa: E731
            ((r[1], r[2], r[3], r[4],
              tuple((i[2], i[3], i[4])
                    for i in cx.execute(f"PRAGMA index_xinfo({r[1]})") if i[5]))
             for r in cx.execute(f"PRAGMA index_list({t})")),
            key=repr)
        idx_diff = sorted(t for t in tables if idx_shape(ta, t) != idx_shape(tb, t))
        f3_label = ("f3 schema.sql == embedded fallback "
                    "(index_list+index_xinfo per table)")
        if idx_diff:
            f3_label += f" — differing tables: {idx_diff}"
        check(f3_label, not idx_diff)
        # f4 (dsh #2430 §2.2, choice (a)): AUTOINCREMENT presence per
        # table, read from sqlite_master.sql with SQL comments stripped —
        # an attribute boolean, NOT a normalized full-DDL diff, so the
        # #2421 §7 comment-normalization quagmire stays out. Load-bearing:
        # comments.id non-reuse is the stated premise of the delivery
        # watermark injectivity argument (server.py ~:4451) and of #944
        # to_v. Known edge (theoretical, registered): the keyword inside
        # a DDL string literal would false-positive — zero carriers today.
        def ai_bool(cx, t):
            row = cx.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (t,)).fetchone()
            if row is None or row[0] is None:
                return False  # internal tables (sqlite_sequence) carry no sql
            s = re.sub(r"/\*.*?\*/", " ", row[0], flags=re.S)
            s = re.sub(r"--[^\n]*", " ", s)
            return "AUTOINCREMENT" in s.upper()
        ai_diff = sorted(t for t in tables if ai_bool(ta, t) != ai_bool(tb, t))
        f4_label = ("f4 schema.sql == embedded fallback "
                    "(AUTOINCREMENT per table, comment-stripped boolean)")
        if ai_diff:
            f4_label += f" — differing tables: {ai_diff}"
        check(f4_label, not ai_diff)
        ta.close(); tb.close()  # dsh #444: same WinError 32 shape as the v2.8 matrix had

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
            INSERT INTO participants(author, first_seen, last_seen, last_read, meta)
                VALUES ('reader', '2026-01-01 00:00:00', '2026-01-01 00:00:00',
                        '2026-01-01 00:00:02', '{"token_hash":"deadbeef"}');
            INSERT INTO participants(author, first_seen, last_seen, last_read)
                VALUES ('newbie', '2026-01-01 00:00:00', '2026-01-01 00:00:00',
                        NULL);
        """)
        c.commit(); c.close()
        _use_db(db5)
        server.BOARD_INSTANCE_ID = None
        server.init_db()
        c = sqlite3.connect(db5)
        pinfo = {r[1]: r[3] for r in c.execute("PRAGMA table_info(participants)")}
        cols = set(pinfo)
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
        # 1.1b/C2 rebuild: an old NOT NULL table is relaxed to nullable, rows kept
        check("g5 v5 NOT NULL last_seen -> nullable after init_db "
              "(v2.12.0 1.1b/C2 rebuild)", pinfo.get("last_seen") == 0)
        # The rebuild copies a `keep` column list: if someone drops "meta" from
        # it, every token_hash is silently wiped on first boot and all three
        # suites stay green (dsh #1293 §五2).  The fixture now carries a payload
        # ({"token_hash":"deadbeef"}) so the copy is asserted, not assumed.
        c = sqlite3.connect(db5)
        kept = json.loads(
            c.execute("SELECT meta FROM participants WHERE author='reader'").fetchone()[0]
            or "{}")
        c.close()
        check("g6 v5->v6 rebuild preserves meta payload (token_hash survives)",
              kept.get("token_hash") == "deadbeef")

    print()
    if fails:
        print(f"❌ SCHEMA MIGRATION TESTS: {fails} failure(s)")
        sys.exit(1)
    print("✅ SCHEMA MIGRATION TESTS PASSED: stamp/identify/idempotent/refuse-newer/"
          "stable-board-id/dual-schema-parity")


if __name__ == "__main__":
    main()
