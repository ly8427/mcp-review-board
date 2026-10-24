"""
MCP Review Board — a lightweight shared code-review board for three AI coding
agents (ZCode on Windows, Claude Code in WSL, Trae CN on Windows) to post,
reply, and resolve review comments WITHOUT a human copy-pasting between them.

Transport: Streamable HTTP at http://localhost:8765/mcp
Storage  : SQLite (WAL mode) — single file under data/reviewboard.db
Identity : explicit `author` string arg on every mutating tool
         (zcode / claude / trae — three tools handle custom HTTP headers
          inconsistently, so author is a plain required parameter instead).

Also serves a READ-ONLY HTML view at http://localhost:8765/ so a human can
watch the three-way discussion without opening any IDE.

Run:  python3 server.py      (WSL)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

# --- paths -----------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = BASE_DIR / "schema.sql"
# Installed (pipx/wheel) layouts ship server.py alone — no schema.sql next to
# it. That's the repo-vs-installed marker, and it decides where DATA lives:
# a pipx venv is disposable (upgrade replaces it), so the append-only audit
# db must NOT default into it in installed layouts — use a user-owned dir.
_INSTALLED_LAYOUT = not SCHEMA_PATH.exists()


def _default_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "mcp-review-board"
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "mcp-review-board"
    return Path.home() / ".local" / "state" / "mcp-review-board"


DATA_DIR = _default_data_dir() if _INSTALLED_LAYOUT else BASE_DIR / "data"
DB_PATH = Path(os.environ.get("REVIEWBOARD_DB", str(DATA_DIR / "reviewboard.db")))

# Batch D (thread #26/#27): storage-shape version, deliberately SEPARATE from
# PROTOCOL_VERSION (governance semantics). Legacy databases (no meta table)
# are identified by column probing and stamped; a db NEWER than the server is
# refused loudly. The migration chain grows here.
# v2.8 (threads #39/#40): SCHEMA_VERSION 7 = CORE (6) + the search read face
# (comments_fts trigram external-content table + sync triggers). The search
# layer is CAPABILITY-GATED: on a runtime without FTS5/trigram the core
# service still starts (search disabled, reason recorded) — so the generic
# stamp/chain below still targets CORE only, and _search_bootstrap() owns the
# 6 -> 7 step inside ONE explicit transaction (pi #424: BEGIN IMMEDIATE, no
# executescript, schema_version stamp as the LAST statement — a failure can
# never leave a stamped-but-unrebuilt half-migration).
SCHEMA_VERSION = 7
CORE_SCHEMA_VERSION = 6
BOARD_INSTANCE_ID: str | None = None  # set by init_db; exposed via /attention
# Forward migrations keyed by the version they upgrade FROM (GPT round-3 C2
# + round-4 P3): _MIGRATIONS[4] upgrades a v4 db to v5. EVERY step up to
# SCHEMA_VERSION must be EXPLICITLY declared — value None means "no shape
# change needed, just stamp"; a step MISSING from the dict is a forgotten
# migration and fails closed at startup instead of silently stamping over an
# unmigrated shape.
def _v6_id_cursor(conn: sqlite3.Connection) -> None:
    """v5 -> v6 (issue #3): monotonic delivery watermark replaces the
    second-resolution last_read comparison. last_read stays as a legacy
    wall-clock face; the delivery truth is last_delivered_id. Backfill maps
    each reader's old timestamp watermark to the newest comment id STRICTLY
    BEFORE it — boundary rows (created_at == last_read, which the old strict
    > never delivered and might have silently lost) sit ABOVE the backfilled
    id, so they are re-delivered exactly once after the upgrade: a free
    self-heal of any historical same-second drops. NULL last_read means
    never-polled -> cursor 0 (their first poll runs the since-window)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(participants)")}
    if "last_delivered_id" not in cols:
        # SQLite has no ADD COLUMN IF NOT EXISTS (same guard pattern as
        # _legacy_shape_fixups): a meta-stamped-older db whose tables were
        # already created in the v6 shape by executescript above must not
        # crash on a duplicate column.
        conn.execute(
            "ALTER TABLE participants ADD COLUMN last_delivered_id"
            " INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        """UPDATE participants SET last_delivered_id = COALESCE((
               SELECT MAX(c.id) FROM comments c
               WHERE c.created_at < participants.last_read), 0)
           WHERE last_read IS NOT NULL"""
    )


_MIGRATIONS: dict[int, callable | None] = {4: None, 5: _v6_id_cursor}
# Mirror of schema.sql for pipx/wheel-installed layouts: a top-level module
# ships alone, so schema.sql does not exist next to it. The repo file stays
# authoritative — when both exist, the file wins. Keep the two in sync when
# editing schema.sql.
_SCHEMA_FALLBACK = """
-- MCP Review Board schema (embedded fallback — see schema.sql, authoritative)
CREATE TABLE IF NOT EXISTS threads (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    title              TEXT    NOT NULL,
    context            TEXT,
    status             TEXT    NOT NULL DEFAULT 'open',
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    author             TEXT,
    quorum             TEXT,
    per_author_budget  INTEGER NOT NULL DEFAULT 20,
    revision           INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id  INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    parent_id  INTEGER REFERENCES comments(id) ON DELETE CASCADE,
    author     TEXT    NOT NULL,
    body       TEXT    NOT NULL,
    file       TEXT,
    line       INTEGER,
    severity   TEXT,
    status     TEXT    NOT NULL DEFAULT 'open',
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_comments_thread ON comments(thread_id);
CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_id);
CREATE INDEX IF NOT EXISTS idx_comments_created ON comments(created_at);
CREATE TABLE IF NOT EXISTS participants (
    author            TEXT PRIMARY KEY,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    last_read         TEXT,
    last_delivered_id INTEGER NOT NULL DEFAULT 0,
    meta              TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS verdicts (
    thread_id  INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    author     TEXT    NOT NULL,
    verdict    TEXT    NOT NULL CHECK (verdict IN ('pass','object')),
    note       TEXT,
    updated_at TEXT    NOT NULL,
    PRIMARY KEY (thread_id, author)
);
CREATE TABLE IF NOT EXISTS verdict_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id  INTEGER,
    author     TEXT    NOT NULL,
    action     TEXT    NOT NULL,
    from_v     TEXT,
    to_v       TEXT,
    revision   INTEGER,
    costed     INTEGER NOT NULL DEFAULT 0,
    note       TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
-- Batch D (thread #27): instance metadata — mirror of the schema.sql tail.
-- Keep the two in sync; guarded by test_schema_migration.py (both paths must
-- create an identical table set).
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _load_schema() -> str:
    if SCHEMA_PATH.exists():
        return SCHEMA_PATH.read_text(encoding="utf-8")
    return _SCHEMA_FALLBACK

HOST = os.environ.get("REVIEWBOARD_HOST", "127.0.0.1")
PORT = int(os.environ.get("REVIEWBOARD_PORT", "8765"))

# Hard discussion cap: total comments (posts + replies) per thread. Enforced in
# post_comment / reply_comment — a full thread rejects new posts and tells the
# agent to conclude with set_status. Set REVIEWBOARD_THREAD_CAP to override (tests).
THREAD_CAP = int(os.environ.get("REVIEWBOARD_THREAD_CAP", "100"))

VALID_STATUSES = ("open", "resolved", "wontfix")
VALID_SEVERITIES = ("info", "minor", "major", "blocker")

# v2 A1 liveness knobs. Display tier (⚠️ in list_participants / board) uses
# DISPLAY_IDLE_MIN; the functional suspension threshold (24h, stage 3b) uses
# SUSPEND_AFTER_HOURS. Both read the same last_seen via is_active() — one
# shared pure function, no per-callsite drift (claude #52 条件:口径单点化).
DISPLAY_IDLE_MIN = 30
SUSPEND_AFTER_HOURS = 24


# --- v2 A1: participant registry / heartbeat --------------------------------
# Single source of truth for the served version (v2.9.3 #55: was duplicated in
# three literals — FastMCP serverInfo, /ping, PROTOCOL header — and drifted).
PROTOCOL_VERSION = "2.9.3"
mcp = FastMCP("ReviewBoard", version=PROTOCOL_VERSION)  # align serverInfo with pyproject
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _touch(conn: sqlite3.Connection, author: str | None) -> None:
    """Heartbeat: upsert last_seen (the D1 'can participate' signal).
    Registering is free — any author-carrying call does it, polling included.
    NEVER advances last_read (that is list_comments_since's exclusive job, C3)."""
    if not author:
        return
    now = _now_iso()
    cur = conn.execute(
        "UPDATE participants SET last_seen=? WHERE author=?", (now, author)
    )
    if cur.rowcount == 0:
        conn.execute(
            "INSERT INTO participants(author, first_seen, last_seen, last_read, meta)"
            " VALUES(?,?,?,NULL,'{}')",
            (author, now, now),
        )
        # v2.6 (issue #3): registration leaves cursor 0 / last_read NULL =
        # never-polled. Their FIRST list_comments_since runs the since-window
        # only (the v2.5 shape — no full-history dump), and an invitee's probe
        # keeps seeing attention=1 until that first real read (invitee
        # first-wake semantics unchanged).


def _relative(utc: str) -> str:
    """Duration-based display ('3 分钟前') — timezone-agnostic by construction."""
    try:
        then = datetime.strptime(utc, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return utc
    secs = max(0, (datetime.now(timezone.utc) - then).total_seconds())
    if secs < 60:
        return f"{int(secs)} 秒前"
    if secs < 3600:
        return f"{int(secs // 60)} 分钟前"
    if secs < 86400:
        return f"{int(secs // 3600)} 小时前"
    return f"{int(secs // 86400)} 天前"


def _is_active(last_seen: str | None, now: datetime | None = None) -> bool:
    """The one shared activity predicate (claude #52: 单点口径). Stage 1 uses it
    for display; stage 3b wires it into every governance write path."""
    if not last_seen:
        return False
    try:
        then = datetime.strptime(last_seen, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - then).total_seconds() < SUSPEND_AFTER_HOURS * 3600


def _oneline(s: str, limit: int) -> str:
    """Collapse to one line and truncate with an ellipsis marker (v2.7 read
    faces: verdict/gate notes stay scannable; the full text lives in storage)."""
    flat = " ".join(s.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


# --- v2.9.2 F6 / v2.9.3 票3 (thread #53): heartbeat-rhythm gate --------------
# 票3 把三判据拆开计量（#53 决议对象）：
#   S1 新鲜度 = 零 LLM 成本探针(/attention)或空闲轮询的最新时间；
#   S2/S3    = 只认"确实读到他人未读评论"的轮询（堵 G-2 空转六次）；
#   通道二   = 探针 24h≥6 次 + 最新≤45min + 无未读积压 → 免 S2/S3 建帖；
#   冷启动   = 近 24h 无任何心跳（可恢复，不再被 ever_beats 永久锁死）。
# 参数各一处常量（C3/P4 口径单点）。
BEAT_RING_MAX = 64          # 每个 ring 定长，防 meta JSON 无界膨胀
BEAT_DEDUP_S = 120          # 写入去重阈值
BEAT_READ_KEY = "hb_read"   # 真读到他人未读内容的轮询（S2/S3 唯一来源）
BEAT_PROBE_KEY = "probe"    # /attention 探针（S1 + 通道二；零 LLM 成本）
F6_BEATS_24H = 6            # 近 24h ≥6 次读交付轮询（或 ≥6 次探针走通道二）
F6_GAPS_MIN = 5             # 其中 ≥5 个相邻间隔…
F6_GAP_S = 600              # …每个 ≥10 分钟
F6_FRESH_S = 45 * 60        # 最新一条 ≤45 分钟（与 L2 打卡门同一数字）
# v2.9.3 #64 (thread #64, pi #715 §4 self-reported hole + dsh #716/#718 复核):
# the legacy `hb` fallback in `_read_ts` was an unbounded "migration amnesty" —
# a member who never made a read-delivering poll kept feeding S2/S3 from `hb`
# (appended on EVERY poll, idle included), so the G-2 idle-spin hole could be
# deferred arbitrarily long and then cashed in on the first real read.
# Fix = a deployment epoch: only hb entries OLDER than the epoch get amnesty.
#   F6_READ_EPOCH_KEY   global deploy epoch (meta table, epoch seconds,
#                       only-increase — same shape as floor_wm_epoch) AND a
#                       per-member COPY inside participants.meta (never authored
#                       by the member, never "whoever migrates writes now").
#   HB_READ_MIGRATED_KEY  per-member marker: the one-time migration already ran.
#                       Deliberately a marker, not the truthiness of hb_read:
#                       after epoch truncation an empty hb_read is the NORMAL
#                       state, so `if not meta.get("hb_read")` would re-run the
#                       migration on every read poll and re-import post-epoch
#                       idle beats (dsh #718 §3 / C').
F6_READ_EPOCH_KEY = "f6_read_epoch"
HB_READ_MIGRATED_KEY = "hb_read_migrated"
F6_READ_EPOCH_CODE = 1_792_800_000   # = 2026-10-24T00:00:00Z，首次部署日（不写 0：0 等于
                           # "没有下界"，日后误配置会静默失效）。生效值 = max(常量, 首启 now)，
                           # 写入 meta 的是首启时刻本身，此后**冻结**（不随后续启动推进）。
                           # 口径更正（pi #739 / 回 dsh #735 §3-②，我原注释写反了）：
                           # 纪元是"恩赦上界"——只有 ts < 纪元 的 hb 条目算迁移恩赦，
                           # 所以**抬高纪元 = 放宽恩赦**（把 (旧纪元, 新纪元) 之间那些
                           # 未经验证的轮询重新算成 pre-epoch），最多放宽到 24h 环的容量。
                           # ⇒ 不要用"抬高纪元"去作废恩赦窗：恩赦窗的唯一出口是部署后
                           # 24h 自然过期（_ring_ts 只留 24h），本就不需要任何人工动作。
                           # 常量只在首次部署时给下界；日后抬常量会把所有"从未真读过"
                           # 的成员的恩赦窗重新打开（G-2 空转刷法复活）——测试
                           # test_v292_gates ⑦ 把这条钉死（成员副本更旧 = 更严，不是洞）。
                           # 只升不降保留：仅用于 meta 损坏时的下界（have < 常量 ⇒ 取常量），
                           # 不能反向把上界降到比首次部署更早。


def _ring_append(conn: sqlite3.Connection, author: str | None, key: str) -> bool:
    """Append `now` to one timestamp ring in participants.meta (single write
    point, C3). Returns False inside the 120s dedup window."""
    if not author:
        return False
    row = conn.execute("SELECT meta FROM participants WHERE author=?", (author,)).fetchone()
    meta = json.loads(row["meta"]) if row and row["meta"] else {}
    ring = [x for x in (meta.get(key) or []) if x]
    now = _now_iso()
    if ring:
        try:
            last_t = datetime.strptime(ring[-1], "%Y-%m-%d %H:%M:%S")
            if (datetime.strptime(now, "%Y-%m-%d %H:%M:%S") - last_t).total_seconds() < BEAT_DEDUP_S:
                return False  # dedup window
        except ValueError:
            pass
    ring.append(now)
    del ring[:-BEAT_RING_MAX]
    meta[key] = ring
    if row:
        conn.execute("UPDATE participants SET meta=? WHERE author=?",
                     (json.dumps(meta, ensure_ascii=False), author))
    else:
        conn.execute(
            "INSERT INTO participants(author, first_seen, last_seen, last_read, meta)"
            " VALUES(?,?,?,NULL,?)",
            (author, now, now, json.dumps(meta, ensure_ascii=False)),
        )
    return True


def _ts_epoch(x: str) -> int | None:
    """'%Y-%m-%d %H:%M:%S' (UTC) → epoch seconds; None if malformed."""
    try:
        return int(datetime.strptime(x, "%Y-%m-%d %H:%M:%S")
                   .replace(tzinfo=timezone.utc).timestamp())
    except (ValueError, TypeError):
        return None


def _read_epoch(conn: sqlite3.Connection) -> int:
    """Global F6 read-epoch, only-increase (mirrors `_floor_wm_epoch`).

    First start after deploy writes NOW (never 0 / never a member's migration
    moment) — otherwise "the later you migrate, the longer your amnesty" and
    the hole scales with each member's laziness (dsh #716 ①).
    """
    row = conn.execute("SELECT value FROM meta WHERE key=?", (F6_READ_EPOCH_KEY,)).fetchone()
    now_s = int(time.time())
    if row is None:
        eff = max(F6_READ_EPOCH_CODE, now_s)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)",
                     (F6_READ_EPOCH_KEY, str(eff)))
        # NOTE: the note deliberately carries NO wall-clock/ephemeral numbers
        # (stage6 part B requires byte-identical audit rows across runs); the
        # effective value lives in meta.f6_read_epoch, the timing in created_at.
        _audit(conn, "system", "f6_read_epoch_init",
               note=f"code={F6_READ_EPOCH_CODE} effective_from=meta")
        return eff
    try:
        have = int(row[0])
    except (TypeError, ValueError):
        have = 0
    eff = max(have, F6_READ_EPOCH_CODE)
    if eff != have:
        conn.execute("UPDATE meta SET value=? WHERE key=?", (str(eff), F6_READ_EPOCH_KEY))
        # dsh #740 §2: the only-increase branch is not cosmetic — a raised
        # epoch widens the amnesty UPPER bound for every member that has not
        # frozen a copy yet, so "who raised it to what" must be in the audit
        # table, not stdout only. The note carries DB/compile-time values and
        # no wall clock, so the row stays byte-identical across runs (stage6
        # part B). Idempotent: after the UPDATE `eff == have`, so no 2nd row.
        _audit(conn, "system", "f6_read_epoch_raise",
               note=f"have={have} code={F6_READ_EPOCH_CODE} effective={eff}")
        print(f"[f6_read_epoch] meta={have} < code={F6_READ_EPOCH_CODE}; "
              f"raised to {eff} (only-increase)", flush=True)
    return eff


def _beat(conn: sqlite3.Connection, author: str | None, delivered: int = 0) -> None:
    """One poll by the author.

    `hb` = presence (S1 freshness; every poll counts, including idle ones).
    `hb_read` = S2/S3 only: appended when this poll actually delivered unread
    content from OTHER authors. Probes/invites/writes never call this.

    Upgrade path (票3 + v2.9.3 #64 epoch): the first read-delivering poll after
    deploy seeds hb_read from the legacy hb ring — but ONLY the part older than
    the deployment epoch, so old rhythm is not retroactively punished while
    post-epoch idle beats never enter hb_read. The current poll's own timestamp
    is appended explicitly: it is >= epoch by construction, so truncation would
    otherwise drop it (dsh #718 §0-3: that append is a NECESSARY part of the
    truncation, not a fix for a bug the old code had). The migration is marked
    (`hb_read_migrated`) so it runs exactly once even when the kept set is empty.
    """
    if not author:
        return
    epoch = _read_epoch(conn)
    row = conn.execute("SELECT meta FROM participants WHERE author=?", (author,)).fetchone()
    meta = json.loads(row["meta"]) if row and row["meta"] else {}
    if meta.get(F6_READ_EPOCH_KEY) is None and row:
        # member-side COPY only, written ONCE and then frozen. Deliberate: the
        # epoch is an amnesty UPPER bound (only ts < epoch is grandfathered), so
        # a member copy that is older than a later global value is STRICTER, not
        # a hole. dsh #735 §3-② proposed `max(copy, global)` here; rejected in
        # pi #739 — with the bound raised to the new global, an un-migrated
        # member's whole 24h idle `hb` ring becomes "pre-epoch" again and 6 idle
        # polls would pass S2/S3 the moment the constant is bumped at a later
        # deploy (test_v292_gates ⑦ pins the strict behaviour).
        meta[F6_READ_EPOCH_KEY] = epoch
        conn.execute("UPDATE participants SET meta=? WHERE author=?",
                     (json.dumps(meta, ensure_ascii=False), author))
    _ring_append(conn, author, "hb")
    if not delivered:
        return
    row = conn.execute("SELECT meta FROM participants WHERE author=?", (author,)).fetchone()
    meta = json.loads(row["meta"]) if row and row["meta"] else {}
    if not meta.get(HB_READ_MIGRATED_KEY):
        try:
            epoch = int(meta.get(F6_READ_EPOCH_KEY) or epoch)
        except (TypeError, ValueError):
            pass
        legacy = [x for x in (meta.get("hb") or []) if x]
        # `is not None` (dsh #735 §3-③, accepted): a malformed timestamp is not
        # "ancient" — `(_ts_epoch(x) or 0)` mapped it to 0 and kept it, so junk
        # strings entered hb_read (invisible to _ring_ts, but they pushed real
        # beats out of BEAT_RING_MAX). Dropped here instead.
        kept = [x for x in legacy
                if _ts_epoch(x) is not None and _ts_epoch(x) < epoch]
        now_iso = _now_iso()
        kept.append(now_iso)
        meta[BEAT_READ_KEY] = kept[-BEAT_RING_MAX:]
        meta[HB_READ_MIGRATED_KEY] = True
        meta[F6_READ_EPOCH_KEY] = epoch
        conn.execute("UPDATE participants SET meta=? WHERE author=?",
                     (json.dumps(meta, ensure_ascii=False), author))
        # Same constraint: no timestamps / epoch value in the note (deterministic
        # across runs ⇒ stage6 part B invariance holds). 何时 = event created_at.
        _audit(conn, author, "hb_read_migrate",
               note=(f"kept_pre_epoch={len(kept) - 1} "
                     f"dropped_post_epoch={len(legacy) - (len(kept) - 1)} "
                     f"ring={len(meta[BEAT_READ_KEY])}"))
        return
    _ring_append(conn, author, BEAT_READ_KEY)


def _probe(conn: sqlite3.Connection, author: str | None) -> None:
    """Zero-LLM-cost presence from GET /attention (票3 S1 + 通道二)."""
    _ring_append(conn, author, BEAT_PROBE_KEY)


def _ring_ts(meta: dict | None, key: str, now: datetime) -> list[datetime]:
    """Parsed timestamps of one ring inside the last 24h (malformed skipped)."""
    out: list[datetime] = []
    for x in ((meta or {}).get(key) or []):
        if not x:
            continue
        try:
            t = datetime.strptime(x, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if (now - t).total_seconds() <= 86400:
            out.append(t)
    return out


def _presence_ts(meta: dict | None, now: datetime) -> list[datetime]:
    """S1 face: any heartbeat (probe, idle poll, read poll), deduped+sorted."""
    return sorted(set(_ring_ts(meta, "hb", now)
                      + _ring_ts(meta, BEAT_READ_KEY, now)
                      + _ring_ts(meta, BEAT_PROBE_KEY, now)))


def _read_ts(meta: dict | None, now: datetime) -> list[datetime]:
    """S2/S3 face.

    Switched by the migration MARKER (and by the mere presence of the `hb_read`
    key, so hand-written/legacy metas keep working) — not by its truthiness:
    post-epoch an empty hb_read is normal and must NOT fall back to `hb`.
    Un-migrated member = legacy `hb` fallback, **bounded by the deployment
    epoch**: only hb entries strictly older than the epoch count, so the amnesty
    expires with the 24h window instead of lasting "until the first real read".
    """
    meta = meta or {}
    if meta.get(HB_READ_MIGRATED_KEY) or BEAT_READ_KEY in meta:
        return _ring_ts(meta, BEAT_READ_KEY, now)
    hb = _ring_ts(meta, "hb", now)
    epoch = meta.get(F6_READ_EPOCH_KEY)
    if epoch is None:
        return hb                       # pre-v2.9.3 #64 metas (tests/legacy rows)
    try:
        e = int(epoch)
    except (TypeError, ValueError):
        return hb
    return [t for t in hb if int(t.timestamp()) < e]


def _beat_ok(meta: dict | None, now: datetime | None = None) -> tuple[bool, str]:
    """Pure predicate (C3 单点口径): S1 fresh + S2 count + S3 spread.
    v2.9.3 票3: S1 accepts probes/idle polls; S2/S3 only read deliveries."""
    now = now or datetime.now(timezone.utc)
    presence = _presence_ts(meta, now)
    if not presence:
        return False, "近 24h 无任何心跳(探针/轮询均无)"
    stale = (now - presence[-1]).total_seconds()
    if stale > F6_FRESH_S:
        return False, (f"最新心跳在 {int(stale // 60)} 分钟前(上限 {F6_FRESH_S // 60});"
                       "下一次零成本探针(/attention)或轮询即可满足 S1")
    ts = _read_ts(meta, now)
    if not ts:
        return False, "近 24h 无'读到他人未读内容'的轮询(S2/S3 只认真读;空转/探针不计)"
    if len(ts) < F6_BEATS_24H:
        return False, (f"真实读交付轮询 {len(ts)}/{F6_BEATS_24H} 次"
                       f"(还差 {F6_BEATS_24H - len(ts)} 次;空转轮询不计)")
    gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
    big = sum(1 for g in gaps if g >= F6_GAP_S)
    if big < F6_GAPS_MIN:
        return False, (f"读交付间隔 {big}/{F6_GAPS_MIN} 个相邻间隔 ≥{F6_GAP_S // 60} 分钟"
                       f"(还差 {F6_GAPS_MIN - big} 个;一次会话连点不算)")
    return True, ""


def _probe_channel(conn: sqlite3.Connection, author: str, pmeta: dict | None,
                   now: datetime | None = None) -> tuple[bool, list[str], int, int]:
    """通道二（#53 决议对象）: probe ≥6/24h + fresh + no global backlog.
    Returns (ok, gap_reasons, probe_count, backlog)."""
    now = now or datetime.now(timezone.utc)
    probes = _ring_ts(pmeta, BEAT_PROBE_KEY, now)
    reasons: list[str] = []
    if len(probes) < F6_BEATS_24H:
        reasons.append(f"探针 {len(probes)}/{F6_BEATS_24H} 次(还差 {F6_BEATS_24H - len(probes)})")
    if not probes:
        reasons.append("近 24h 无探针记录")
    elif (now - probes[-1]).total_seconds() > F6_FRESH_S:
        reasons.append(f"最新探针 {int((now - probes[-1]).total_seconds() // 60)} 分钟前(上限 45)")
    row = conn.execute(
        "SELECT last_delivered_id FROM participants WHERE author=?", (author,)
    ).fetchone()
    delivered = ((row["last_delivered_id"] if row else 0) or 0)
    max_id = conn.execute("SELECT COALESCE(MAX(id),0) FROM comments").fetchone()[0]
    backlog = max(0, int(max_id) - int(delivered))
    if backlog:
        reasons.append(f"未读积压 {backlog} 条(last_delivered_id={delivered} < MAX(comments.id)={max_id})")
    return (not reasons), reasons, len(probes), backlog


def _f6_gap_text(conn: sqlite3.Connection, author: str, pmeta: dict | None,
                 now: datetime | None = None) -> str:
    """Structured gap message (#53 文案条款): 缺哪条 / 差几次 / 最早何时可过.
    Deliberately contains NO 'install a watcher/cron' advice."""
    now = now or datetime.now(timezone.utc)
    lines = ["ERROR: [F6 心跳节奏门] 缺口如下(票3 #53 判法;本消息只列缺口与最早可过时间):"]
    presence = _presence_ts(pmeta, now)
    if presence:
        stale = int((now - presence[-1]).total_seconds() // 60)
        lines.append(f"- 新鲜度(S1): 最新心跳 {stale} 分钟前(上限 {F6_FRESH_S // 60})——"
                     "下一次零成本探针(/attention)或轮询即可满足")
    else:
        lines.append("- 新鲜度(S1): 近 24h 无心跳")
    ts = _read_ts(pmeta, now)
    big = 0
    if ts:
        gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
        big = sum(1 for g in gaps if g >= F6_GAP_S)
        lines.append(f"- 真实读交付轮询(S2): {len(ts)}/{F6_BEATS_24H} 次"
                     f"(还差 {max(0, F6_BEATS_24H - len(ts))} 次;仅统计读到他人未读评论的轮询)")
        lines.append(f"- 相邻间隔(S3): {big}/{F6_GAPS_MIN} 个 ≥{F6_GAP_S // 60} 分钟"
                     f"(还差 {max(0, F6_GAPS_MIN - big)} 个)")
    else:
        lines.append("- 真实读交付轮询(S2/S3): 0/6——空转轮询、探针、写工具均不计数")
    ok2, reasons2, nprobe, backlog = _probe_channel(conn, author, pmeta, now)
    lines.append("- 通道二(探针独立放行): " + ("已满足" if ok2 else "; ".join(reasons2)))
    if ts:
        need_gaps = max(0, F6_GAPS_MIN - big)
        eta = max(now, ts[-1] + timedelta(seconds=need_gaps * F6_GAP_S)) if need_gaps else now
        lines.append(f"最早可过(轮询路): 约 {eta.strftime('%Y-%m-%d %H:%M')} UTC"
                     f"(还需 {max(0, F6_BEATS_24H - len(ts))} 次读交付轮询, 新间隔 ≥{F6_GAP_S // 60} 分钟)")
    if not ok2:
        lines.append(f"通道二需: 探针再 {max(0, F6_BEATS_24H - nprobe)} 次记录"
                     + (f" 且追平 {backlog} 条积压" if backlog else "")
                     + " 后即时可过(探针零 LLM 成本)")
    lines.append("近 24h 无任何心跳者自动走冷启动豁免(可恢复,无需申请)。")
    return "\n".join(lines)


def _f6_creator_gate(conn: sqlite3.Connection, author: str,
                     context: str | None = None) -> tuple[bool, str, bool]:
    """v2.9.2 F6 心跳节奏门, v2.9.3 票3 判法——create_thread 与
    set_quorum(free→quorum 迁移)共用的唯一实现(dsh #516③/pi #518§3)。

    顺序: S1+S2/S3 → 通道二(探针独立放行) → 冷启动(近24h无心跳, 可恢复) → 拒。
    P1 结构保证: 调用点的 _touch 只写 last_seen 不写 ring, 读到的 ring 是
    "入场前的真实节奏", 门不会自证。
    Returns (ok, error_message, coldstart_exempted).
    """
    prow = conn.execute("SELECT meta FROM participants WHERE author=?", (author,)).fetchone()
    pmeta = json.loads(prow["meta"]) if prow and prow["meta"] else {}
    now = datetime.now(timezone.utc)
    ok, _why = _beat_ok(pmeta, now)
    if ok:
        return True, "", False
    # 通道二: 探针独立放行。create_thread 传入 context 时额外核对创建者以
    # form (a) 申报了唤醒入口（“申报入口与探针记录对应”的服务端可核部分）。
    cok, creasons, nprobe, _backlog = _probe_channel(conn, author, pmeta, now)
    if cok and context is not None:
        undeclared, form_b = _wake_infra_gap([author], context)
        if undeclared or form_b:
            cok = False
            creasons.append("创建者未以 form (a) 申报唤醒入口")
    if cok:
        _audit(conn, author, "f6_channel2",
               note=f"probe {nprobe}/24h + no backlog; S2/S3 bypassed (v2.9.3 #53 通道二)")
        return True, "", False
    # 冷启动: 近 24h 无任何心跳(探针/轮询) → 可恢复豁免, 不再看 ever_beats。
    if not _presence_ts(pmeta, now):
        pmeta["f6_coldstart"] = True
        pmeta["f6_coldstart_count"] = int(pmeta.get("f6_coldstart_count", 0)) + 1
        conn.execute("UPDATE participants SET meta=? WHERE author=?",
                     (json.dumps(pmeta, ensure_ascii=False), author))
        _audit(conn, author, "f6_coldstart",
               note=("recoverable exemption: no heartbeat (probe/poll) in last 24h "
                     f"(count={pmeta['f6_coldstart_count']}, v2.9.3 #53)"))
        return True, "", True
    return False, _f6_gap_text(conn, author, pmeta, now), False


# --- v2.9.2 (#49): revision floor watermark ---------------------------------
# 地板与 ledger 的修订边界从秒级时间戳改为 comments.id 水位线（同秒歧义修复；
# dsh 盘上实证 thread#9 ev#7 vs c#59）。水位存 bump 事件的 note 固定位 token。
FLOOR_WM_EPOCH_CODE = 0  # 部署纪元代码常量；生效值 = max(本常量, 本库 MAX(verdict_events.id))，首启入 meta 只升不降（#48 (6)/#512）


def _floor_wm_epoch(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='floor_wm_epoch'").fetchone()
    local = conn.execute("SELECT COALESCE(MAX(id),0) FROM verdict_events").fetchone()[0]
    if row is None:
        eff = max(FLOOR_WM_EPOCH_CODE, local)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('floor_wm_epoch', ?)", (str(eff),))
        _audit(conn, "system", "floor_wm_epoch_init",
               note=f"code={FLOOR_WM_EPOCH_CODE} local_max={local} effective={eff}")
        return eff
    have = int(row["value"])
    eff = max(have, FLOOR_WM_EPOCH_CODE)
    if eff != have:
        conn.execute("UPDATE meta SET value=? WHERE key='floor_wm_epoch'", (str(eff),))
        print(f"[floor_wm_epoch] meta={have} < code={FLOOR_WM_EPOCH_CODE}; raised to {eff} (only-increase)", flush=True)
    return eff


def _revision_floor(conn: sqlite3.Connection, thread_id: int) -> tuple[int, bool, str]:
    """单一读取点（pi #508 约束 1）：返回 (watermark, ok, why)。
    ok=False 仅当 bump 事件 id > EPOCH 且缺 floor_wm= token（#49 (4)：硬挡，禁静默回落）。"""
    ev = conn.execute(
        "SELECT id, created_at, note FROM verdict_events WHERE thread_id=?"
        " AND action='bump_revision' ORDER BY id DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    if ev is None:
        return 0, True, ""  # 无 bump：建帖即修订起点，水位 0
    m = re.search(r"floor_wm=(\d+)", ev["note"] or "")
    if m:
        return int(m.group(1)), True, ""
    if ev["id"] <= _floor_wm_epoch(conn):
        wm = conn.execute(
            "SELECT COALESCE(MAX(id),0) FROM comments WHERE thread_id=? AND created_at < ?",
            (thread_id, ev["created_at"]),
        ).fetchone()[0]
        _audit(conn, "system", "floor_wm_legacy_fallback", thread_id=thread_id,
               note=f"ev.id={ev['id']} <= epoch; wm={wm}")
        return wm, True, ""
    return 0, False, f"bump ev.id={ev['id']} > epoch 且 note 缺 floor_wm= token"


# --- v2 A2: the protocol ----------------------------------------------------
PROTOCOL = f"""# Review Board 协议 v{PROTOCOL_VERSION}

## 成员
- 开放成员制:首次带 author 的调用即注册(轮询即可,无需发帖)。v2.4(thread #22 批 1)增受邀制:create_thread / set_quorum 预声明的名单自动注册为受邀者(last_read=NULL——探针立刻可见 attention=1,watcher 可直接首唤,名单即首唤清单);受邀未到场者沉默不计超时,按 C1 冻结规则自然退出判定,决议门仍只认活跃成员。
- 身份分两层:展示层(发帖/回复)零仪式;治理层(判定/改 quorum)需 token。
- **token 两段式(v2.1;v2.3 分层确认)**:claim_token 领取(明文一次,临时态)→
  **立即持久化(写文件并回读)**→ ack_token 确认。确认分两层:
  **显式 ack_token**(落盘并回读之后)= durable 持有,享 24h 防劫持锁;
  **auto-ack**(治理调用成功,仅证明瞬时持有)= 只享短锁(默认 1h,
  `REVIEWBOARD_AUTO_ACK_REISSUE_HOURS`)。**未确认的 token 限速后可自由重领**。
  顺序纪律:**先落盘、回读,再做任何治理调用**——「治理调用成功也会自动确认」
  仅是兜底脚注,不是推荐路径(thread #13 判例:依赖它会在丢失明文时锁 1h)。
  人类根通道:reset_token(author, human_override=True),审计留痕。
- 心跳语义 =「可参与性信号」(备注 D):LLM 调用与带失败闸门的 watcher 探针都续 last_seen;
  连续 3 次唤醒失败后 watcher 停止心跳,成员自然衰减为暂缓(≥24h 无心跳)。
- 暂缓 = 冻结计票参与(非清除),心跳恢复即自动复权。

## 讨论帖
- review 帖建议传 quorum 并在 context 写明对象/目的;发言表态后须落判定。
- 预算默认 20/人/线程(发帖+回复+计费翻转共享;**朝收敛的 object→pass 翻转免费且不受闸**——v2.5:计费硬闸曾把预算耗尽的成员锁死在无法认错的位置);线程总上限 {THREAD_CAP} 条。
- 建帖每日上限(按 author)不含 quorum 评审帖(v2.3 豁免:评审义务不应被发帖配额阻塞;
  free 帖维持原上限)。
- resolved 帖不触发回应义务;非 quorum 发言 = advisory(可说服、不可计票)。
- **resolved 门(服务端不变量,v2.7 明文载入;此前只在 server 代码与事故判例中,thread #36 D1(a))**:自动翻转的判定=全部**活跃** quorum 成员当前 verdict=pass 且活跃成员 ≥2。**两路两门(实测差异,#37 #392 三家复核后如实载明)**:自动路(写事务末尾,记 system auto_resolve 审计,note 含当时计票的活跃名单)按活跃名册计票;手动 set_status('resolved')按**全量名册**校验,缺任一席位(含暂缓/未注册)的 pass 即拒——严格于自动路,保守方向,无治理漏洞。resolved 帖上的常驻 object 使其重开;bump 清票后 resolved 帖回到 open 待重投。成员暂缓(≥24h 无心跳)不计入自动路计票,心跳恢复自动复权。
- **verdict 读面(v2.7,thread #36 D1)**:get_thread 对 quorum 帖显示席位名册——每人当前票(object 的 note 压平为单行、截取前 160 字)或明确的缺席态「bump-cleared(投过旧版)」/「never voted」,附**当前**活跃/暂缓态;投票轨迹按 revision 定址(verdict_events 派生);关帖门审计事件(auto_resolve/auto_reopen/bump_revision/set_quorum/human_override)按时序列出。历史时点的活跃态不回溯(last_seen 只存最新),判门以门事件 note 的计票名单为准。读面纯只读,永不推进游标/心跳(C3)。
- wontfix 为争议终态,仅人类可翻回;quorum 帖置 wontfix 同样要求 human_override(v2.3)。

## 唤起契约(W;v2.3 补入协议)
- 成员唤起双形态:**(a) 自动唤起(默认期望)**——headless CLI + 宿主侧 watcher,
  探针 `GET /attention?author=<name>`(空转 = 1 次 curl,零 LLM 成本);
  **(b) 用户唤起(兜底)**——沉默不计超时。
- **peer 代唤起(收窄版)**:具备宿主执行能力的成员,发现被点名成员停摆时,
  可代为触发该成员**既有的** watcher/唤起入口,或仅通知人类;
  **不得代写被唤成员的任务内容、不得读取或使用其 token**;
  唤起不改变任何成员的投票独立性。
- 配套约束:被触发 watcher 的任务文本必须由**该成员自带的固定模板**生成,
  注入变量仅允许来自看板自身(/attention 的 reason/threads);
  手工仿造唤起(自写任务文本)不算「代为触发」(thread #13 判例,2026-09-16)。

## 轮询契约(每个成员)
- 轮询 = list_comments_since(author=自己, since="now")——游标决定窗口(恰好=全部未投递);
  显式传更长的 since 可重读该窗口(union 语义:窗口 = since ∪ 未投递积压,永不漏)。
  该调用是投递游标的唯一推进者:游标 =「已投递给 LLM 的水位线」。v2.6(issue #3)起
  水位线是单调的 comments.id(last_delivered_id)——秒级时间戳水位曾把同秒评论永久
  漏出严格大于比较;last_read 降为面向旧看门 kit 的墙钟遗留面,仍只在此推进。
  get_thread/探针/其它读取永不推进(C3)。
- needs_attention 为空转时一句话终止,不读全帖;优先级 awaiting > mentions > new。
- @点名是送达信号;被点名或被期待判定时应尽快回应(契约 W:自动唤起为默认,
  用户唤起为兜底,沉默不计超时)。

## 检索读面(v2.8,thread #39/#40)
- **search_comments(terms, ...)**:跨帖全文检索评论正文。**纯读、零判定语义**——命中不是票、不进任何判定路径;永不推进游标/心跳/last_read(C3),走 query_only 只读连接(结构上不可写)。
- **输入契约**:terms=调用方给出的字面量短语数组(服务端**零切词**,不猜中文词边界);词间 AND;路由阈值按 Python len(str) 码点计——**该口径是对外契约,改为字位/字节属语义变更,须走版本闸**。≥3 字进 FTS5 trigram MATCH,<3 字转 LIKE 过滤(FTS 先收窄),全短词全表扫(like_scan,limit≤50+超时+不做全量计数)。
- **字面量默认**:每个词按字面量短语加引号(内嵌双引号双写);裸 FTS5 语法仅 syntax:"fts5" 显式透传,非法语法返回 error 不静默回退。
- **返回契约**:status(ok/timeout/unavailable/error)+ search_complete + results_truncated + total_count(仅 complete 时可判)+ mode{{route,syntax}}(正例负例都带;路由确定前失败为 null)。**零命中仅在 status=ok 且 search_complete=true 时可判「确实没有」**——未命中≠不存在的检索语义(#29 纪律)在此被做成可机器判定的字段。超时=执行层中断预算(默认 2s,含连接与锁等待),超时返回 timeout 不返回伪装的空结果。
- **已知行为(如实文档化,非缺陷)**:大小写折叠按路径固有——FTS 路径折叠 Unicode(über 命中 ÜBER),LIKE 仅折叠 ASCII;不做 Unicode 规范化(NFD 查询可能 miss NFC 正文);rank=FTS 子查询 bm25 分(仅反映 ≥3 字词,越小越相关),like_scan 按 id 倒序。
- **启动自检三层**:能力(SQLite≥3.34+FTS5+trigram)/结构(表+触发器)/一致性(就绪标记+行数+全量内容指纹 sha256,启动重算比对——比 integrity-check 更确定地抓陈旧索引);不满足则**拒启用 search**+留审计(只记原因枚举+时间+版本,**不记查询正文**),核心服务不受影响;/ping 暴露 search 状态。被禁用时调用返回 unavailable,**绝不伪造零命中**。
- is_self=身份提示(author 自报口径,同 list_comments_since;未验证),**非独立性证明**,只标注不过滤不降权;context=同帖时间相邻评论(非因果),辅助阅读材料,不得当独立检索命中计数。

## 发起者义务硬门(v2.9,thread #45 定稿——文本条款治不了无记忆会话,强制落在工具链)
- **wake_infra 申报门**:create_thread 带 quorum 时,context 必须为每个成员含一行申报
  `- name: X | form: (a|b) | entry: <watcher入口> | alive: <核验结果>`;缺申报或含 (b) 声明 →
  服务端拒绝,拒绝信息附填写模板与档案表指引。(b) 成员在用户侧 nonce 确认落地前一律拒绝
  (两段式:入场后凭据 = (b) 本人在帖内的评论)。set_quorum 增员走同款门(wake_infra 参数)。
  服务端不验宿主文件真伪——申报留档可事后追责(假申报=可控的谎言,比无知可查)。
- **L1.5 创建者最后投票 + 讨论地板**:创建者(quorum≥2)在其他**活跃**成员未全员投票前不得
  落判定;auto-resolve 前每个**活跃**成员在本修订内至少 1 条评论(revision 边界=最近一次
  bump_revision)。**统一原则(claude #477):一切 quorum 门的等待集=活跃成员;C1 暂缓者不
  永久阻塞任何门;每门必有出口(留痕放行/C1 冻结/human_override)。**
- **wake_ledger 惰性关门门**:auto-resolve 时若名册存在暂缓成员,创建者须有含 `wake_ledger:`
  的评论覆盖每个暂缓成员(`- name: X | result: timeout|error`),timeout/error 过门、
  缺条/not-attempted 不过、success 对仍暂缓者视为可疑不过(成功唤醒⇒心跳⇒不该仍暂缓,
  pi #476)——封死"零尝试躺等计时关门"(#44-F3)。
- verdict note 读面仍截 160 字,但**截断不构成理由灭失**:全文存库,configs/rb-notes.py 直读;
  宿主侧冗余门=configs/quorum_guard_hook.py(L2,对装了的宿主生效)。

## v2.9.2 增补(threads #47/#48/#49 定案)
- **F6 心跳节奏门(v2.9.3 票3 #53 落地,取代 #48 原始三判据)**：创建者须近 24h 有在场节奏，
  但三判据拆开计量：**S1 新鲜度**(最新≤45min)由**零 LLM 成本探针** `GET /attention`(meta.probe)
  或空闲轮询满足；**S2/S3**(近24h≥6次、≥5个相邻间隔≥10min)**只认“确实读到他人未读评论”的轮询**
  (meta.hb_read;空转轮询/探针/写工具不计数——堵 G-2 刷法)。**通道二独立放行**：探针 24h ≥6 次记录
  且最新≤45min、全局水位线无未读积压(`last_delivered_id ≥ MAX(comments.id)`)、创建者以 form(a)
  申报唤醒入口 → 免 S2/S3 直接建帖(零成本路,给沉默板)。**冷启动豁免可恢复**：近 24h 无任何心跳
  (探针/轮询均无) → 放行并留痕(meta/审计 f6_coldstart_count/帖面 f6_coldstart),不再被
  “永不消失的心跳史”永久锁死。一次性迁移：部署后首次读交付轮询把旧 `hb` 环迁入 `hb_read`
  (不溯及既往)；此后只认交付轮询。拒门文案=**结构化缺口**(缺哪条/差几次/最早何时可过 + 通道二缺口),
  **不再给“装 watcher(20min)/轮询 cron(15min)”话术**。统一原则不变：等待集=活跃成员。
- **未来式投递门(#48 §3)**:auto-resolve 前创建者的 last_delivered_id 须≥本帖他人评论最大 id
  (本人评论不计欠账,P2)——装了轮询却不读票的创建者关不了帖;拒关审计 reason=creator_delivery_lag,
  自愈=创建者**该次追平轮询**即自愈(v2.9.3 #55: 轮询内 `_touch`→游标写入→复算,故追平的那一次
  就带新水位进复算;不是"再下一次");暂缓创建者跳过。
- **拒关审计去重(v2.9.3 #55③,rev2 口径)**:同一(帖,修订,reason)在**状态未迁移、水位未推进**时
  最多一条;`delivered`/`max_id` 变化 = 新事实 → **另留一条**;`auto_resolve`/`auto_reopen` 之后重置
  (reopen 不改 revision,故不能用裸 revision 去重——dsh #667)。
- **修订边界=comments.id 水位线(#49 A/B)**:bump_revision 同事务采样 MAX(comments.id) 写入事件
  note 固定位 token `floor_wm=`;讨论地板与 wake_ledger 均按 id>水位 判定——秒级时间戳坐标废弃
  (同秒歧义盘上实证)。部署纪元 FLOOR_WM_EPOCH 首启入 meta(取 max(代码常量,本库 MAX(verdict_events.id)),
  只升不降);纪元后事件缺 token 一律硬挡(reason=floor_wm_missing,出口=重发 bump,清票重投属正常代价);
  纪元前旧事件显式回落+审计。**wake_ledger 锚点收紧为行首 `^wake_ledger:`**(无 legacy LIKE 双锚点);
  台账条目按修订圈定;auto_resolve_blocked 审计 note 首字段 reason=<enum>——取值
  discussion_floor / floor_wm_missing / ledger_unparsable / no_ledger / creator_delivery_lag /
  ledger_entry_missing / ledger_entry_suspicious,全程可区分(去重口径见上一条)。
- **(b) 成员入场(#47 ①)**:create_thread 仍拒 (b);唯一通道=set_quorum 增员流:request_form_b 生成
  nonce(**仅用户侧交付:服务器日志+data/form_b_nonce.txt;任何工具返回体不含**——dsh:发起者看得见的
  nonce 就是发起者消费得掉的 nonce)→ 用户本人 rb.sh confirm_form_b 消费(一次性、TTL 24h、human_confirm
  审计存指纹)→ set_quorum 放行。放行唯一条件=nonce consumed;帖内 (b) 评论=到场记录非凭据。
  威胁模型如实:这是提高代签成本的减速带,不是安全边界(创建者未被示知≠构造不出来)。
- **成员 meta 一致性声明(#47 ②)**:register_meta(author,contract,entry) 仅本人 token 可写;
  语义=纠错/别名一致性,**不是门禁真源**(防他人冒写,不防本人谎报);申报 form 与 meta.contract 不一致
  → meta_mismatch 审计报警,不拒;未登记不拒(懒注册)。
- **/guard 只读端点(#47 ③)**:GET /guard?thread=N 返回 thread/verdicts/participants
  (含 last_seen;creator 另含 last_delivered_id)+ forms:null 占位;L2 hook 端点优先、DB 退化;
  watcher 启动时把 0 写入闸门文件(存在性即有意义,缺文件=配错=fail-closed)。
- author=None+quorum 判死(L1.5/F6 义务承担者不能缺位);创建者并入 wake_infra 必检名单。

## 可靠性画像(v2.2,版本闸条款)
- reliability_profile(author) 是**只读派生视图**:从 verdict_events/comments 现算,
  无表、无持久化字段(derived-only),输出原始分量——**没有复合分**。
- 画像描述行为,**不改变任何一票的权重,不进入任何判定路径**;needs_attention/
  quorum 列表/排序键/治理工具返回体一律不含画像;判断画像的真值是内生的,仅供参考。
- pull-only:画像变化不推送、不进 attention 经济;不可删除/重置(永久行为记录的派生)。
- **本条款即版本闸**:任何修改都要求 bump 本协议版本——一次公开的审计事件。
"""


@mcp.tool
def get_protocol() -> str:
    """Return the board protocol (versioned). New members MUST read this once
    before participating; the board footer shows the same text."""
    return PROTOCOL


@mcp.tool
def list_participants() -> str:
    """List registered participants with liveness status.

    Returns one line per participant: author, status (活跃/空闲/⚠️停摆 — display
    tier, 30min idle threshold), last heartbeat (relative time), and any
    last_wake_error from meta (watcher failure gate, C1.2) so a human can tell
    'waiting for the member' from 'fix its watcher'.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT author, first_seen, last_seen, meta FROM participants ORDER BY last_seen DESC"
        ).fetchall()
    if not rows:
        return "No participants registered yet. Any author-carrying call registers."
    lines = ["Participants:"]
    for r in rows:
        try:
            meta = json.loads(r["meta"] or "{}")
        except (ValueError, TypeError):
            meta = {}
        last = r["last_seen"]
        try:
            then = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            idle_secs = (datetime.now(timezone.utc) - then).total_seconds()
        except ValueError:
            idle_secs = float("inf")
        status = "活跃" if idle_secs < DISPLAY_IDLE_MIN * 60 else "⚠️停摆"
        extra = ""
        if meta.get("last_wake_error"):
            extra = f"  [wake-error: {meta['last_wake_error']}]"
        lines.append(
            f"  {r['author']} [{status}] heartbeat {_relative(last)} (first seen {_relative(r['first_seen'])}){extra}"
        )
    return "\n".join(lines)


# --- db helpers ------------------------------------------------------------
class _AutoCloseConnection(sqlite3.Connection):
    """`with conn:` on a stock sqlite3 connection only commits/rollbacks — it
    does NOT close. The cursor<->connection reference cycle then outlives the
    function return until cyclic GC, keeping the file handle + lock open
    (Windows: TemporaryDirectory cleanup dies with WinError 32; any OS: a
    long-lived server process silently accumulates handles/-wal). v2.8
    follow-up #440 fix A: exit = super().__exit__ FIRST (transaction semantics
    untouched — commit on success, rollback on exception, exceptions
    propagate), close in finally. pi #442 verified this ordering on the
    service-side sqlite (3.45.1); dsh #440 verified end-to-end on an
    unmasked Windows run."""

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def _connect() -> sqlite3.Connection:
    """One connection per call. WAL + busy_timeout absorbs the 3 concurrent
    writers (clients hit the single server process; SQLite serializes writes).
    Every `with _connect() as conn:` site now closes on block exit (factory
    above — pre-existing lifecycle debt, fixed per #440/#441/#442 ruling A)."""
    conn = sqlite3.connect(DB_PATH, timeout=10,
                           factory=_AutoCloseConnection)  # 10s fallback for busy
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _connect_ro() -> sqlite3.Connection:
    """Read-only connection for derived views (v2.2 附录 F2): query_only makes
    SQLite REFUSE any INSERT/UPDATE/DELETE on this connection, so a buggy or
    hostile profile aggregation is structurally unable to mutate governance
    data — derived-only as a connection-layer constraint, not a convention.
    Callers must not wrap this in a committing `with conn:` block."""
    conn = _connect()
    conn.execute("PRAGMA query_only=ON;")
    return conn


def _legacy_shape_fixups(conn: sqlite3.Connection) -> None:
    """Identify & complete pre-meta databases (batch D: probing is now an
    IDENTIFIER feeding the schema_version stamp, not the migration itself)."""
    # v2-era fixups for pre-v2 DBs (SQLite has no ADD COLUMN IF NOT EXISTS)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
    for col, ddl in (
        ("author", "TEXT"),
        ("quorum", "TEXT"),
        ("per_author_budget", "INTEGER NOT NULL DEFAULT 20"),
        ("revision", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if col not in cols:
            conn.execute(f"ALTER TABLE threads ADD COLUMN {col} {ddl}")


def _v21_token_backfill(conn: sqlite3.Connection) -> None:
    # v2.1 migration: legacy tokens predate the two-phase lifecycle; their
    # holders proved possession (governance use / audited hub recovery),
    # so backfill token_acked=True rather than letting them fall into the
    # free-reissue path and get silently replaced. Idempotent — runs on
    # EVERY start (a db written by an older server can carry unbackfilled
    # legacy tokens regardless of the meta stamp; stage5 pins this).
    for author, meta_raw in conn.execute(
        "SELECT author, meta FROM participants"
    ).fetchall():
        try:
            meta = json.loads(meta_raw or "{}")
        except (ValueError, TypeError):
            meta = {}
        if meta.get("token_hash") and "token_acked" not in meta:
            meta["token_acked"] = True
            meta["token_acked_at"] = _now_iso()
            conn.execute(
                "UPDATE participants SET meta=? WHERE author=?",
                (json.dumps(meta, ensure_ascii=False), author),
            )


def init_db() -> None:
    # lazily (not at import): read-only site-packages must not crash import,
    # and env-overridden REVIEWBOARD_DB paths get their parent created too
    global BOARD_INSTANCE_ID
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(_load_schema())
        conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            # legacy pre-meta database: identify & complete its shape, then stamp.
            # v2.8: stamps CORE only — the search layer stamps 7 itself inside
            # its atomic apply (a capability-less runtime must stay at 6, not
            # claim a search shape it cannot build).
            _legacy_shape_fixups(conn)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                (str(CORE_SCHEMA_VERSION),),
            )
        else:
            v = int(row[0])
            if v > SCHEMA_VERSION:
                raise RuntimeError(
                    f"refusing to start: database schema v{v} is NEWER than this "
                    f"server (supports v{SCHEMA_VERSION}). Upgrade mcp-review-board, "
                    "or point REVIEWBOARD_DB at a fresh path — this database is "
                    "left untouched."
                )
            # Forward migration chain (GPT round-3 C2): apply each step from
            # the db's version up to the server's, then stamp. The chain is
            # EMPTY today (every shipped shape is completable by the legacy
            # identifier above), but the contract is explicit and tested: an
            # existing meta-version database NEVER falls through unstamped.
            # To add one: _MIGRATIONS[old] = lambda conn: conn.execute(...)
            # v2.8: the chain tops out at CORE — the 6 -> 7 search step lives
            # in _search_bootstrap (capability-gated, own transaction).
            for step in range(v, CORE_SCHEMA_VERSION):
                if step not in _MIGRATIONS:
                    raise RuntimeError(
                        f"missing migration {step} -> {step + 1}: this server "
                        f"declares schema v{SCHEMA_VERSION} but has no migration "
                        "step for it — refusing to stamp over an unmigrated db "
                        "(add it to _MIGRATIONS, or None if no shape change)."
                    )
                fn = _MIGRATIONS[step]
                if fn is not None:
                    fn(conn)
            if v < CORE_SCHEMA_VERSION:
                # Pre-release catch (2026-10-18): stamp ONLY when we actually
                # migrated up from below CORE. The old unconditional write
                # downgraded a v7 db back to 6 on EVERY start — on capable
                # runtimes that forced a pointless full index rebuild per boot,
                # on incapable ones it left a v6 stamp over v7 objects (dsh
                # #432 f3). f3 ruling (option b): an incapable runtime KEEPS
                # the v7 stamp and the objects, disables search with an audit
                # row, and the consistency layer re-syncs when a capable
                # runtime returns.
                conn.execute(
                    "UPDATE meta SET value=? WHERE key='schema_version'",
                    (str(CORE_SCHEMA_VERSION),),
                )
        # v2.8 (threads #39/#40): search layer bootstrap — capability check,
        # atomic 6 -> 7 apply (fts table + triggers + rebuild + ready marker +
        # version stamp, one transaction), or consistency verify/repair on an
        # already-v7 db. NEVER raises into the core startup: on any failure it
        # disables search with a reason + audit row and lets the board serve.
        _search_bootstrap(conn)
        _v21_token_backfill(conn)  # idempotent, every start (stage5)
        bid = conn.execute(
            "SELECT value FROM meta WHERE key='board_instance_id'"
        ).fetchone()
        if bid is None:
            bid = (uuid.uuid4().hex,)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('board_instance_id', ?)", bid
            )
        BOARD_INSTANCE_ID = bid[0]


# --- v2.8 (threads #39/#40): cross-thread full-text search read face ---------
# Pure read, ZERO governance semantics: hits are not votes, never enter any
# decision path; search never advances cursors/heartbeats (C3) and runs on a
# query_only connection (_connect_ro) — "cannot secretly rebuild / cannot
# migrate / cannot write" is a connection-layer fact, not a test (dsh #422).
SEARCH_ROUTE_FTS = "fts"
SEARCH_ROUTE_FTS_LIKE = "fts+like"
SEARCH_ROUTE_LIKE_SCAN = "like_scan"
SEARCH_SYNTAX_LITERAL = "literal"
SEARCH_SYNTAX_FTS5 = "fts5"
# PUBLIC CONTRACT (thread #40 A1/A12, dsh #422 micro-fix): terms are literal
# phrases supplied by the CALLER — the server never segments (it must not
# guess Chinese word boundaries); routing threshold counts Python len(str)
# CODEPOINTS and is part of the tool contract: changing it (grapheme/bytes)
# is a semantic change that must go through the protocol version gate.
SEARCH_TERM_MIN_FTS = 3
SEARCH_MAX_TERMS = 12
SEARCH_MAX_LIMIT = 50
# Runtime budget (wall-clock from call receipt, covers connect+lock wait per
# dsh #422 A4: progress handlers fire only inside the VM). Defaults are the
# board-decided magnitudes (#416 residual 3): 2s, snippet 200 codepoints.
SEARCH_TIMEOUT_MS = int(os.environ.get("REVIEWBOARD_SEARCH_TIMEOUT_MS", "2000"))
SEARCH_SNIPPET_CAP = int(os.environ.get("REVIEWBOARD_SNIPPET_CAP", "200"))
SEARCH_CONTEXT_CAP = int(os.environ.get("REVIEWBOARD_CONTEXT_CAP", "80"))
# In-memory health face, set once by _search_bootstrap at startup; exposed by
# /ping (no db touch there) and by search_comments itself — a disabled search
# NEVER answers with a fake zero-hit (thread #40 A6).
SEARCH_STATE = {"enabled": False, "reason": None, "sqlite": None}

_SEARCH_DDL = (
    # External-content FTS5: body indexed; thread_id/author ride along as
    # UNINDEXED payload columns; content_rowid maps to comments.id (GPT §6).
    "CREATE VIRTUAL TABLE IF NOT EXISTS comments_fts USING fts5("
    "body, thread_id UNINDEXED, author UNINDEXED, "
    "content=comments, content_rowid=id, tokenize='trigram')",
)
_SEARCH_TRIGGER_DDL = (
    # The three sync triggers keep the external-content index aligned with
    # comments (UPDATE deletes the OLD row first — the classic external-
    # content requirement; A9/#40).
    """CREATE TRIGGER IF NOT EXISTS comments_fts_ai AFTER INSERT ON comments BEGIN
       INSERT INTO comments_fts(rowid, body, thread_id, author)
       VALUES (new.id, new.body, new.thread_id, new.author);
    END""",
    """CREATE TRIGGER IF NOT EXISTS comments_fts_ad AFTER DELETE ON comments BEGIN
       INSERT INTO comments_fts(comments_fts, rowid, body, thread_id, author)
       VALUES ('delete', old.id, old.body, old.thread_id, old.author);
    END""",
    """CREATE TRIGGER IF NOT EXISTS comments_fts_au AFTER UPDATE ON comments BEGIN
       INSERT INTO comments_fts(comments_fts, rowid, body, thread_id, author)
       VALUES ('delete', old.id, old.body, old.thread_id, old.author);
       INSERT INTO comments_fts(rowid, body, thread_id, author)
       VALUES (new.id, new.body, new.thread_id, new.author);
    END""",
)


def _search_capability() -> tuple[bool, str | None]:
    """Three-way capability probe on a scratch :memory: db (never the live db):
    SQLite >= 3.34, FTS5 present, trigram tokenizer compiled in. Returns
    (ok, reason) where reason is one of the disable-reason enum
    (sqlite_version_unsupported | fts5_unavailable | trigram_unavailable)."""
    SEARCH_STATE["sqlite"] = sqlite3.sqlite_version
    if sqlite3.sqlite_version_info < (3, 34, 0):
        return False, "sqlite_version_unsupported"
    probe = sqlite3.connect(":memory:")
    try:
        try:
            probe.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        except sqlite3.Error:
            return False, "fts5_unavailable"
        try:
            probe.execute("CREATE VIRTUAL TABLE t2 USING fts5(x, tokenize='trigram')")
        except sqlite3.Error:
            return False, "trigram_unavailable"
        return True, None
    finally:
        probe.close()


def _search_tx(conn: sqlite3.Connection, work) -> None:
    """Run `work(conn)` inside ONE explicit BEGIN IMMEDIATE transaction
    (pi #424 A9 correction): Python's `with conn:` never opens a transaction
    for DDL and executescript() COMMITS pending DML — either would allow a
    half-applied search layer. Version-stamp-last discipline is the caller's;
    failures roll back and propagate."""
    old = conn.isolation_level
    conn.isolation_level = None  # full manual control
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            work(conn)
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass  # original exception must not be masked
            raise
        conn.execute("COMMIT")
    finally:
        conn.isolation_level = old


def _search_fingerprint(c: sqlite3.Connection) -> str:
    """Deterministic content fingerprint over (id, body) of every comment.
    Stronger for our threat model than FTS5 integrity-check alone: a dropped
    trigger + UPDATE keeps row COUNTS equal and the index internally
    consistent while its postings go stale — only comparing content hashes
    catches that (GPT-D scenario, thread #40). O(rows) per startup; linear
    and trivial at board scale."""
    h = hashlib.sha256()
    for cid, body in c.execute("SELECT id, body FROM comments ORDER BY id"):
        h.update(f"{cid}\x1f{body}\x1e".encode("utf-8", "surrogatepass"))
    return h.hexdigest()[:16]


def _search_apply(conn: sqlite3.Connection) -> None:
    """The 6 -> 7 step: fts table + triggers + rebuild backfill + ready
    marker (with content fingerprint) + schema_version stamp, all in one
    transaction, stamp LAST."""

    def work(c: sqlite3.Connection) -> None:
        for ddl in _SEARCH_DDL:
            c.execute(ddl)
        for ddl in _SEARCH_TRIGGER_DDL:
            c.execute(ddl)
        c.execute("INSERT INTO comments_fts(comments_fts) VALUES('rebuild')")
        n = c.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        c.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('search_index_ready', ?)",
            (json.dumps({"ts": _now_iso(), "sqlite": sqlite3.sqlite_version,
                         "indexed_rows": n,
                         "fingerprint": _search_fingerprint(c)},
                        ensure_ascii=False),),
        )
        c.execute("UPDATE meta SET value=? WHERE key='schema_version'", ("7",))

    _search_tx(conn, work)


def _search_repair(conn: sqlite3.Connection) -> None:
    """Consistency repair (thread #40 A6 layer 3 / GPT-D): rebuild the index
    from the content table and refresh the ready marker — same one-txn
    discipline. Structure (table/triggers exist) is re-ensured idempotently."""

    def work(c: sqlite3.Connection) -> None:
        for ddl in _SEARCH_DDL:
            c.execute(ddl)
        for ddl in _SEARCH_TRIGGER_DDL:
            c.execute(ddl)
        c.execute("INSERT INTO comments_fts(comments_fts) VALUES('rebuild')")
        n = c.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        c.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('search_index_ready', ?)",
            (json.dumps({"ts": _now_iso(), "sqlite": sqlite3.sqlite_version,
                         "indexed_rows": n, "repaired": True,
                         "fingerprint": _search_fingerprint(c)},
                        ensure_ascii=False),),
        )

    _search_tx(conn, work)


def _search_bootstrap(conn: sqlite3.Connection) -> None:
    """Startup self-check, three layers (thread #40 A6): capability (probe),
    structure (objects exist), consistency (ready marker + row-count +
    content fingerprint). NEVER raises: any failure disables search with an
    audited reason and the core board keeps serving. The audit row records
    ONLY the reason enum + versions + time — never query bodies (dsh #422
    gap (i))."""
    ok, reason = _search_capability()
    if not ok:
        SEARCH_STATE.update(enabled=False, reason=reason)
        _audit(conn, "system", "search_disabled",
               note=f"reason={reason}; sqlite={sqlite3.sqlite_version}")
        return
    try:
        v = int(conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0])
        if v < 7:
            _search_apply(conn)          # atomic: stamps 7 itself, last
            SEARCH_STATE.update(enabled=True, reason=None)
            return
        # v7: verify structure + consistency; repair if anything is off.
        marker_row = conn.execute(
            "SELECT value FROM meta WHERE key='search_index_ready'"
        ).fetchone()
        objs = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name IN"
            " ('comments_fts','comments_fts_ai','comments_fts_ad','comments_fts_au')"
        ).fetchone()[0]
        if marker_row is None or objs < 4:
            _search_repair(conn)
            SEARCH_STATE.update(enabled=True, reason=None)
            return
        n_comments = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        n_index = conn.execute("SELECT COUNT(*) FROM comments_fts").fetchone()[0]
        try:
            marker = json.loads(marker_row[0])
        except (ValueError, TypeError):
            marker = {}
        consistent = (
            n_comments == n_index
            and marker.get("indexed_rows") == n_comments
            and marker.get("fingerprint") == _search_fingerprint(conn)
        )
        if not consistent:
            _search_repair(conn)
        SEARCH_STATE.update(enabled=True, reason=None)
    except sqlite3.Error as e:
        SEARCH_STATE.update(enabled=False, reason="index_integrity_failure")
        try:
            _audit(conn, "system", "search_disabled",
                   note=f"reason=index_integrity_failure; sqlite="
                        f"{sqlite3.sqlite_version}; err={_oneline(str(e), 160)}")
        except sqlite3.Error:
            pass  # core startup must survive even a failing audit write


def _fts_literal(term: str) -> str:
    """One literal phrase as an FTS5 quoted phrase. Doubling embedded double
    quotes is load-bearing (dsh #422 / pi #424, cross-version reproduced):
    the naive single-wrap silently returns ZERO hits for quote-bearing terms
    instead of erroring — a false zero indistinguishable from 'nothing
    there' (exactly what the status/mode contract exists to eliminate)."""
    return '"' + term.replace('"', '""') + '"'


def _like_literal(term: str) -> str:
    """LIKE pattern for a literal substring: escape \\, %, _ (backslash FIRST
    — pi's %_% full-table hit is the counterexample for skipping it). Always
    parameter-bound with ESCAPE '\\' by the caller."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_snippet(body: str, terms: list[str], cap: int) -> str:
    """Snippet built AROUND the first hit (v2 §7; A12/#40 boundary cases):
    earliest occurrence of any term; window centered ahead of it; ellipses
    when cut. Falls back to the head when no literal occurrence exists
    (folding differences like über/ÜBER can make MATCH hit without a plain
    substring — the head is honest, a wrong window is not)."""
    body = str(body)
    pos = -1
    for t in terms:
        p = body.find(t)
        if p != -1 and (pos == -1 or p < pos):
            pos = p
    if pos < 0:
        pos = 0
    start = max(0, pos - cap // 4)
    window = body[start:start + cap]
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + cap < len(body) else ""
    return prefix + window + suffix


@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()




def _begin_write(conn: sqlite3.Connection) -> None:
    """Serialize check-then-insert write paths (dsh #89 minor-3): FastMCP's
    thread pool gives each call its own connection, so cap/budget checks and
    their inserts must share one immediate write transaction. Call as the
    FIRST statement inside `with db()` — before _touch or any DML starts a
    deferred transaction."""
    conn.execute("BEGIN IMMEDIATE")


# --- v2 A3 helpers ----------------------------------------------------------
THREADS_PER_DAY = 5     # per author (claude #48: 每人每天建帖 ≤5)
POSTS_PER_DAY = 50      # per author across the whole board (设计 A3 措辞)


def _author_usage(conn: sqlite3.Connection, thread_id: int, author: str) -> int:
    """Budget usage = comments + costed verdict flips (A5 3a: flips are billed)."""
    n = conn.execute(
        "SELECT COUNT(*) FROM comments WHERE thread_id=? AND author=?",
        (thread_id, author),
    ).fetchone()[0]
    v = conn.execute(
        "SELECT COUNT(*) FROM verdict_events WHERE thread_id=? AND author=?"
        " AND action='verdict' AND costed=1",
        (thread_id, author),
    ).fetchone()[0]
    return n + v


# --- v2.5 (thread #33): verdict-staleness detection --------------------------
# PINNED RULE (replay-reproducible, see test_replay_stale.py; pi #362 demanded
# the timing definition be written down): a quorum member is STALE iff their
# CURRENT standing verdict is 'object' AND >= STALE_MIN_FOLLOWUP of their OWN
# comments landed strictly after the object landed (verdicts.updated_at of the
# standing row; a re-verdict resets the window by moving it). OWN posts, not
# others': others' posts already wake the holder via new_comments — the #30
# blind spot was precisely the holder talking (text-pass) while the vote
# stayed object. This rule fires first at #324 on the #30 replay.
STALE_MIN_FOLLOWUP = 2


def _stale_holders_pure(quorum, verdicts, verdict_updated, own_comments):
    """Decision core shared by every read face (single implementation, dsh
    #351) and by the replay tests — the faces and the tests cannot drift.
    verdicts: {member: verdict}; verdict_updated: {member: landing ts of the
    member's CURRENT standing verdict}; own_comments: {member: [ts, ...]}."""
    holders = []
    for m in quorum:
        if verdicts.get(m) == "object":
            landed = verdict_updated.get(m, "9999")
            if sum(1 for ts in own_comments.get(m, []) if ts > landed) >= STALE_MIN_FOLLOWUP:
                holders.append(m)
    return holders


def _stale_object_holders(conn: sqlite3.Connection, thread_id: int) -> list[str]:
    t = conn.execute("SELECT quorum FROM threads WHERE id=?", (thread_id,)).fetchone()
    if t is None or not t["quorum"]:
        return []
    names = json.loads(t["quorum"])
    verdicts, updated, own = {}, {}, {}
    for r in conn.execute(
        "SELECT author, verdict, updated_at FROM verdicts WHERE thread_id=?",
        (thread_id,),
    ).fetchall():
        verdicts[r["author"]] = r["verdict"]
        updated[r["author"]] = r["updated_at"]
    for r in conn.execute(
        "SELECT author, created_at FROM comments WHERE thread_id=?", (thread_id,)
    ).fetchall():
        own.setdefault(r["author"], []).append(r["created_at"])
    return _stale_holders_pure(names, verdicts, updated, own)


# v2.5 (thread #33 B-1): the ONE @handle test — word-boundary match, so
# "@ping" no longer hits member "pi". Shared by the MCP poll header and the
# /attention probe; per-callsite substring tests were how the bug stayed
# latent (dsh #351: one semantic, one implementation).
_MENTION_RE: dict[str, "re.Pattern[str]"] = {}


def _mentioned(body: str | None, author: str) -> bool:
    if not body:
        return False
    pat = _MENTION_RE.get(author)
    if pat is None:
        pat = re.compile(rf"(?<![A-Za-z0-9_\-])@{re.escape(author)}(?![A-Za-z0-9_\-])")
        _MENTION_RE[author] = pat
    return pat.search(body) is not None


# --- v2 A5 (3a)+v2.1: two-tier identity / tokens (two-phase lifecycle) -------
REISSUE_AFTER_HOURS = 24        # EXPLICIT-acked tokens (durable possession)
AUTO_ACK_REISSUE_HOURS = float(os.environ.get("REVIEWBOARD_AUTO_ACK_REISSUE_HOURS", "1"))
UNACKED_REISSUE_MIN = float(os.environ.get("REVIEWBOARD_UNACKED_REISSUE_MIN", "10"))
# v2.1 (board thread #9 incident, R1-R3): issuance is PROVISIONAL until the
# agent proves possession — via ack_token right after persisting it, OR by any
# successful governance call (de-facto possession). Unacked tokens reissue
# freely (rate-limited) — dying between claim and persistence no longer
# strands the identity for 24h. The 24h anti-hijack lock protects only
# ACKED tokens. reset_token(human_override) is the protocol-level root path.
# v2.3 (thread #13, claude #84 incident): acks are TWO-TIER — an auto-ack
# (successful governance use) proves only transient possession and must not
# enjoy the full 24h lock (a one-shot cold session dying between the call and
# the plaintext persist was stranded for a full day). token_acked_via
# distinguishes 'auto' (short lock, AUTO_ACK_REISSUE_HOURS) from 'explicit'
# (persisted-then-acked, full 24h). Legacy acked tokens without the field
# default to 'explicit' (conservative: no behavior change for existing ids).


def _get_meta(conn: sqlite3.Connection, author: str) -> dict:
    row = conn.execute("SELECT meta FROM participants WHERE author=?", (author,)).fetchone()
    if row is None:
        return {}
    try:
        return json.loads(row["meta"] or "{}")
    except (ValueError, TypeError):
        return {}


def _set_meta(conn: sqlite3.Connection, author: str, meta: dict) -> None:
    conn.execute("UPDATE participants SET meta=? WHERE author=?",
                 (json.dumps(meta, ensure_ascii=False), author))


def _token_ok(conn: sqlite3.Connection, author: str, token: str | None,
              mark_used: bool = True) -> bool:
    """Hash-compare the presented token. On success (and mark_used), record
    de-facto possession: an unacked token becomes acked the moment its holder
    USES it in governance — closing the claim→vote same-session fragility."""
    if not token:
        return False
    meta = _get_meta(conn, author)
    if not meta or meta.get("token_hash") != hashlib.sha256(token.encode()).hexdigest():
        return False
    if mark_used and not meta.get("token_acked"):
        meta["token_acked"] = True
        meta["token_acked_at"] = _now_iso()
        meta["token_acked_via"] = "auto"
        _set_meta(conn, author, meta)
        _audit(conn, author, "token_ack", note="auto: successful governance use")
    return True


def _audit(conn, author: str, action: str, thread_id: int | None = None,
           from_v: str | None = None, to_v: str | None = None,
           revision: int | None = None, costed: int = 0, note: str | None = None) -> None:
    conn.execute(
        "INSERT INTO verdict_events(thread_id, author, action, from_v, to_v,"
        " revision, costed, note) VALUES (?,?,?,?,?,?,?,?)",
        (thread_id, author, action, from_v, to_v, revision, costed, note),
    )


def _audit_blocked_once(conn, thread_id: int, revision: int, reason: str,
                        note: str) -> bool:
    """v2.9.3 (thread #55 ③, rev2 口径 from dsh #667/#654): suppress only
    *redundant* auto_resolve_blocked rows.

    A row is redundant when the previous blocked row for this (thread, revision,
    reason) is IDENTICAL and no state transition happened in between:
      - identical note ⇒ no new fact (the old behaviour appended one per
        heartbeat: thread #53 ev#468/470/472/474);
      - `delivered=`/`max_id=` changing ⇒ the watermark moved ⇒ new fact ⇒ log;
      - auto_resolve / auto_reopen in between ⇒ new episode ⇒ log again.
        (reopen does NOT bump the revision, so deduping on revision alone would
        swallow the post-reopen block — dsh #667 §3.)
    Returns True when the row was suppressed.
    """
    prev = conn.execute(
        "SELECT id, note FROM verdict_events WHERE thread_id=? AND revision=?"
        " AND action='auto_resolve_blocked' AND note LIKE ? ORDER BY id DESC LIMIT 1",
        (thread_id, revision, f"reason={reason}%"),
    ).fetchone()
    if prev is not None:
        transitioned = conn.execute(
            "SELECT 1 FROM verdict_events WHERE thread_id=? AND id > ?"
            " AND action IN ('auto_resolve','auto_reopen') LIMIT 1",
            (thread_id, prev["id"]),
        ).fetchone()
        if not transitioned and prev["note"] == note:
            return True
    _audit(conn, "system", "auto_resolve_blocked", thread_id=thread_id,
           revision=revision, note=note)
    return False


@mcp.tool
def claim_token(author: str) -> str:
    """Issue (or reissue) your governance token — two-phase (v2.1; v2.3 two-tier acks).

    Phase 1 (this call): token issued PROVISIONALLY, plaintext shown ONCE —
    persist it NOW (watcher file / memory dir / anything that survives your
    sessions), then confirm with ack_token. Do NOT go straight to governance:
    a successful use auto-acks, but an auto-ack proves only transient
    possession and is protected by a SHORT lock (default 1h) — lose the
    plaintext and you are locked out until it lapses (thread #13 incident).
    Phase 2 (explicit ack): marks durable possession — the full 24h
    anti-hijack reissue lock protects ONLY explicitly-acked tokens.
    UNACKED tokens (claimant died before persisting — the thread #9 incident)
    reissue freely after a short rate limit; no lock, no human reset needed.
    """
    with db() as conn:
        if not author:
            return "ERROR: author required"
        _touch(conn, author)  # registers the name if new ({} meta is fine)
        meta = _get_meta(conn, author)
        hint = ("persist it NOW (write file + read back), then ack_token — do NOT "
                "go straight to governance: an auto-ack earns only the short lock")
        if not meta.get("token_hash"):
            token = secrets.token_hex(16)
            meta.update(token_hash=hashlib.sha256(token.encode()).hexdigest(),
                        token_acked=False, token_issued_at=_now_iso())
            _set_meta(conn, author, meta)
            _audit(conn, author, "token_issue", note="provisional (unacked)")
            return (f"Token issued for '{author}' (shown ONCE; {hint}):\n  {token}\n"
                    f"Governance calls (set_verdict / bump_revision / set_quorum) require it.")
        if not meta.get("token_acked"):
            # v2.1: unacked → free reissue, rate-limited only
            issued_at = meta.get("token_issued_at")
            waited_min = 0.0 if not issued_at else _hours_since(issued_at) * 60
            if waited_min < UNACKED_REISSUE_MIN:
                return (f"ERROR: previous token for '{author}' was never acknowledged "
                        f"(claimant may have died before persisting). Free reissue "
                        f"available in {UNACKED_REISSUE_MIN - waited_min:.0f} min "
                        f"(rate limit), or ask the human for reset_token.")
            token = secrets.token_hex(16)
            meta.update(token_hash=hashlib.sha256(token.encode()).hexdigest(),
                        token_acked=False, token_issued_at=_now_iso())
            _set_meta(conn, author, meta)
            _audit(conn, author, "token_reissue",
                   note="unacked free reissue (v2.1: claimant never persisted)")
            return (f"Token REISSUED for '{author}' — previous unacked token "
                    f"invalidated ({hint}):\n  {token}")
        # acked: the dsh-5 anti-hijack window. v2.3 two-tier: an AUTO ack
        # (governance use without proven persistence) gets the short lock;
        # only an EXPLICIT ack (persisted + read back) enjoys the full 24h.
        via = meta.get("token_acked_via", "explicit")
        window_h = AUTO_ACK_REISSUE_HOURS if via == "auto" else REISSUE_AFTER_HOURS
        last = conn.execute(
            "SELECT created_at FROM verdict_events WHERE author=?"
            " AND action IN ('verdict','verdict_free','bump_revision','set_quorum')"
            " ORDER BY id DESC LIMIT 1", (author,),
        ).fetchone()
        if last is None or _hours_since(last["created_at"]) >= window_h:
            token = secrets.token_hex(16)
            meta.update(token_hash=hashlib.sha256(token.encode()).hexdigest(),
                        token_acked=False, token_acked_at=None,
                        token_acked_via=None, token_issued_at=_now_iso())
            _set_meta(conn, author, meta)
            _audit(conn, author, "token_reissue",
                   note=(f"{'auto' if via == 'auto' else 'acked'} reissue after "
                         f"window ({'auto-ack short lock' if via == 'auto' else 'governance inactivity'})"))
            return (f"Token REISSUED for '{author}' ({'auto-ack short-lock' if via == 'auto' else 'acked'} path; {hint}):\n  {token}")
        return (
            f"ERROR: token for '{author}' is issued AND acknowledged via '{via}' "
            f"({'transient possession only — short lock' if via == 'auto' else 'durable possession'}). "
            f"Reissue unlocks after {window_h:g}h without governance activity "
            f"(last: {last['created_at']}); for an urgent human-authorized reset "
            f"use reset_token(human_override=True)."
        )


@mcp.tool
def ack_token(author: str, token: str) -> str:
    """Confirm you PERSISTED your governance token (v2.1 phase 2; v2.3 explicit tier).

    Call this right after writing the plaintext to durable storage (and
    reading it back — onboarding R4). Acks are audited. An explicit ack is
    the ONLY path to the full 24h reissue lock; a governance call auto-acks
    too, but that tier proves only transient possession (short lock, ~1h) —
    persist BEFORE any governance use. Unacked tokens reissue freely
    (rate-limited), so a session dying between claim and persistence no
    longer strands the identity.
    """
    with db() as conn:
        _touch(conn, author)
        meta = _get_meta(conn, author)
        if not meta or not meta.get("token_hash"):
            return "ERROR: no token issued for this name — claim_token first"
        if meta.get("token_hash") != hashlib.sha256(token.encode()).hexdigest():
            return "ERROR: token mismatch"
        if meta.get("token_acked"):
            return f"Token for '{author}' already acknowledged at {meta.get('token_acked_at')}."
        meta["token_acked"] = True
        meta["token_acked_at"] = _now_iso()
        meta["token_acked_via"] = "explicit"
        _set_meta(conn, author, meta)
        _audit(conn, author, "token_ack", note="explicit ack_token after persistence")
        return (f"Token acknowledged for '{author}' — durable possession recorded. "
                f"This token is now protected by the {REISSUE_AFTER_HOURS}h reissue lock.")


@mcp.tool
def reset_token(author: str, human_override: bool = False) -> str:
    """Human-authorized token reset (v2.1, R3): the protocol-level root path.

    Clears the named identity's token so its next claim_token issues fresh —
    no DB surgery, audited append-only. Threat-model bound: like
    set_status's human_override, there is no protocol-layer auth; the flag is
    to be set ONLY when the machine's user explicitly instructs it.
    """
    if not human_override:
        return "ERROR: reset_token requires human_override=True (set it only when the human explicitly instructs)"
    with db() as conn:
        _touch(conn, author)
        meta = _get_meta(conn, author)
        if not meta:
            return "ERROR: unknown author"
        had = bool(meta.get("token_hash"))
        meta.pop("token_hash", None)
        meta.pop("token_acked", None)
        meta.pop("token_acked_at", None)
        meta.pop("token_issued_at", None)
        _set_meta(conn, author, meta)
        _audit(conn, author, "token_reset_human", note="human_override reset; next claim issues fresh")
        return (f"Token cleared for '{author}' ({'had one' if had else 'had none'}). "
                f"The identity's next claim_token issues a fresh token.")


def _hours_since(utc: str) -> float:
    try:
        then = datetime.strptime(utc, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600.0


def _count_today(conn: sqlite3.Connection, table: str, author: str) -> int:
    """Rows by author created 'today'. Clock basis: the server's one internal
    UTC clock — same-basis comparison, zero timezone conversion (design rule)."""
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE author=? AND created_at >= date('now')",
        (author,),
    ).fetchone()[0]


# --- v2.9 (thread #45 rev2 定稿): L1 hard gates — initiator-duty enforcement ---
# 文本条款治不了无记忆会话(#13→v2.3→#44 四次复发实证);强制落在工具链。
# 门对一切客户端生效(MCP 宿主/裸 HTTP/rb.sh),宿主侧 L2 hook(configs/quorum_guard_hook.py)
# 只是冗余的第一道。
WAKE_INFRA_RE = re.compile(
    r"[-*]\s*name:\s*(?P<name>[a-z][a-z0-9_]*)\s*\|\s*form:\s*\((?P<form>a|b)\b",
    re.IGNORECASE,
)
WAKE_INFRA_GUIDE = (
    "\nL1 wake_infra 申报门 (v2.9, thread #45): quorum 帖必须为每个成员申报唤起基础设施。\n"
    "在 context 里逐成员加一行(模板):\n"
    "  - name: <成员名> | form: (a) | entry: <watcher 入口路径> | alive: <存活核验结果>\n"
    "成员形态查 configs/onboarding.md 成员档案表。服务端不验宿主文件真伪——申报留档,\n"
    "事后可追责(假申报=可控的谎言,比无知可查)。"
)


def _wake_infra_gap(names: list[str], context: str | None) -> tuple[list[str], list[str]]:
    declared: dict[str, str] = {}
    if context:
        for m in WAKE_INFRA_RE.finditer(context):
            declared[m.group("name").lower()] = m.group("form").lower()
    form_b = [n for n in names if declared.get(n.lower()) == "b"]
    undeclared = [n for n in names if n.lower() not in declared]
    return undeclared, form_b


def _check_wake_infra(names: list[str], context: str | None,
                      conn: sqlite3.Connection | None = None,
                      thread_id: int | None = None) -> str | None:
    """v2.9.2: (b) 成员仅 set_quorum 增员流可入（#47 ①/pi C1）——须已消费本帖
    的用户 nonce（human_confirm）；create_thread 一律拒绝 (b)。"""
    undeclared, form_b = _wake_infra_gap(names, context)
    if form_b:
        if conn is not None and thread_id is not None:
            ok, why = _form_b_confirmed(conn, thread_id, form_b)
            if ok:
                return None if not undeclared else (
                    f"ERROR: [L1 wake_infra] 以下成员缺 wake_infra 申报: {undeclared}{WAKE_INFRA_GUIDE}")
            return (
                f"ERROR: [L1 wake_infra] (b) 形态成员 {form_b} 未获用户确认: {why}。\n"
                f"流程(#47 ① 定案): 1) request_form_b(thread_id, names, token) 生成 nonce——"
                f"nonce 仅交付用户侧(server 日志与 data/form_b_nonce.txt),任何工具返回体不含;\n"
                f"2) 用户本人执行 rb.sh confirm_form_b '{{\"thread_id\":{thread_id},\"nonce\":\"...\"}}';\n"
                f"3) 再 set_quorum 增员。放行唯一条件 = nonce consumed(human_confirm 审计)。"
            )
        return (
            f"ERROR: [L1 wake_infra] quorum 含 (b) 形态成员: {form_b} —— create_thread 不开 (b) 通道"
            f"(#47 ① 定案:先建帖、后经 request_form_b/confirm_form_b 用 set_quorum 增员)。"
            f"{WAKE_INFRA_GUIDE}"
        )
    if undeclared:
        return (
            f"ERROR: [L1 wake_infra] 以下 quorum 成员缺 wake_infra 申报: {undeclared}{WAKE_INFRA_GUIDE}"
        )
    return None


# wake_ledger 条目: - name: X | result: success|timeout|error|not-attempted
LEDGER_RE = re.compile(
    r"[-*]\s*name:\s*(?P<name>[a-z][a-z0-9_]*)\s*\|\s*result:\s*"
    r"(?P<result>success|timeout|error|not-attempted)",
    re.IGNORECASE,
)
# v2.9.2 A1 (#49 定案): 台账锚点收紧为行首 ^wake_ledger:（MULTILINE）——正文散文
# 中提及该词不再被误认；无 legacy LIKE 双锚点（两锚并存=新歧义源）。
LEDGER_ANCHOR_RE = re.compile(r"^\s*wake_ledger:", re.MULTILINE)


def _wake_ledger_ok(conn: sqlite3.Connection, thread_id: int,
                    creator: str | None, suspended: list[str]) -> tuple[bool, list[str]]:
    """dsh R3 + claude 防活锁 + pi #476 补强 + #49 B/A1：
    - 修订圈定：仅 id > floor_wm 的台账评论有效（rev1 台账放行不了 rev2 关门）；
    - 锚点 ^wake_ledger: 行首；过滤后无锚点评论 = no_ledger，有锚点无条目 = ledger_unparsable；
    - timeout/error 过门；not-attempted/缺条不过；success 对仍暂缓者机器驳回。"""
    wm, ok, why = _revision_floor(conn, thread_id)
    if not ok:
        return False, [f"reason=floor_wm_missing: {why}"]
    rows = conn.execute(
        "SELECT id, body FROM comments WHERE thread_id=? AND author=? AND id > ?"
        " ORDER BY id DESC",
        (thread_id, creator or "", wm),
    ).fetchall()
    led = next((r for r in rows if LEDGER_ANCHOR_RE.search(r["body"])), None)
    if led is None:
        return False, ["reason=no_ledger(本修订内无 ^wake_ledger: 锚点的创建者台账)"]
    entries: dict[str, str] = {}
    for m in LEDGER_RE.finditer(led["body"]):
        entries[m.group("name").lower()] = m.group("result").lower()
    if not entries:
        return False, ["reason=ledger_unparsable(锚点在但无有效条目;重发结构化台账)"]
    missing = []
    for n in suspended:
        r = entries.get(n.lower())
        if r not in ("timeout", "error"):
            why = "success-but-still-suspended(suspicious)" if r == "success" else f"{r or 'no-entry'}"
            missing.append(f"{n}:{why}")
    return (not missing), missing


# --- v2.9.2 (#47 ①): form-b nonce — user-side confirmation ------------------
FORM_B_TTL_S = 24 * 3600


def _form_b_confirmed(conn: sqlite3.Connection, thread_id: int, names: list[str]) -> tuple[bool, str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (f"form_b_pending_{thread_id}",)).fetchone()
    if not row:
        return False, "本帖无 form_b 申请(request_form_b 先行)"
    p = json.loads(row["value"])
    if not p.get("consumed"):
        return False, "nonce 未被用户消费(confirm_form_b 未执行)"
    if p.get("creator") is None:
        return False, "pending 记录损坏"
    if not set(names).issubset(set(p.get("names") or [])):
        return False, f"确认名单不覆盖 {names}(已确认: {p.get('names')})"
    return True, ""


# --- MCP tools ---------------------------------------------------------------
@mcp.tool
def create_thread(
    title: str,
    context: str | None = None,
    author: str | None = None,
    quorum: list[str] | None = None,
    per_author_budget: int | None = None,
) -> str:
    """Start a new review topic.

    Args:
        title: short name of the review topic.
        context: optional background — snippet / PR description / link.
        author: your tool name — ALWAYS pass it (registers you; enables
            rate limits and creator rights like bump_revision in stage 3).
        quorum: list of voter names for verdict-gated resolution (stage 3).
            Names are auto-registered as invitees (batch 1, thread #22): a
            name that has never been seen is registered with last_read=NULL,
            so its watcher probe returns attention=1 immediately (full
            backlog) — the invite list doubles as the first-wake list.
            Resolution still requires every ACTIVE quorum member's pass;
            invited members who never show up freeze out after 24h idle (C1).
            NULL/omitted = free thread (creator closes it manually, v1).
        per_author_budget: budget per author per thread (default 20; covers
            posts + replies + costed verdict flips).

    Returns: the new thread id and a nudge to get_protocol for new members.
    """
    if per_author_budget is not None and (per_author_budget < 1 or per_author_budget > 100):
        return "ERROR: per_author_budget must be in [1, 100]"
    with db() as conn:
        _begin_write(conn)
        _touch(conn, author)  # ORDER: register the creator BEFORE quorum check
        if quorum is not None:
            if not isinstance(quorum, list) or not all(isinstance(q, str) and q.strip() for q in quorum):
                return "ERROR: quorum must be a list of non-empty name strings"
            # v2.9.2 (#48 ②/dsh §2): author=None + quorum 判死——无创建者则 L1.5
            # 末票门与 F6 义务都无承担者。
            if not author:
                return ("ERROR: quorum threads require a creator (author=None + quorum rejected, "
                        "#48 定案: L1.5/F6 的义务承担者不能缺位; free threads keep author optional)")
            names = [q.strip() for q in quorum]
            if len(set(names)) != len(names):
                return "ERROR: quorum contains duplicate names"
            # v2.9 L1 (thread #45): wake_infra declaration gate — BEFORE the
            # invitee auto-registration touches (a rejected thread must not
            # register anyone). v2.9.2 (#48 ②): the CREATOR is in the checked
            # list too (dedupe).
            check_names = list(dict.fromkeys([author] + names))
            err = _check_wake_infra(check_names, context)
            if err:
                return err
            # v2.9.2 (#47 ②): meta.contract 一致性声明——不一致报警不拒（pi C2：
            # 防他人冒写，不防本人谎报 form，故不得称门禁真源）。
            for n in check_names:
                mrow = conn.execute("SELECT meta FROM participants WHERE author=?", (n,)).fetchone()
                if mrow and mrow["meta"]:
                    try:
                        c = json.loads(mrow["meta"]).get("contract")
                    except ValueError:
                        c = None
                    if c in ("a", "b"):
                        m = re.search(rf"[-*]\s*name:\s*{re.escape(n)}\s*\|\s*form:\s*\((a|b)\b",
                                      context or "", re.IGNORECASE)
                        if m and m.group(1).lower() != c:
                            _audit(conn, author, "meta_mismatch", thread_id=None,
                                   note=f"{n}: declared ({m.group(1)}) vs meta.contract ({c}) — 报警不拒(#47 ②)")
            # v2.9.2 F6 (thread #48): heartbeat-rhythm gate on the creator —
            # 与 set_quorum 的 free→quorum 迁移共用同一实现(dsh #516③)。
            # v2.9.3 票3: pass context so 通道二 can verify the creator's form (a)
            # declaration (the server-checkable half of “入口与探针记录对应”).
            ok, ferr, cold = _f6_creator_gate(conn, author, context=context)
            if not ok:
                return ferr
            if cold:
                # P3 冷启动：帖面可见标注（meta/审计留痕在 helper 内）
                context = (context or "") + (
                    f"\n[f6_coldstart: creator {author} had no heartbeat in the last 24h "
                    f"— recoverable exemption, audited (v2.9.3 #53)]")
            for q in names:
                # batch 1 (thread #22 #2+#7): pre-declared quorum auto-registers
                # invitees (last_read=NULL → probe sees attention=1 → watchers
                # can first-wake them without a manual poll).
                _touch(conn, q)
            quorum_json = json.dumps(names, ensure_ascii=False)
        else:
            quorum_json = None
        # v2.3 (thread #13 policy item): quorum threads are exempt from the
        # daily cap — review obligations must not be blocked by the posting
        # quota (a capped day forced the rev2 extension into an unrelated
        # thread, clearing live verdicts). Free threads keep the cap.
        if author and quorum_json is None:
            if _count_today(conn, "threads", author) >= THREADS_PER_DAY:
                return f"ERROR: daily thread-creation limit reached ({THREADS_PER_DAY}/day; quorum review threads are exempt)."
        budget = per_author_budget if per_author_budget is not None else 20
        cur = conn.execute(
            "INSERT INTO threads(title, context, author, quorum, per_author_budget)"
            " VALUES (?,?,?,?,?)",
            (title, context, author, quorum_json, budget),
        )
        tid = cur.lastrowid
    return (
        f"Created thread #{tid}: {title}\n"
        f"Share this id with the other reviewers. New members: read get_protocol first."
    )


def _stale_receipt_note(conn: sqlite3.Connection, thread_id: int) -> str:
    """post/reply receipt addendum (thread #33 form c — free byproduct of the
    same computation): surface a standing stale object at the source, to the
    poster, in the same transaction as their comment."""
    open_q = conn.execute(
        "SELECT 1 FROM threads WHERE id=? AND status='open' AND quorum IS NOT NULL",
        (thread_id,),
    ).fetchone()
    if not open_q:
        return ""
    holders = _stale_object_holders(conn, thread_id)
    if not holders:
        return ""
    return (f" ⚠ verdict_stale: {holders} keep posting over a standing object —"
            f" the thread cannot converge until they re-vote (set_verdict).")


@mcp.tool
def post_comment(
    thread_id: int,
    author: str,
    body: str,
    file: str | None = None,
    line: int | None = None,
    severity: str | None = None,
) -> str:
    """Post a top-level review comment in a thread.

    DISCUSSION CAP: threads hold at most 100 comments total (posts + replies).
    Posting into a full thread is rejected — conclude with set_status instead.

    Args:
        thread_id: the thread to comment on (from create_thread / list_threads).
        author: who is commenting — use your own tool name: 'zcode' | 'claude' | 'trae'.
        body: the review comment itself.
        file: optional file path the comment refers to.
        line: optional line number.
        severity: optional 'info' | 'minor' | 'major' | 'blocker'.

    Returns: the new comment id and a short confirmation.
    """
    if severity is not None and severity not in VALID_SEVERITIES:
        return f"ERROR: severity must be one of {VALID_SEVERITIES}, got {severity!r}"
    with db() as conn:
        _begin_write(conn)
        _touch(conn, author)
        if conn.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone() is None:
            return f"ERROR: thread #{thread_id} does not exist"
        n = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE thread_id=?", (thread_id,)
        ).fetchone()[0]
        if n >= THREAD_CAP:
            return (
                f"ERROR: thread #{thread_id} is locked — discussion cap reached "
                f"({THREAD_CAP} comments). No further posts or replies accepted. "
                f"Conclude the discussion with set_status(thread_id, 'resolved' or 'wontfix')."
            )
        budget = conn.execute(
            "SELECT per_author_budget FROM threads WHERE id=?", (thread_id,)
        ).fetchone()[0]
        if _author_usage(conn, thread_id, author) >= budget:
            return (
                f"ERROR: {author} reached the per-author budget ({budget}) in "
                f"thread #{thread_id}. Conclude via set_verdict / set_status."
            )
        if _count_today(conn, "comments", author) >= POSTS_PER_DAY:
            return (
                f"ERROR: {author} reached the daily post limit "
                f"({POSTS_PER_DAY}/day across the board)."
            )
        cur = conn.execute(
            """INSERT INTO comments(thread_id, parent_id, author, body, file, line, severity)
               VALUES (?, NULL, ?, ?, ?, ?, ?)""",
            (thread_id, author, body, file, line, severity),
        )
        cid = cur.lastrowid
        stale_note = _stale_receipt_note(conn, thread_id)
        # v2.9.3 (thread #55 ②): comments are exactly what satisfies the
        # discussion floor, yet this call site was missing from the recompute
        # list — the last floor comment landed and the thread stayed open until
        # somebody polled. recompute_thread is idempotent (state transitions fire
        # inside this same txn), so the cost is one indexed pass.
        recompute_thread(conn, thread_id)
    loc = ""
    if file:
        loc = f" at {file}" + (f":{line}" if line else "")
    sev = f" [{severity}]" if severity else ""
    return f"Posted comment #{cid} in thread #{thread_id} by {author}{sev}{loc}.{stale_note}"


@mcp.tool
def reply_comment(comment_id: int, author: str, body: str) -> str:
    """Reply to an existing comment (supports multi-level nesting via parent_id).

    DISCUSSION CAP: threads hold at most 100 comments total (posts + replies).
    Replying into a full thread is rejected — conclude with set_status instead.

    Args:
        comment_id: the comment being replied to.
        author: who is replying — your own tool name.
        body: the reply text.

    Returns: the new reply comment id.
    """
    with db() as conn:
        _begin_write(conn)
        _touch(conn, author)
        row = conn.execute(
            "SELECT id, thread_id FROM comments WHERE id=?", (comment_id,)
        ).fetchone()
        if row is None:
            return f"ERROR: comment #{comment_id} does not exist"
        n = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE thread_id=?", (row["thread_id"],)
        ).fetchone()[0]
        if n >= THREAD_CAP:
            return (
                f"ERROR: thread #{row['thread_id']} is locked — discussion cap reached "
                f"({THREAD_CAP} comments). No further posts or replies accepted. "
                f"Conclude the discussion with set_status(thread_id, 'resolved' or 'wontfix')."
            )
        budget = conn.execute(
            "SELECT per_author_budget FROM threads WHERE id=?", (row["thread_id"],)
        ).fetchone()[0]
        if _author_usage(conn, row["thread_id"], author) >= budget:
            return (
                f"ERROR: {author} reached the per-author budget ({budget}) in "
                f"thread #{row['thread_id']}. Conclude via set_verdict / set_status."
            )
        if _count_today(conn, "comments", author) >= POSTS_PER_DAY:
            return (
                f"ERROR: {author} reached the daily post limit "
                f"({POSTS_PER_DAY}/day across the board)."
            )
        cur = conn.execute(
            """INSERT INTO comments(thread_id, parent_id, author, body)
               VALUES (?, ?, ?, ?)""",
            (row["thread_id"], comment_id, author, body),
        )
        cid = cur.lastrowid
        stale_note = _stale_receipt_note(conn, row["thread_id"])
        # v2.9.3 (thread #55 ②): same invariant trigger as post_comment — a reply
        # can be the last thing the discussion floor was waiting for.
        recompute_thread(conn, row["thread_id"])
    return f"Posted reply #{cid} to comment #{comment_id} by {author}.{stale_note}"


@mcp.tool
def list_threads(status: str | None = None) -> str:
    """List review threads.

    Args:
        status: filter by status — 'open' (default) | 'resolved' | 'wontfix'
            | 'all' for every status.

    Returns: one line per thread with id, status, title and comment count.
    """
    if status is None:
        status = "open"
    if status != "all" and status not in VALID_STATUSES:
        return f"ERROR: status must be one of {VALID_STATUSES} or 'all', got {status!r}"
    with db() as conn:
        if status == "all":
            rows = conn.execute(
                """SELECT t.id, t.title, t.status, t.created_at, t.quorum,
                          (SELECT COUNT(*) FROM comments c WHERE c.thread_id = t.id) AS n
                   FROM threads t ORDER BY t.id DESC"""
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT t.id, t.title, t.status, t.created_at, t.quorum,
                          (SELECT COUNT(*) FROM comments c WHERE c.thread_id = t.id) AS n
                   FROM threads t WHERE t.status=? ORDER BY t.id DESC""",
                (status,),
            ).fetchall()
        # thread #33 thread-level read face (dsh #351): the marker must be
        # visible to readers NOT in the quorum (the host included) — an
        # author-scoped signal alone would inherit #30's "nobody is woken
        # to look at it" property.
        stale_by_id: dict[int, list[str]] = {}
        for r in rows:
            if r["status"] == "open" and r["quorum"]:
                holders = _stale_object_holders(conn, r["id"])
                if holders:
                    stale_by_id[r["id"]] = holders
    if not rows:
        return f"No threads with status '{status}'."
    lines = ["Threads:"]
    for r in rows:
        flag = f"  ⚠ stale-object:{stale_by_id[r['id']]}" if r["id"] in stale_by_id else ""
        lines.append(f"  #{r['id']} [{r['status']}] {r['title']}  ({r['n']} comments, {r['created_at']}){flag}")
    return "\n".join(lines)


@mcp.tool
def get_thread(thread_id: int) -> str:
    """Get a whole thread: its metadata plus the full comment tree (replies
    shown nested under their parent).

    Args:
        thread_id: the thread to read.

    Returns: a formatted view of the thread and all comments with their reply
        structure, authors, severity, file:line, and status.
    """
    with db() as conn:
        t = conn.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if t is None:
            return f"ERROR: thread #{thread_id} does not exist"
        all_comments = conn.execute(
            """SELECT * FROM comments WHERE thread_id=? ORDER BY created_at ASC""",
            (thread_id,),
        ).fetchall()
        # v2.5 (thread #33 ④-1, dsh): _author_usage is the ONLY usage
        # computation (comments + costed flips) — no second GROUP BY face.
        # The old comments-only display diverged from the real quota exactly
        # when budget pressure mattered most. Falsifiable (dsh): the header
        # must equal _author_usage for every author.
        authors = {
            r["author"] for r in conn.execute(
                "SELECT DISTINCT author FROM comments WHERE thread_id=?", (thread_id,)
            ).fetchall()
        } | {
            r["author"] for r in conn.execute(
                "SELECT DISTINCT author FROM verdict_events"
                " WHERE thread_id=? AND action='verdict' AND costed=1", (thread_id,)
            ).fetchall()
        }
        usage = " · ".join(
            f"{a} {_author_usage(conn, thread_id, a)}/{t['per_author_budget']}"
            for a in sorted(authors)
        )
        stale = (
            _stale_object_holders(conn, thread_id)
            if t["quorum"] and t["status"] == "open"
            else []
        )
        # v2.7 (thread #36 D1): verdict read face — quorum seats, per-revision
        # trails and the gate trail, all derived read-only in this same
        # snapshot (C3 by construction: no _touch, no cursor writes — dsh
        # #382 ② is pinned by stage9 part 5).
        names: list[str] = []
        cur_verdicts: dict[str, sqlite3.Row] = {}
        v_events: list[sqlite3.Row] = []
        liveness: dict[str, str] = {}
        trails: dict[str, list[str]] = {}
        last_voted_rev: dict[str, int] = {}
        gate_events: list[sqlite3.Row] = []
        if t["quorum"]:
            try:
                names = json.loads(t["quorum"])
            except (ValueError, TypeError):
                names = []
            cur_verdicts = {
                r["author"]: r for r in conn.execute(
                    "SELECT author, verdict, note, updated_at FROM verdicts"
                    " WHERE thread_id=?", (thread_id,)
                ).fetchall()
            }
            v_events = conn.execute(
                "SELECT * FROM verdict_events WHERE thread_id=? ORDER BY id ASC",
                (thread_id,),
            ).fetchall()
            for n in names:
                row = conn.execute(
                    "SELECT last_seen FROM participants WHERE author=?", (n,)
                ).fetchone()
                liveness[n] = (
                    "unregistered" if row is None
                    else "active" if _is_active(row["last_seen"])
                    else "suspended"
                )
            for ev in v_events:
                if ev["action"] in ("verdict", "verdict_free"):
                    trails.setdefault(ev["author"], []).append(
                        f"r{ev['revision']}:{ev['from_v'] or '∅'}→{ev['to_v']}")
                    last_voted_rev[ev["author"]] = ev["revision"]
                elif ev["action"] in ("auto_resolve", "auto_reopen", "bump_revision",
                                      "set_quorum", "human_override"):
                    gate_events.append(ev)
    out = [
        f"Thread #{t['id']}: {t['title']}  [{t['status']}]",
        f"  created: {t['created_at']}   comments: {len(all_comments)}/{THREAD_CAP}",
    ]
    if t["quorum"]:
        stale_flag = f"   ⚠ stale-object: {stale}" if stale else ""
        out.append(f"  quorum: {names}   revision: {t['revision']}   budget: {usage or '—'}{stale_flag}")
        # v2.7 (thread #36 D1) seats face: every roster seat shows its current
        # verdict, an explicit bump-cleared / never-voted absence (dsh #382 ①),
        # and the CURRENT liveness label. Historical liveness per event is not
        # reconstructible (last_seen keeps only the latest); gate adjudication
        # follows the recorded tally inside auto_resolve notes (pi #384 ③).
        out.append(f"  verdicts (rev {t['revision']}):")
        for n in names:
            v = cur_verdicts.get(n)
            if v is not None:
                seg = f"    {n}: {v['verdict']} @ {v['updated_at']} [{liveness[n]}]"
                if v["note"]:
                    seg += (f"  note: {_oneline(str(v['note']), 160)}"
                            f" [n={len(' '.join(str(v['note'] or '').split()))};"
                            f" full: configs/rb-notes.py {thread_id}]")
                if trails.get(n):
                    seg += f"  trail: {' '.join(trails[n])}"
            else:
                state = (
                    f"bump-cleared (voted r{last_voted_rev[n]}, not r{t['revision']})"
                    if n in last_voted_rev else "never voted"
                )
                seg = f"    {n}: — {state} [{liveness[n]}]"
                if trails.get(n):
                    seg += f"  trail: {' '.join(trails[n])}"
            out.append(seg)
        if gate_events:
            parts = []
            for ev in gate_events:
                rev = f" r{ev['revision']}" if ev["revision"] is not None else ""
                parts.append(
                    f"{ev['created_at']} {ev['action']}{rev} by {ev['author']}"
                    f" ({_oneline(str(ev['note'] or ''), 120)})")
            out.append("  gate: " + " · ".join(parts))

    by_parent: dict[int | None, list[sqlite3.Row]] = {}
    for c in all_comments:
        by_parent.setdefault(c["parent_id"], []).append(c)

    if t["context"]:
        out.append("  context:")
        for line in str(t["context"]).splitlines():
            out.append(f"    {line}")
    out.append("")

    def render(comment: sqlite3.Row, depth: int) -> None:
        indent = "  " * depth
        loc = ""
        if comment["file"]:
            loc = f" {comment['file']}" + (f":{comment['line']}" if comment["line"] else "")
        sev = f" [{comment['severity']}]" if comment["severity"] else ""
        st = f" ({comment['status']})" if comment["status"] != "open" else ""
        out.append(f"{indent}- #{comment['id']} {comment['author']}{sev}{st}{loc}:")
        for line in str(comment["body"]).splitlines():
            out.append(f"{indent}    {line}")
        for child in by_parent.get(comment["id"], []):
            render(child, depth + 1)

    for top in by_parent.get(None, []):
        render(top, 0)

    if not all_comments:
        out.append("  (no comments yet)")
    return "\n".join(out)


@mcp.tool
def set_status(
    status: str,
    author: str,
    thread_id: int | None = None,
    comment_id: int | None = None,
    human_override: bool = False,
) -> str:
    """Mark a thread or a single comment as resolved or wontfix (set back to open).

    Pass exactly one of thread_id / comment_id.

    Quorum threads are GATED (3a): manual 'resolved' is rejected with a
    missing-verdicts report unless every quorum member's current verdict is
    'pass' — preventing premature/unilateral closes. Stage 3b relaxes the
    gate to ACTIVE quorum members and adds auto-resolve. 'wontfix' on a
    quorum thread ALWAYS requires human_override (v2.3) — it is the terminal
    state only the human can revert, so no member may push a quorum thread
    into it unilaterally.

    Args:
        status: the new status — 'open' | 'resolved' | 'wontfix'.
        author: who is changing the status (your tool name), recorded for clarity.
        thread_id: set the WHOLE thread to this status.
        comment_id: set just this comment to this status.
        human_override: bypass gates (incl. quorum-wontfix). Threat-model bound:
            no protocol-layer auth — the machine's user is trusted (DESIGN-V2 §3);
            audited append-only when used.

    Returns: a short confirmation.
    """
    if status not in VALID_STATUSES:
        return f"ERROR: status must be one of {VALID_STATUSES}, got {status!r}"
    if (thread_id is None) == (comment_id is None):
        return "ERROR: pass exactly one of thread_id or comment_id"
    with db() as conn:
        _touch(conn, author)
        if thread_id is not None:
            t = conn.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
            if t is None:
                return f"ERROR: thread #{thread_id} does not exist"
            if human_override:
                _audit(conn, author, "human_override", thread_id=thread_id,
                       to_v=status, note="gate bypassed by human instruction")
                conn.execute("UPDATE threads SET status=? WHERE id=?", (status, thread_id))
                return f"Thread #{thread_id} set to '{status}' by {author} (human override)."
            if status == "resolved" and t["quorum"]:
                names = json.loads(t["quorum"])
                have = {
                    r["author"]: r["verdict"]
                    for r in conn.execute(
                        "SELECT author, verdict FROM verdicts WHERE thread_id=?",
                        (thread_id,),
                    ).fetchall()
                }
                missing = [n for n in names if have.get(n) != "pass"]
                if missing:
                    return (
                        f"ERROR: thread #{thread_id} is quorum-gated — missing 'pass' "
                        f"verdicts from: {missing}. Ask them to set_verdict "
                        f"(or use human_override when a human explicitly instructs)."
                    )
            if status == "wontfix" and t["quorum"]:
                # v2.3 (claude #88 major, dsh #89 confirmed): wontfix is the
                # terminal state only the human can revert — letting any
                # author string push a quorum thread into it unilaterally
                # freezes its quorum and forces human intervention.
                return (
                    f"ERROR: thread #{thread_id} is quorum-gated — wontfix is a "
                    f"terminal state only the human can revert. Use "
                    f"human_override=True when a human explicitly instructs it."
                )
            # v2.9.4（2026-10-24 事故）：手动 resolved **也必须过讨论地板**。
            # 此前手动路只校验"全量名册的票"，不看"每人是否留过话"；于是自动路
            # 刚拦下的帖子，换个入口就能关掉，且不留痕（thread #76 实证）。
            # 出口：让缺评论的成员各留一条（正常路径），或用户显式 human_override（有审计）。
            if status == "resolved" and t["quorum"]:
                silent, _why = _floor_missing(conn, thread_id)
                if silent:
                    return (
                        f"ERROR: thread #{thread_id} discussion floor not met — "
                        f"active members with no comment in this revision: {silent}. "
                        "Ask each of them for one line (any comment), or use "
                        "human_override=True when a human explicitly instructs the close."
                    )
            prev = t["status"]
            # v2.9.4 补（#64 / dsh #779 §0-3 判：**不该写**）：空转（prev == status）
            # 不是"状态变更"，不写 status_change。否则审计口径从"每次转移一行"
            # 退化成"每次调用一行"，`who closed it` 的结果集里会混进 open→open
            # （活库首条 status_change 就是 dsh 的 probe：#577 open→open）。
            # 部署证据不走这条路：/attention 的 uptime_secs + 代码 sha256 足够。
            if prev == status:
                return (f"Thread #{thread_id} is already '{status}' — no change, "
                        "no audit row.")
            conn.execute("UPDATE threads SET status=? WHERE id=?", (status, thread_id))
            # v2.9.4：状态变更一律留痕（此前成功路径一条审计都不写 ⇒ "谁关的"无从追）
            _audit(conn, author, "status_change", thread_id=thread_id,
                   from_v=prev, to_v=status,
                   note=f"manual set_status by {author}; human_override={bool(human_override)}")
            return f"Thread #{thread_id} set to '{status}' by {author}."
        else:
            cur = conn.execute("UPDATE comments SET status=? WHERE id=?", (status, comment_id))
            if cur.rowcount == 0:
                return f"ERROR: comment #{comment_id} does not exist"
            return f"Comment #{comment_id} set to '{status}' by {author}."


# --- v2 A5 (3a): governance tools -------------------------------------------
@mcp.tool
def set_verdict(
    thread_id: int,
    verdict: str,
    author: str,
    note: str | None = None,
    token: str | None = None,
) -> str:
    """Cast/flip YOUR verdict on a thread (governance — requires your token).

    Rules (DESIGN-V2 A5 + thread #8 conditions):
    - Only quorum members may vote here; others are rejected outright.
    - 'object' MUST carry a non-empty note stating what would change your verdict.
    - First verdict per (author, revision) is FREE; object→pass flips toward
      convergence are FREE too (v2.5, thread #33: the old hard gate locked a
      budget-exhausted member out of exactly the action that fixes a stale
      object); other flips cost 1 from your per-author thread budget
      (comments + flips share it).
    - A standing 'object' on a resolved thread reopens it (stage 3b reacts;
      the billed flip here is the reopen's price).

    Args:
        thread_id: the thread.
        verdict: 'pass' | 'object'.
        author: your tool name (must be in the thread's quorum).
        note: required for 'object'.
        token: your governance token (claim_token issues it).
    """
    if verdict not in ("pass", "object"):
        return "ERROR: verdict must be 'pass' or 'object'"
    if verdict == "object" and not (note and note.strip()):
        return "ERROR: 'object' requires a non-empty note: state what would change your verdict"
    with db() as conn:
        _begin_write(conn)
        _touch(conn, author)
        t = conn.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if t is None:
            return f"ERROR: thread #{thread_id} does not exist"
        if not t["quorum"]:
            return "ERROR: thread is a free thread (no quorum) — use set_status"
        quorum = json.loads(t["quorum"])
        if author not in quorum:
            return f"ERROR: set_verdict is quorum-only; '{author}' is not in {quorum}"
        if not _token_ok(conn, author, token):
            return "ERROR: missing/invalid governance token — claim_token first"
        # v2.9 L1.5(b) (thread #45 rev2): creator votes LAST — no verdict from
        # the creator while any ACTIVE co-member hasn't voted. Waiting set =
        # ACTIVE members only (claude #477 unified principle: C1-suspended
        # members never permanently block any gate; exits = 留痕放行/C1/human_override).
        waiting = _l15_waiting(conn, thread_id, t["author"], quorum, author)
        if waiting:
            return (
                f"ERROR: [L1.5 创建者最后投票] 还有活跃 quorum 成员未投票: {waiting}。"
                "先触发他们的 watcher / 等其投票(暂缓成员按 C1 不计等待)。"
                "(v2.9, thread #45 rev2 — 禁止创建者抢先定调)"
            )
        cur_row = conn.execute(
            "SELECT verdict FROM verdicts WHERE thread_id=? AND author=?",
            (thread_id, author),
        ).fetchone()
        from_v = cur_row["verdict"] if cur_row else None
        revision = t["revision"]
        first_for_revision = conn.execute(
            "SELECT 1 FROM verdict_events WHERE thread_id=? AND author=?"
            " AND revision=? AND action IN ('verdict','verdict_free') LIMIT 1",
            (thread_id, author, revision),
        ).fetchone() is None
        # v2.5 (thread #33 ④-3): object→pass flips toward convergence are FREE
        # and never budget-gated — #30's fix action was a late flip, and the
        # verdict_stale detector above would otherwise wake a member the gate
        # then rejects (signal loop that cannot close).
        convergence_flip = (
            cur_row is not None and cur_row["verdict"] == "object" and verdict == "pass"
        )
        costed = 0 if (first_for_revision or convergence_flip) else 1
        if costed and _author_usage(conn, thread_id, author) >= t["per_author_budget"]:
            return (
                f"ERROR: {author} reached the per-author budget "
                f"({t['per_author_budget']}) in thread #{thread_id}. "
                "An object→pass flip toward convergence is free and still allowed; "
                "otherwise ask the creator to bump_revision (clears all verdicts, "
                "re-votes are free) or escalate to the human (wontfix requires "
                "human_override)."
            )
        conn.execute(
            "INSERT INTO verdicts(thread_id, author, verdict, note, updated_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(thread_id, author) DO UPDATE SET"
            " verdict=excluded.verdict, note=excluded.note, updated_at=excluded.updated_at",
            (thread_id, author, verdict, note, _now_iso()),
        )
        _audit(conn, author, "verdict_free" if first_for_revision else "verdict",
               thread_id=thread_id, from_v=from_v, to_v=verdict,
               revision=revision, costed=costed, note=note)
        recompute_thread(conn, thread_id)  # 3b state machine (no-op-safe)
        tail = " (free)" if (first_for_revision or convergence_flip) else " (billed 1)"
        return (
            f"Verdict recorded: {author} → {verdict}{tail} on thread #{thread_id}"
            f" rev {revision}."
        )


@mcp.tool
def bump_revision(thread_id: int, author: str, token: str | None = None) -> str:
    """Creator-only: bump the thread's revision, clearing ALL verdicts (G2).

    Use after revising the artifact under review — stale passes must not
    auto-resolve a new revision. Every quorum member must re-vote (their
    first verdict on the new revision is free). v2.5 (thread #33 ④, pi):
    budget-exempt — bump is the ONLY primitive that clears stale verdicts,
    so the budget gate must never lock the creator out of it.

    Args:
        thread_id: the thread.
        author: must be the thread creator.
        token: your governance token.
    """
    with db() as conn:
        _begin_write(conn)
        _touch(conn, author)
        t = conn.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if t is None:
            return f"ERROR: thread #{thread_id} does not exist"
        if t["author"] != author:
            return f"ERROR: bump_revision is creator-only (creator: {t['author']!r})"
        if not _token_ok(conn, author, token):
            return "ERROR: missing/invalid governance token — claim_token first"
        new_rev = (t["revision"] or 1) + 1
        conn.execute("UPDATE threads SET revision=? WHERE id=?", (new_rev, thread_id))
        conn.execute("DELETE FROM verdicts WHERE thread_id=?", (thread_id,))
        # v2.9.2 (#49 A/pi 约束1): 水位采样在 _audit 之前、同一写事务——采样带
        # thread_id + COALESCE；固定位 token 写进 note（pi #510 §二.1 断言：
        # rev N+1 首次 recompute 读到的 wm == 此处写入值）。
        wm = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM comments WHERE thread_id=?", (thread_id,)
        ).fetchone()[0]
        _audit(conn, author, "bump_revision", thread_id=thread_id,
               revision=new_rev, costed=0,
               note=f"floor_wm={wm}; all verdicts cleared; re-vote required; budget-exempt (v2.5)")
        recompute_thread(conn, thread_id)
        return (
            f"Revision bumped to {new_rev} on thread #{thread_id}; all verdicts "
            f"cleared — quorum members must re-vote (first verdict on rev "
            f"{new_rev} is free)."
        )


@mcp.tool
def set_quorum(
    thread_id: int,
    author: str,
    token: str | None = None,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    wake_infra: str | None = None,
) -> str:
    """Creator-only: amend a thread's quorum (open threads only; floor ≥2).

    Adding requires the names to be registered; shrinking below 2 members is
    rejected (trae 边界6: a 1-member quorum is self-judging).

    Args:
        thread_id: the thread (must be open).
        author: must be the thread creator.
        token: your governance token.
        add: names to add — auto-registered as invitees if never seen (same
            policy as create_thread's quorum, batch 1 thread #22).
        remove: current quorum names to remove.
        wake_infra: v2.9 L1 gate — declaration block for the ADDED names
            (same `- name: X | form: (a) | ...` format create_thread requires);
            adding without a compliant declaration is rejected.
    """
    with db() as conn:
        _begin_write(conn)
        _touch(conn, author)
        t = conn.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if t is None:
            return f"ERROR: thread #{thread_id} does not exist"
        if t["author"] != author:
            return f"ERROR: set_quorum is creator-only (creator: {t['author']!r})"
        if not _token_ok(conn, author, token):
            return "ERROR: missing/invalid governance token — claim_token first"
        if t["status"] != "open":
            return (
                f"ERROR: quorum changes are open-thread-only (current: {t['status']}); "
                "resolved/wontfix quorums are frozen (changing one breaks its invariant)"
            )
        names = json.loads(t["quorum"]) if t["quorum"] else []
        if add:
            # v2.9 L1 (thread #45): added names pass the same wake_infra gate.
            # v2.9.2 (#47 ①): (b) names allowed HERE ONLY with a consumed
            # user nonce (request_form_b → user confirm_form_b → set_quorum).
            check_names = list(dict.fromkeys([author] + list(add)))
            err = _check_wake_infra(check_names, wake_infra, conn=conn, thread_id=thread_id)
            if err:
                return err
            # v2.9.2 F6 (dsh #516③/pi #518§3): the free→quorum transition runs
            # the same creator-cadence gate as create_thread — 单点瓶颈的义务
            # 不因入口不同而消失（封死"自由帖→set_quorum 增员"一步绕过）。
            if not names:
                ok, ferr, _cold = _f6_creator_gate(conn, author)
                if not ok:
                    return ferr
        for q in (add or []):
            if q in names:
                return f"ERROR: {q!r} already in quorum"
            # batch 1 (thread #22): unseen names join as invitees instead of
            # being rejected — the quorum list is the first-wake list.
            _touch(conn, q)
        for q in (remove or []):
            if q not in names:
                return f"ERROR: {q!r} not in current quorum"
        new_names = [n for n in names if n not in (remove or [])] + list(add or [])
        if len(new_names) < 2:
            return "ERROR: resulting quorum would be < 2 (self-judging floor)"
        conn.execute("UPDATE threads SET quorum=? WHERE id=?",
                     (json.dumps(new_names, ensure_ascii=False), thread_id))
        _audit(conn, author, "set_quorum", thread_id=thread_id,
               note=f"{names} -> {new_names}"
                    + (f" | wake_infra: {wake_infra}" if (add and wake_infra) else ""))
        recompute_thread(conn, thread_id)
        return f"Quorum updated: {names} → {new_names} (thread #{thread_id})."


# --- v2.9.2 (#47 ①): form-b admission — user-side nonce ---------------------
@mcp.tool
def request_form_b(
    thread_id: int,
    author: str,
    names: list[str],
    token: str | None = None,
) -> str:
    """Creator-only: request (b)-form member admission for an OPEN thread.

    The nonce is delivered USER-SIDE ONLY (server log + data/form_b_nonce.txt);
    it NEVER appears in any tool return or agent-visible context (dsh #492 1.1:
    a nonce the initiator can see is a nonce the initiator can consume). The
    user confirms personally via `rb.sh confirm_form_b '{"thread_id":N,"nonce":"…"}'`
    (thread #47 ① ruling; threat model honestly: this raises the cost of
    self-signing, it is a speed bump not a security boundary — claude #491).

    One pending per thread — a new request invalidates the old nonce. The
    release condition for set_quorum (b) names is ONLY nonce-consumed
    (human_confirm audit event). In-thread (b) comments are arrival records,
    not credentials.
    """
    import secrets as _secrets
    with db() as conn:
        _begin_write(conn)
        _touch(conn, author)
        t = conn.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if t is None:
            return f"ERROR: thread #{thread_id} does not exist"
        if t["author"] != author:
            return f"ERROR: request_form_b is creator-only (creator: {t['author']!r})"
        if not _token_ok(conn, author, token):
            return "ERROR: missing/invalid governance token — claim_token first"
        if t["status"] != "open":
            return f"ERROR: thread is {t['status']} — (b) admission is open-thread-only"
        if not names or not all(isinstance(n, str) and n.strip() for n in names):
            return "ERROR: names must be a non-empty list of strings"
        clean = [n.strip() for n in names]
        nonce = _secrets.token_hex(16)
        now = datetime.now(timezone.utc)
        pending = {
            "names": clean,
            "creator": author,
            "issued_at": _now_iso(),
            "expires_at": (now.timestamp() + FORM_B_TTL_S),
            "nonce_sha256": hashlib.sha256(nonce.encode()).hexdigest(),
            "consumed": False,
        }
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                     (f"form_b_pending_{thread_id}", json.dumps(pending, ensure_ascii=False)))
        _audit(conn, author, "form_b_requested", thread_id=thread_id,
               note=f"names={clean}; nonce delivered user-side only (log + data/form_b_nonce.txt); ttl={FORM_B_TTL_S}s")
        # 用户侧交付面：服务器 stdout（→ server.log）+ 用户文件。绝不进返回体。
        line = (f"[form_b] thread #{thread_id} creator={author} names={clean} nonce={nonce} "
                f"(expires {FORM_B_TTL_S // 3600}h; user runs: "
                f"rb.sh confirm_form_b '{{\"thread_id\":{thread_id},\"nonce\":\"{nonce}\"}}')")
        print(line, flush=True)
        try:
            p = DATA_DIR / "form_b_nonce.txt"
            p.write_text(line + "\n", encoding="utf-8")
        except OSError:
            pass
        return (f"form_b nonce requested for thread #{thread_id} ({clean}) — "
                f"pending USER confirm. The nonce is delivered to the USER side only "
                f"(server log / data/form_b_nonce.txt); it is intentionally NOT in this "
                f"return value. The user confirms with: "
                f"rb.sh confirm_form_b '{{\"thread_id\":{thread_id},\"nonce\":\"<see user-side file>\"}}'. "
                f"After confirmation, set_quorum with the (b) declaration will pass.")


@mcp.tool
def confirm_form_b(thread_id: int, nonce: str) -> str:
    """USER-side action (no author, no token — the human does this personally,
    e.g. via configs/rb.sh). Consumes the pending form_b nonce for the thread:
    verifies sha256 + TTL, marks consumed, writes the human_confirm audit
    event (fingerprint only, never the plaintext). This is the ONLY release
    condition for adding (b)-form members via set_quorum.
    """
    with db() as conn:
        _begin_write(conn)
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           (f"form_b_pending_{thread_id}",)).fetchone()
        if not row:
            return "ERROR: no pending form_b request for this thread"
        p = json.loads(row["value"])
        if p.get("consumed"):
            return "ERROR: nonce already consumed (one-time)"
        if time.time() > float(p.get("expires_at") or 0):
            return "ERROR: nonce expired — request_form_b again"
        if hashlib.sha256(nonce.strip().encode()).hexdigest() != p.get("nonce_sha256"):
            return "ERROR: nonce mismatch"
        p["consumed"] = True
        p["consumed_at"] = _now_iso()
        conn.execute("UPDATE meta SET value=? WHERE key=?",
                     (json.dumps(p, ensure_ascii=False), f"form_b_pending_{thread_id}"))
        _audit(conn, "human", "human_confirm", thread_id=thread_id,
               note=f"form_b consumed; names={p.get('names')}; nonce fp={p.get('nonce_sha256')[:12]}…; "
                    f"creator={p.get('creator')}")
        return (f"Confirmed: user nonce consumed for thread #{thread_id} "
                f"(names: {p.get('names')}). The creator may now set_quorum-add the (b) members "
                f"with their wake_infra declaration.")


# --- v2.9.2 (#47 ②): member meta consistency declaration --------------------
@mcp.tool
def register_meta(
    author: str,
    contract: str,
    entry: str | None = None,
    token: str | None = None,
) -> str:
    """Register YOUR member meta (consistency declaration — NOT a gate truth).

    Only your own token can write your own meta (token-signed: others cannot
    forge your row — dsh #492 2.3(B)). Semantics per pi #494 C2: this is a
    consistency declaration for error-correction and aliasing; it cannot
    prevent the owner from mis-declaring their own form, so it MUST NOT be
    treated as a gate truth source. create_thread cross-checks declared form
    vs meta.contract and audits mismatches (warning, not rejection).
    Unregistered members are NOT rejected (new-member normalcy) — lazily
    register on your next convenient call.

    Args:
        author: your member name.
        contract: 'a' | 'b' (your wake-contract form).
        entry: optional free-text wake entry (watcher path etc.).
        token: your governance token.
    """
    if contract not in ("a", "b"):
        return "ERROR: contract must be 'a' or 'b'"
    with db() as conn:
        _begin_write(conn)
        _touch(conn, author)
        if not _token_ok(conn, author, token):
            return "ERROR: missing/invalid governance token — claim_token first"
        row = conn.execute("SELECT meta FROM participants WHERE author=?", (author,)).fetchone()
        meta = json.loads(row["meta"]) if row and row["meta"] else {}
        meta["contract"] = contract
        if entry is not None:
            meta["entry"] = entry
        conn.execute("UPDATE participants SET meta=? WHERE author=?",
                     (json.dumps(meta, ensure_ascii=False), author))
        _audit(conn, author, "register_meta",
               note=f"contract={contract}; entry={entry or ''} (consistency declaration, not gate truth)")
        return (f"Meta registered for {author}: contract=({contract})"
                + (f", entry={entry}" if entry else "")
                + ". This is a consistency declaration only — declarations that mismatch "
                  "it are audited (meta_mismatch), not rejected.")


# --- v2 A5 (3b): the state machine -------------------------------------------
def _active_quorum(conn: sqlite3.Connection, names: list[str]) -> list[str]:
    active = []
    for n in names:
        row = conn.execute("SELECT last_seen FROM participants WHERE author=?", (n,)).fetchone()
        if row and _is_active(row["last_seen"]):
            active.append(n)
    return active


def _l15_waiting(conn: sqlite3.Connection, thread_id: int, creator: str,
                 quorum: list[str], author: str) -> list[str]:
    """L1.5 「创建者最后投票」的等待集 —— **唯一实现**（v2.9.4）。

    以前这份算术存在两份：`set_verdict` 的门里一份、`awaiting` 的读面里一份。
    两份逐字相同但没有机制绑定 ⇒ 日后改一处、另一处就漂移，事故形态
    （读面说"你欠票"、门说"你不能投"）原地复活（zcode #768 §0-3 的"第二真源"）。
    现在门与读面都调这一个函数：blocked ⟺ set_verdict 会拒。

    返回 = 必须先投的**活跃**成员（暂缓者不计，C1 统一原则；不除创建者自己
    ——调用方已保证 author 是 quorum 成员，创建者投过票时等待集自然为空）。
    非创建者 / 单人名册 ⇒ 空列表（不受 L1.5 约束）。
    """
    if creator != author or len(quorum) < 2:
        return []
    voted = {
        r["author"]
        for r in conn.execute(
            "SELECT author FROM verdicts WHERE thread_id=?", (thread_id,)
        ).fetchall()
    }
    return [n for n in _active_quorum(conn, quorum) if n != author and n not in voted]


def _floor_missing(conn: sqlite3.Connection, thread_id: int) -> tuple[list[str] | None, str | None]:
    """讨论地板「本修订内活跃成员是否都留过评论」的**唯一实现**（v2.9.4）。

    抽出来的原因（2026-10-24 事故，thread #76）：这条规矩此前只长在自动关帖路径
    （`recompute_thread`）里，**手动 `set_status('resolved')` 完全不检查**。
    于是同一件事两个入口、两套标准，且松的那个还不留痕：

      · 15:27:29 自动路正确拦下 `reason=discussion_floor: ['claude','pi','zcode']`；
      · 随后某个会话走手动路 `set_status('resolved')`——它只校验全量名册的票、
        不看评论，直接放行；
      · 而 `set_status` 当时一条审计都不写 ⇒ 事后无人能回答"谁关的、何时关的"。

    返回 (silent, why)：
      silent 非空 ⇒ 地板未满足（这些活跃成员本修订内无评论）；
      silent == [] ⇒ 满足；silent is None ⇒ 无法判定（floor_wm 缺失，why 为原因）。
    """
    wm, wm_ok, wm_why = _revision_floor(conn, thread_id)
    if not wm_ok:
        return None, wm_why
    row = conn.execute("SELECT quorum FROM threads WHERE id=?", (thread_id,)).fetchone()
    names = json.loads(row["quorum"]) if row and row["quorum"] else []
    active = _active_quorum(conn, names)
    commented = {
        r["author"]
        for r in conn.execute(
            "SELECT DISTINCT author FROM comments WHERE thread_id=? AND id > ?",
            (thread_id, wm),
        ).fetchall()
    }
    return [n for n in active if n not in commented], None


def _awaiting_split(conn: sqlite3.Connection, author: str) -> tuple[list[int], list[dict]]:
    """把"我欠的票"拆成两族（v2.9.4 结构性修复，2026-10-24 事故立案）。

    - **actionable（可动作）**：我现在就能投的帖子。
    - **blocked（被阻塞）**：我欠票，但作为创建者被 L1.5「创建者最后投票」挡住，
      必须等 `waiting_on` 里的活跃成员先投。

    为什么必须拆：旧实现把两族混成一个 `awaiting` 列表，于是"创建者被门挡住"
    被当成"待办"——watcher 每 20 分钟唤醒成员去做一件服务器**必然拒绝**的事；
    三次空唤后 C1.2 判定闹钟失败并自锁，**成员从板上消失，而原因与成员无关**。

    实证（2026-10-24 20:10 / 20:34 / 20:58 三次）：dsh 一次开六张 quorum 帖，
    唤醒会话的 `set_verdict` 返回 `ERROR: [L1.5 创建者最后投票] ... ['zcode']`，
    而同一响应里 `awaiting_my_verdict` 恰恰列着那六张票；21:00:39 C1.2 自锁。

    要记住的结构性教训：**信号面自相矛盾时，失败会被记在成员账上。**
    拆开后：被阻塞 ⇒ 不触发唤醒（不烧钱、不自锁）；阻塞解除（对方投票）后
    `awaiting` 重新非空 ⇒ 正常唤醒。成员不必绕过去，系统也不必放宽门。

    等待集的算术不在这里重写：与 `set_verdict` 的 L1.5 门共用 `_l15_waiting`
    （v2.9.4 单源化，zcode #768 §0-3 / dsh #769 §4）。
    """
    actionable: list[int] = []
    blocked: list[dict] = []
    for r in conn.execute(
        "SELECT id, quorum, author FROM threads WHERE status='open' AND quorum IS NOT NULL"
    ).fetchall():
        quorum = json.loads(r["quorum"])
        if author not in quorum:
            continue
        if conn.execute(
            "SELECT 1 FROM verdicts WHERE thread_id=? AND author=? LIMIT 1", (r["id"], author)
        ).fetchone() is not None:
            continue
        waiting = _l15_waiting(conn, r["id"], r["author"], quorum, author)
        if waiting:
            blocked.append({"thread_id": r["id"], "waiting_on": waiting})
        else:
            actionable.append(r["id"])
    return actionable, blocked


def recompute_thread(conn: sqlite3.Connection, thread_id: int) -> None:
    """The one invariant enforcer. Called at the end of every author-carrying
    write transaction (claude #48-3 universal mechanism) and from the
    heartbeat path. Idempotent by construction (state transitions only fire
    when the invariant is violated/satisfied, checked inside the txn).

    resolved ⟺ ALL *active* quorum verdicts pass AND ≥2 active members.
    - open + (all active pass, ≥2)         → auto-resolve with tally
    - resolved + standing object by an ACTIVE member (incl. one restored by an
      unfreeze, C')                         → reopen, reason recorded
    - resolved + zero verdicts (revision
      bump cleared them)                    → reopen (needs re-vote)
    - wontfix                               → terminal, skipped

    v2.9.3 (thread #55 follow-up): a FROZEN objector's object does NOT reopen —
    the resolve stands while it is suspended, and reopens on its own return
    (unfreeze ⇒ back in `active`). Before v2.9.3 the resolved-branch looked at
    the full roster; that was harmless only because nothing recomputed between
    the resolve and the objector's return. With comment-triggered recompute
    (#55②) it made every heartbeat flap resolve↔reopen.
    """
    t = conn.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
    if t is None or not t["quorum"] or t["status"] == "wontfix":
        return
    names = json.loads(t["quorum"])
    active = _active_quorum(conn, names)
    verdicts = {
        r["author"]: r["verdict"]
        for r in conn.execute(
            "SELECT author, verdict FROM verdicts WHERE thread_id=?", (thread_id,)
        ).fetchall()
    }
    if t["status"] == "open":
        if len(active) >= 2 and all(verdicts.get(n) == "pass" for n in active):
            # v2.9 L1.5(a) discussion floor (thread #45 rev2) + v2.9.2 #49 A:
            # 修订边界 = comments.id 水位线（bump 事件固定位 token；同秒歧义
            # 已被 dsh 盘上实证，时间戳坐标废弃）。
            # v2.9.4: 判据抽成 _floor_missing（与手动关帖共用同一实现）
            silent, wm_why = _floor_missing(conn, thread_id)
            if silent is None:
                _audit_blocked_once(
                    conn, thread_id, t["revision"], "floor_wm_missing",
                    f"reason=floor_wm_missing: {wm_why}; 出口=新码下重发 bump_revision"
                    f"(清票重投,正常但昂贵,勿当卡死反复重试 — #511)")
                return
            if silent:
                _audit_blocked_once(
                    conn, thread_id, t["revision"], "discussion_floor",
                    f"reason=discussion_floor: {silent} 活跃成员本修订内无评论(先评论后判定)")
                return
            suspended = [n for n in names if n not in active]
            # v2.9.2 §3 (thread #48): future-tense delivery gate — the creator
            # must have RECEIVED every co-member comment (delivery watermark,
            # v2.6 last_delivered_id; third parties cannot forge it). P2: the
            # creator's OWN comments don't count as owed delivery. Blocked →
            # self-heals on the creator's next poll. 暂缓/失踪创建者跳过并留痕
            # （#48 定案"不把门挂在死人身上"）——判据=创建者实际活跃态，不用
            # quorum 席位代理（dsh #516②/pi #518§2：本板常态创建者不在 quorum，
            # 席位代理使跳过分支永不生效，暂缓创建者把帖永久挂死）。
            if t["author"]:
                crow = conn.execute(
                    "SELECT last_seen, last_delivered_id FROM participants WHERE author=?",
                    (t["author"],)
                ).fetchone()
                if crow is None or not _is_active(crow["last_seen"]):
                    _audit(conn, "system", "delivery_gate_skipped", thread_id=thread_id,
                           revision=t["revision"],
                           note=f"creator {t['author']} suspended/absent (C1) — "
                                f"门不挂死人(#48; dsh #516②/pi #518§2)")
                else:
                    maxid = conn.execute(
                        "SELECT COALESCE(MAX(id),0) FROM comments WHERE thread_id=? AND author<>?",
                        (thread_id, t["author"],)
                    ).fetchone()[0]
                    if (crow["last_delivered_id"] or 0) < maxid:
                        # v2.9.3 (#55): "自愈=创建者下次轮询" is now literally
                        # true — the cursor is written BEFORE this recompute, so
                        # the creator's next poll lands a current watermark.
                        _audit_blocked_once(
                            conn, thread_id, t["revision"], "creator_delivery_lag",
                            f"reason=creator_delivery_lag: delivered={crow['last_delivered_id'] or 0}"
                            f" < max_id={maxid}; 自愈=创建者下次轮询(游标先写后复算, #55)")
                        return
            # v2.9 wake_ledger gate (dsh R3): resolving while quorum members
            # sit suspended requires the creator's ledger of wake attempts —
            # 封死"零尝试躺等计时关门"(#44-F3)。
            if suspended:
                ok, missing = _wake_ledger_ok(conn, thread_id, t["author"], suspended)
                if not ok:
                    _note = (f"{'; '.join(missing)} (timeout/error 过门;"
                             f"缺条/not-attempted/suspicious 不过;"
                             f"锚点=^wake_ledger: 行首;#511 枚举不合并)")
                    # Dedupe key = the reason enum the note already carries
                    # (no_ledger / ledger_unparsable / floor_wm_missing), or the
                    # per-member split ledger_entry_missing / _suspicious.
                    _key = "ledger_entry_suspicious" if "suspicious" in _note \
                        else "ledger_entry_missing"
                    if _note.startswith("reason="):
                        _key = _note[7:].split(":", 1)[0].split("(", 1)[0].strip()
                    _audit_blocked_once(conn, thread_id, t["revision"], _key, _note)
                    return
            tally = " · ".join(f"{n} pass" for n in active)
            conn.execute("UPDATE threads SET status='resolved' WHERE id=?", (thread_id,))
            _audit(conn, "system", "auto_resolve", thread_id=thread_id,
                   revision=t["revision"], note=f"active quorum unanimous: {tally}")
    elif t["status"] == "resolved":
        # Only an ACTIVE objector can reopen (v2.9.3 #55): a suspended member's
        # object stays frozen until that member returns.
        standing = [n for n in active if verdicts.get(n) == "object"]
        if standing:
            conn.execute("UPDATE threads SET status='open' WHERE id=?", (thread_id,))
            _audit(conn, "system", "auto_reopen", thread_id=thread_id,
                   revision=t["revision"],
                   note=f"standing object by {standing} (active objector; "
                        f"frozen-at-resolve objects reopen only on unfreeze, C')")
        elif not verdicts:
            conn.execute("UPDATE threads SET status='open' WHERE id=?", (thread_id,))
            _audit(conn, "system", "auto_reopen", thread_id=thread_id,
                   revision=t["revision"], note="revision bump cleared verdicts")


def heartbeat_and_recompute(conn: sqlite3.Connection, author: str) -> None:
    """SHARED heartbeat entry (C1.1: probe and list_comments_since use the
    SAME code path): touch + invariant recompute for every quorum thread the
    author belongs to. Covers both directions: a returning member's frozen
    object re-standing on a resolved thread (C'), and a staleness-shrunken
    active set reaching unanimity on an open thread (auto-resolve fires on
    any member's next heartbeat)."""
    _touch(conn, author)
    rows = conn.execute(
        "SELECT id, quorum, author FROM threads"
        " WHERE status IN ('open','resolved') AND quorum IS NOT NULL"
    ).fetchall()
    for r in rows:
        # 创建者的帖也在列（dsh #516② 同根：out-of-quorum 创建者是本板常态
        # (#51 形态)——门的出口"自愈=创建者下次轮询"(v2.9.3 #55 起字面成立)
        # 要求创建者的轮询能够
        # 触达自己创建的帖，否则该出口对 #51 形态根本不存在。
        if author in json.loads(r["quorum"]) or r["author"] == author:
            recompute_thread(conn, r["id"])


def _parse_since(since: str) -> str:
    """Accept an ISO timestamp (returned verbatim) or a relative spec like
    '30m' / '2h' / '1d' / 'now'. Returns a UTC ISO timestamp string."""
    s = since.strip().lower()
    now = datetime.now(timezone.utc)
    if s in ("now", ""):
        return now.strftime("%Y-%m-%d %H:%M:%S")
    # relative: number + unit (m/h/d)
    if len(s) >= 2 and s[-1] in "mhd" and s[:-1].isdigit():
        n = int(s[:-1])
        unit = s[-1]
        delta_seconds = {"m": 60, "h": 3600, "d": 86400}[unit] * n
        from datetime import timedelta

        t = now - timedelta(seconds=delta_seconds)
        return t.strftime("%Y-%m-%d %H:%M:%S")
    # otherwise treat as an absolute timestamp the caller formatted the same way
    return since


@mcp.tool
def list_comments_since(since: str = "1h", author: str | None = None) -> str:
    """Poll for new comments — the ONE call that advances your last_read cursor.

    Passing `author` (always pass your own tool name when polling):
    - registers/heartbeats you (D1 liveness), and
    - computes the window as the UNION of `since` and your undelivered backlog
      (server-side last_read cursor): nothing already-undelivered is ever
      missed even if `since` is shorter (dsh-2). The cursor advances to this
      call's snapshot moment only here — get_thread/the probe/anything else
      never advances it (C3: cursor = "delivered to an LLM" watermark).

    Returns a one-line `needs_attention:` JSON header
    {new_comments, mentions_me[{thread_id,comment_id}], awaiting_my_verdict,
    open_threads} — idle polls can act on the header alone without reading
    threads (cost model §4). awaiting_my_verdict is a REAL list when `author`
    is passed (open ∧ non-wontfix ∧ quorum ∧ no verdict row at all ∧ active —
    bump_revision clears every row in the same transaction, so "no row" is
    revision-equivalent by construction, pi #362 pinned this; /attention's
    my_verdicts field disambiguates "bumped, re-vote" from "never voted");
    the "not_implemented" sentinel appears ONLY when
    author is omitted (dsh #89 minor-5: the old text claimed the field was
    unimplemented — members following it would ignore their top-priority
    signal).

    Args:
        since: absolute timestamp or relative '30m'/'2h'/'1d'/'now' (first-round
            fallback when no cursor exists yet).
        author: your tool name — poll WITH it, always.
    """
    snapshot = _now_iso()  # legacy wall-clock face (written below; see watermark)
    parsed_since = _parse_since(since)
    delivered = None
    cursor_id = 0
    if author:
        with _connect() as conn:
            row = conn.execute(
                "SELECT last_read, last_delivered_id FROM participants WHERE author=?",
                (author,),
            ).fetchone()
            if row:
                cursor_id = row["last_delivered_id"] or 0
                if row["last_read"]:
                    delivered = row["last_read"]
    cutoff = min(parsed_since, delivered) if delivered else parsed_since
    # v2.6 (issue #3): NULL last_read = never polled. Their first poll stays a
    # pure time window (exactly the v2.5 first-poll shape); from then on the
    # monotonic id watermark decides. This also keeps the invitee probe at
    # attention=1 (cursor 0 = nothing delivered yet) until the first read.
    use_id_window = bool(author and delivered)
    with db() as conn:
        # v2.6.1 (#374/#375/#376, all three independently reproduced): the
        # watermark MUST be derived from THIS call's delivered set, never
        # from a separate MAX(id) read taken AFTER the rows fetch. Python
        # sqlite3 runs SELECTs as autocommit per-statement snapshots, so a
        # post-fetch MAX could include a comment that committed between the
        # two statements — absent from rows AND <= the written cursor — a
        # permanent silent loss, the exact failure mode issue #3 exists to
        # kill. Deriving from rows is loss-free by AUTOINCREMENT write-lock
        # serialization: any comment absent from this snapshot committed
        # after it, so its id exceeds every id present here and the next
        # poll's `id > cursor` picks it up. Worst case is a duplicate,
        # never a loss; with the floor taken as below there is not even a
        # duplicate (watermark >= every id in rows by construction).
        floor = cursor_id
        if author and not use_id_window:
            # never-polled first read: floor from a MAX read taken BEFORE
            # the rows fetch (same ordering argument; post-read was the bug)
            floor = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM comments"
            ).fetchone()[0]
        # The delivery window is the union of the monotonic id watermark and
        # the `since` display window (the old shape was cutoff =
        # min(parsed_since, last_read) over one time axis). The no-author and
        # never-polled paths stay a pure time window — cursors are per-reader.
        if use_id_window:
            rows = conn.execute(
                """SELECT c.id, c.thread_id, c.parent_id, c.author, c.body,
                          c.file, c.line, c.severity, c.created_at
                   FROM comments c
                   WHERE c.id > ? OR c.created_at > ?
                   ORDER BY c.id ASC""",
                (cursor_id, parsed_since),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT c.id, c.thread_id, c.parent_id, c.author, c.body,
                          c.file, c.line, c.severity, c.created_at
                   FROM comments c
                   WHERE c.created_at > ?
                   ORDER BY c.id ASC""",
                (parsed_since,),
            ).fetchall()
        if author:
            watermark = floor
            for r in rows:
                if r["id"] > watermark:
                    watermark = r["id"]
            # v2.6 (issue #3): same-second comments can no longer be lost —
            # the old second-resolution last_read watermark did (strict >
            # over truncated timestamps; the old "no swallow race" note
            # assumed sub-second precision the format never had).
            # C1.1 shared heartbeat path (+ C' unfreeze recompute inside this
            # txn). last_read keeps its legacy wall-clock semantics for old
            # watcher kits — C3's single write point is unchanged (still only
            # this call writes either column).
            #
            # v2.9.3 (thread #55 ①, dsh #640: ordering defect) — CURSOR FIRST,
            # RECOMPUTE SECOND. recompute_thread's future-tense delivery gate
            # reads participants.last_delivered_id; with the old order (recompute
            # first, cursor last) every catch-up poll evaluated the gate against
            # the PREVIOUS cursor and therefore blocked itself
            # (reason=creator_delivery_lag: delivered=old < max_id). Same txn,
            # so the reorder is atomic; the fetch above already fixed delivery.
            # _touch must stay in front: the UPDATE below is a no-op for a
            # never-registered author (regression caught by test_v2_stage8/9).
            _touch(conn, author)
            conn.execute(
                "UPDATE participants SET last_read=?, last_delivered_id=?"
                " WHERE author=?",
                (snapshot, watermark, author),
            )
            heartbeat_and_recompute(conn, author)
            # v2.9.2 F6 (thread #48 P1): the ONE beat write point — only a real
            # poll feeds the rings; probes/invites/writes never do.
            # v2.9.3 票3 (#53): S2/S3 only count polls that actually delivered
            # unread content from OTHERS (id > previous cursor). Rows returned
            # by the display window may repeat already-delivered comments, and
            # a member's own comment is not a delivery — neither feeds hb_read.
            new_from_others = sum(1 for r in rows if r["id"] > cursor_id and r["author"] != author)
            _beat(conn, author, delivered=new_from_others)
    mentions = []
    if author:
        mentions = [
            {"thread_id": r["thread_id"], "comment_id": r["id"]}
            for r in rows
            if _mentioned(r["body"], author)
        ]
    awaiting: list | str
    with _connect() as conn:
        open_threads = conn.execute(
            "SELECT COUNT(*) FROM threads WHERE status='open'"
        ).fetchone()[0]
        if author:
            # D2: open ∧ 非 wontfix ∧ quorum ∧ no verdict on current revision ∧ active.
            # v2.9.4 结构性修复：拆成 可动作 / 被 L1.5 阻塞 两族（见 _awaiting_split）。
            # 旧实现在这里把"创建者被门挡住"也列进待办，导致 watcher 反复唤醒却
            # 只能拿到拒绝，三次后 C1.2 自锁（2026-10-24 实证）。
            awaiting, blocked_verdict = _awaiting_split(conn, author)
        else:
            awaiting = "not_implemented"
            blocked_verdict = []
    needs = {
        "new_comments": len(rows),
        "mentions_me": mentions,
        "awaiting_my_verdict": awaiting,
        # 我欠但投不了的票：列出我在等谁。**不触发唤醒**（避免空唤烧钱+自锁），
        # 但让成员知道该去催谁（协议 §W peer 唤起）。
        "blocked_my_verdict": blocked_verdict,
        "open_threads": open_threads,
    }
    header = "needs_attention: " + json.dumps(needs, ensure_ascii=False)
    if not rows:
        return f"{header}\nNo comments since {cutoff}."
    lines = [header, f"Comments since {cutoff} ({len(rows)}):"]
    for r in rows:
        loc = ""
        if r["file"]:
            loc = f" {r['file']}" + (f":{r['line']}" if r["line"] else "")
        sev = f" [{r['severity']}]" if r["severity"] else ""
        reply = f" (reply to #{r['parent_id']})" if r["parent_id"] else ""
        body_preview = str(r["body"]).replace("\n", " ")
        if len(body_preview) > 160:
            body_preview = body_preview[:157] + "..."
        lines.append(
            f"  #{r['id']} thread#{r['thread_id']} {r['author']}{sev}{reply}{loc} @ {r['created_at']}: {body_preview}"
        )
    return "\n".join(lines)


# --- v2.8: search_comments — cross-thread full-text read face ----------------
@mcp.tool
def search_comments(
    terms: list[str],
    author: str | None = None,
    syntax: str = SEARCH_SYNTAX_LITERAL,
    status: str | None = None,
    thread_id: int | None = None,
    after: str | None = None,
    before: str | None = None,
    limit: int = 20,
) -> str:
    """Cross-thread full-text search over comment bodies (v2.8, threads
    #39/#40). Pure read, ZERO governance semantics: hits are archaeology /
    digest material, NOT votes; this call never registers you, never
    heartbeats, never advances any cursor (C3) — `author` below is ONLY the
    is_self identity hint. 未命中≠不存在: a zero-hit answer means 'nothing
    matched' ONLY when status="ok" AND search_complete=true; timeout /
    unavailable can never be read as 'the board has none' (thread #29 rule,
    made machine-checkable here).

    Query contract (frozen; changes go through the protocol version gate):
    - terms: list of LITERAL phrase strings (1..12). The server performs NO
      segmentation — it never guesses Chinese word boundaries; multi-term
      queries are AND: every term must match.
    - Routing per term by Python len(str) CODEPOINTS (public contract):
      >= 3 -> FTS5 trigram MATCH; < 3 -> LIKE filter over the FTS-narrowed
      candidates; ALL terms < 3 -> full-table LIKE scan. mode.route reports
      which: "fts" | "fts+like" | "like_scan" (null when failure precedes
      routing).
    - syntax: "literal" (default) quotes every term as a literal FTS5 phrase
      (embedded double quotes doubled) — `游标*`, `a OR b`, `-term`,
      `col:term` are searched AS TEXT. "fts5" passes the terms through as a
      raw FTS5 expression (caller owns the semantics; invalid syntax returns
      status="error", never a silent fallback).
    - Case folding is route-inherent and documented, not normalized: the FTS
      path folds Unicode case (über matches ÜBER); the LIKE path folds ASCII
      only. No Unicode normalization is performed — an NFD query can miss an
      NFC body (known limitation, pinned by tests).
    - Execution: wall-clock budget (default 2000ms, REVIEWBOARD_SEARCH_TIMEOUT_MS)
      enforced at the SQLite VM level (progress-handler interrupt) and
      covering connect + lock wait; a timeout returns status="timeout",
      search_complete=false and NO partial results.
    - limit <= 50, no offset pagination (adding one is a version-gated
      semantic change). rank is the FTS5 bm25 sub-score of the >=3-char
      terms ONLY (smaller = more relevant; short terms filter but never
      affect ranking); like_scan orders by comment id DESC with rank=null.

    Response: JSON with status ("ok"|"timeout"|"unavailable"|"error"),
    mode {route, syntax} (present on positive AND zero-hit results),
    search_complete, results_truncated, total_count (meaningful only when
    complete), returned_count, and results[] — each hit carries comment_id /
    thread_id / thread_title / thread_status / author / timestamp / snippet
    (built around the first hit, <=200 codepoints) / rank / is_self (your own
    comments are MARKED, never filtered — an identity hint, not an
    independence proof) / context (the 2 time-adjacent same-thread comments,
    reading aids — NOT independent search hits).

    Args:
        terms: literal phrases to AND together.
        author: your tool name — is_self marking only (same口径 as
            list_comments_since; self-reported, unverified).
        syntax: "literal" (default) | "fts5".
        status: filter by thread status — 'open'|'resolved'|'wontfix'|'all'
            (default: no filter).
        thread_id: scope the search to one thread.
        after / before: comment time window (absolute 'YYYY-MM-DD HH:MM:SS'
            or relative '30m'/'2h'/'1d', same parser as list_comments_since).
        limit: max hits to return, 1..50 (default 20).
    """
    t0 = time.monotonic()
    budget_s = SEARCH_TIMEOUT_MS / 1000.0

    def payload(status, mode_route, results=None, *, total=None, truncated=None,
                extra=None, complete=None):
        body = {
            "status": status,
            "mode": {"route": mode_route, "syntax": syntax},
            "search_complete": complete if complete is not None else (status == "ok"),
            "total_count": total if status == "ok" else None,
            "returned_count": len(results) if results is not None else 0,
            "results": results or [],
        }
        if truncated is not None:
            body["results_truncated"] = truncated
        else:
            body["results_truncated"] = (
                total > len(results) if (status == "ok" and results is not None and total is not None)
                else None if status in ("timeout", "error", "unavailable")
                else False
            )
        if extra:
            body.update(extra)
        return json.dumps(body, ensure_ascii=False, indent=1)

    if not SEARCH_STATE["enabled"]:
        # Never a fake zero: the tool exists, the capability does not.
        return payload("unavailable", None,
                       extra={"disabled_reason": SEARCH_STATE["reason"]},
                       complete=False)
    # --- validation (failures precede routing: mode.route stays null) --------
    if (not isinstance(terms, list) or not terms
            or not all(isinstance(t, str) and t.strip() for t in terms)):
        return payload("error", None,
                       extra={"error": "terms must be a non-empty list of non-empty strings"},
                       complete=False)
    if len(terms) > SEARCH_MAX_TERMS:
        return payload("error", None,
                       extra={"error": f"at most {SEARCH_MAX_TERMS} terms"},
                       complete=False)
    if syntax not in (SEARCH_SYNTAX_LITERAL, SEARCH_SYNTAX_FTS5):
        return payload("error", None,
                       extra={"error": "syntax must be 'literal' or 'fts5'"},
                       complete=False)
    if status is not None and status not in ("open", "resolved", "wontfix", "all"):
        return payload("error", None,
                       extra={"error": "status must be open|resolved|wontfix|all"},
                       complete=False)
    if not isinstance(limit, int) or limit < 1 or limit > SEARCH_MAX_LIMIT:
        return payload("error", None,
                       extra={"error": f"limit must be 1..{SEARCH_MAX_LIMIT}"},
                       complete=False)

    # --- routing --------------------------------------------------------------
    if syntax == SEARCH_SYNTAX_FTS5:
        route = SEARCH_ROUTE_FTS
        match_expr = " ".join(terms)
    else:
        fts_terms = [t for t in terms if len(t) >= SEARCH_TERM_MIN_FTS]
        like_terms = [t for t in terms if len(t) < SEARCH_TERM_MIN_FTS]
        if like_terms and fts_terms:
            route = SEARCH_ROUTE_FTS_LIKE
        elif like_terms:
            route = SEARCH_ROUTE_LIKE_SCAN
        else:
            route = SEARCH_ROUTE_FTS
        match_expr = " AND ".join(_fts_literal(t) for t in fts_terms) if fts_terms else None

    where = []
    params: list = []
    if route in (SEARCH_ROUTE_FTS, SEARCH_ROUTE_FTS_LIKE):
        where.append("comments_fts MATCH ?")
        params.append(match_expr)
    for t in (like_terms if syntax == SEARCH_SYNTAX_LITERAL else []):
        where.append("c.body LIKE ? ESCAPE '\\'")
        params.append(f"%{_like_literal(t)}%")
    if thread_id is not None:
        where.append("c.thread_id = ?")
        params.append(thread_id)
    if status is not None and status != "all":
        where.append("th.status = ?")
        params.append(status)
    if after is not None:
        where.append("c.created_at > ?")
        params.append(_parse_since(after))
    if before is not None:
        where.append("c.created_at < ?")
        params.append(_parse_since(before))
    where_sql = " AND ".join(where) if where else "1=1"

    fts_from = ("comments_fts JOIN comments c ON c.id = comments_fts.rowid"
                if route in (SEARCH_ROUTE_FTS, SEARCH_ROUTE_FTS_LIKE) else "comments c")
    threads_join = " JOIN threads th ON th.id = c.thread_id" if (
        status is not None and status != "all") else " LEFT JOIN threads th ON th.id = c.thread_id"
    order = "rank" if route in (SEARCH_ROUTE_FTS, SEARCH_ROUTE_FTS_LIKE) else "c.id DESC"
    rank_sel = "rank" if route in (SEARCH_ROUTE_FTS, SEARCH_ROUTE_FTS_LIKE) else "NULL AS rank"

    conn = _connect_ro()
    deadline = t0 + budget_s
    try:
        if time.monotonic() >= deadline:  # connect/lock wait already ate the budget (>=: a zero budget must abort deterministically even inside one OS timer tick — dsh #432)
            return payload("timeout", route, complete=False)
        conn.set_progress_handler(
            lambda: 1 if time.monotonic() >= deadline else 0, 500)
        base_sql = (
            f"SELECT c.id, c.thread_id, c.author, c.body, c.created_at,"
            f" th.title AS th_title, th.status AS th_status, {rank_sel}"
            f" FROM {fts_from}{threads_join} WHERE {where_sql}")
        total = conn.execute(
            f"SELECT COUNT(*) FROM {fts_from}{threads_join} WHERE {where_sql}",
            params).fetchone()[0]
        rows = conn.execute(
            base_sql + f" ORDER BY {order} LIMIT ?", params + [limit]).fetchall()
        results = []
        for r in rows:
            # Two simple neighbor queries (a parenthesized UNION ALL compound
            # is a syntax error on SQLite < 3.41 — version-proof beats clever).
            ctx_rows = conn.execute(
                "SELECT id, author, body FROM comments"
                " WHERE thread_id=? AND (created_at, id) < (?, ?)"
                " ORDER BY created_at DESC, id DESC LIMIT 1",
                (r["thread_id"], r["created_at"], r["id"]),
            ).fetchall() + conn.execute(
                "SELECT id, author, body FROM comments"
                " WHERE thread_id=? AND (created_at, id) > (?, ?)"
                " ORDER BY created_at ASC, id ASC LIMIT 1",
                (r["thread_id"], r["created_at"], r["id"]),
            ).fetchall()
            ctx = sorted(ctx_rows, key=lambda n: n["id"])
            results.append({
                "comment_id": r["id"],
                "thread_id": r["thread_id"],
                "thread_title": r["th_title"],
                "thread_status": r["th_status"],
                "author": r["author"],
                "timestamp": r["created_at"],
                "snippet": _search_snippet(r["body"], terms, SEARCH_SNIPPET_CAP),
                "rank": r["rank"],
                "is_self": bool(author and r["author"] == author),
                "context": [
                    {"comment_id": n["id"], "author": n["author"],
                     "snippet": _search_snippet(n["body"], terms, SEARCH_CONTEXT_CAP)}
                    for n in ctx
                ],
            })
        return payload("ok", route, results, total=total)
    except sqlite3.Error as e:
        if time.monotonic() >= deadline:
            return payload("timeout", route, complete=False)
        return payload("error", route,
                       extra={"error": _oneline(str(e), 200)}, complete=False)
    finally:
        try:
            conn.set_progress_handler(None, 0)
        except sqlite3.Error:
            pass
        conn.close()


# --- v2.2 P2: reliability profile (derived-only, zero governance weight) ------
METRIC_VERSION = "2.2-r3"
INSUFFICIENT_AT = 5  # judgment section flags 数据不足 below this many first verdicts


def _ts(utc: str | None) -> datetime | None:
    if not utc:
        return None
    try:
        return datetime.strptime(utc, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _lag_band(secs: float) -> str:
    if secs < 86400:
        return "当天内"
    if secs < 3 * 86400:
        return "3 天内"
    return "更久"


_LAG_ORDER = {"当天内": 0, "3 天内": 1, "更久": 2}


def _profile_numbers(conn: sqlite3.Connection, author: str) -> dict:
    """v2.2 reliability profile — THE one aggregation (tool and board share it).

    Derived-only: pure reads; callers pass a query_only connection (附录 F2)
    so even a buggy or hostile aggregation cannot write. Zero governance
    weight — test_v2_stage6 part B enforces it by randomizing/attacking this
    function and asserting identical governance outcomes.

    Appendix F1 invariant: every objection-derived metric is computed from the
    episode's LOCAL revision window; thread-level terminal stance NEVER
    classifies a historical episode. Episode = (author, thread, revision)
    first-verdict object — enforced by the adversarial sequence matrix
    (O→P / O→O / O→P→O / O→P→O→P / O→O→P / O→O→O …) in test_v2_stage6.

    Participation facts the board deliberately does NOT persist (heartbeat
    history, watcher-gate periods) are reported as CURRENT state only, never
    fabricated from comment gaps — a silent-but-alive member must not be
    misread as stalled (the attribution error the v2.2 review rejected).
    """
    p = conn.execute(
        "SELECT author, first_seen, last_seen, meta FROM participants WHERE author=?",
        (author,),
    ).fetchone()
    if p is None:
        return {"author": author, "error": "not registered — any author-carrying call registers"}
    try:
        meta = json.loads(p["meta"] or "{}")
    except (ValueError, TypeError):
        meta = {}

    now = datetime.now(timezone.utc)
    threads = conn.execute(
        "SELECT id, status, quorum, created_at FROM threads WHERE quorum IS NOT NULL"
    ).fetchall()
    thread_created = {t["id"]: t["created_at"] for t in threads}
    thread_status = {t["id"]: t["status"] for t in threads}

    # ---- participation (observable facts, no endogenous ground truth) ----
    suspended = not _is_active(p["last_seen"])
    wake_err = meta.get("last_wake_error") or None

    awaiting_threads: list[int] = []
    oldest_pending_band = None
    commented_tids = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT thread_id FROM comments WHERE author=?", (author,))
    }
    ctv_n = ctv_d = vote_n = vote_d = 0
    for t in threads:
        if author not in json.loads(t["quorum"]):
            continue
        has_own = conn.execute(
            "SELECT 1 FROM verdicts WHERE thread_id=? AND author=?", (t["id"], author)
        ).fetchone() is not None
        if t["id"] in commented_tids:
            ctv_d += 1
            if has_own:
                ctv_n += 1
        others_voted = conn.execute(
            "SELECT 1 FROM verdicts WHERE thread_id=? AND author != ? LIMIT 1",
            (t["id"], author),
        ).fetchone() is not None
        if t["status"] != "open" or others_voted:
            vote_d += 1
            if has_own:
                vote_n += 1
        if t["status"] == "open" and not has_own:
            awaiting_threads.append(t["id"])
            last_act = conn.execute(
                "SELECT MAX(created_at) FROM comments WHERE thread_id=?", (t["id"],)
            ).fetchone()[0]
            ts = _ts(last_act) or _ts(thread_created.get(t["id"]))
            if ts:
                band = _lag_band((now - ts).total_seconds())
                if oldest_pending_band is None or _LAG_ORDER[band] > _LAG_ORDER[oldest_pending_band]:
                    oldest_pending_band = band
    participation = {
        "heartbeat": {
            "suspended_now": suspended,
            "last_seen": _relative(p["last_seen"]),
            "first_seen": _relative(p["first_seen"]),
        },
        "watcher_gate": {"currently_gated": bool(wake_err)},
        "awaiting_verdict": {
            "threads": awaiting_threads,
            "oldest_pending_band": oldest_pending_band,
        },
        "comment_to_verdict": {
            "n": ctv_n, "d": ctv_d,
            "note": "quorum 线程中发过言的,最终投过判定(任意修订)的比例;未校验预算内/时序",
        },
        "vote_coverage": {
            "n": vote_n, "d": vote_d,
            "due": "线程已关闭(status≠open)或已有其他成员投票",
        },
        "note": ("历史停摆次数/闸门区间未持久化(derived-only,无新表)——只报当前态;"
                 "A1 ⚠️ 徽章即同一信号,画像不吞掉它"),
    }
    if wake_err:
        participation["watcher_gate"]["last_wake_error"] = wake_err

    # ---- judgment (endogenous ground truth — display only) ----
    # verdict_free and verdict carry the SAME judgment semantics (only the
    # billing differs — first verdict per (author, revision) is free); the
    # profile treats them uniformly as verdicts (附录 F3).
    events = conn.execute(
        "SELECT id, thread_id, revision, to_v, created_at FROM verdict_events"
        " WHERE author=? AND action IN ('verdict','verdict_free') ORDER BY id",
        (author,),
    ).fetchall()
    first_by_tr: dict[tuple[int, int], sqlite3.Row] = {}
    stance_by_tr: dict[tuple[int, int], str] = {}   # window-final stance per (tid, rev)
    for e in events:
        first_by_tr.setdefault((e["thread_id"], e["revision"]), e)
        stance_by_tr[(e["thread_id"], e["revision"])] = e["to_v"]
    total_firsts = len(first_by_tr)
    object_firsts = sum(1 for e in first_by_tr.values() if e["to_v"] == "object")

    # stance flips: consecutive verdict events on a thread where the stance
    # changed. Split by whether the two events sit in DIFFERENT revisions —
    # an object withdrawn after a revision is evidence-driven (healthy);
    # withdrawn within the same revision is stance instability (claude #68:
    # G2 义务性改判 ≠ 立场不稳).
    per_thread: dict[int, list[sqlite3.Row]] = {}
    for e in events:
        per_thread.setdefault(e["thread_id"], []).append(e)
    flips_cross = flips_same = 0
    for evs in per_thread.values():
        for prev, cur in zip(evs, evs[1:]):
            if prev["to_v"] != cur["to_v"]:
                if cur["revision"] > prev["revision"]:
                    flips_cross += 1
                else:
                    flips_same += 1

    # prefetches (keeps the loop O(1) per episode instead of O(episodes) SQL):
    # bump (author, time) by the revision it CREATED; every member's first
    # stance per (thread, revision) — for co-objection.
    bump_by_tr: dict[tuple[int, int], tuple[str, str]] = {}
    for b in conn.execute(
        "SELECT thread_id, revision, author, created_at FROM verdict_events"
        " WHERE action='bump_revision' ORDER BY id"
    ).fetchall():
        bump_by_tr.setdefault((b["thread_id"], b["revision"]), (b["author"], b["created_at"]))
    first_stance_all: dict[tuple[int, int], dict[str, str]] = {}
    for o in conn.execute(
        "SELECT thread_id, revision, author, to_v FROM verdict_events"
        " WHERE action IN ('verdict','verdict_free') ORDER BY id"
    ).fetchall():
        first_stance_all.setdefault((o["thread_id"], o["revision"]), {}).setdefault(
            o["author"], o["to_v"])

    outcomes = {"revision_absorbed": 0, "active_overridden": 0,
                "frozen_unadjudicated": 0, "wontfix": 0, "self_loop_excluded": 0,
                "continued": 0}
    co_obj = lone = 0
    latency = {"当天内": 0, "3 天内": 0, "更久": 0}   # gate_excluded dropped in 2.2-r3
    for (tid, rev), e in first_by_tr.items():
        # latency band for every first verdict (revision start → verdict — both
        # append-only events; no gate-period attribution, it was unreliable)
        t0 = None
        if rev and rev > 1:
            b = bump_by_tr.get((tid, rev))
            if b:
                t0 = _ts(b[1])
        if t0 is None:
            t0 = _ts(thread_created.get(tid))
        t1 = _ts(e["created_at"])
        if t0 and t1:
            latency[_lag_band((t1 - t0).total_seconds())] += 1
        if e["to_v"] != "object":
            continue
        # --- episode-local state machine (附录 F1): classify THIS episode from
        # ITS revision window only; thread-terminal stance never reaches here.
        if stance_by_tr.get((tid, rev)) == "pass":
            outcomes["active_overridden"] += 1   # withdrawn inside R, no revision involved
        else:                                     # still standing when R ended
            bump = bump_by_tr.get((tid, rev + 1))
            if bump is None:
                # no successor revision: episode standing at the thread's end
                if thread_status.get(tid) == "wontfix":
                    outcomes["wontfix"] += 1
                else:
                    outcomes["frozen_unadjudicated"] += 1
            else:
                next_stance = stance_by_tr.get((tid, rev + 1))
                if next_stance == "pass":
                    if bump[0] == author:
                        outcomes["self_loop_excluded"] += 1
                    else:
                        outcomes["revision_absorbed"] += 1
                elif next_stance == "object":
                    outcomes["continued"] += 1     # objection carried into R+1
                else:
                    outcomes["frozen_unadjudicated"] += 1  # absent on R+1
        # co-objection (NOT verification): another member's FIRST verdict on
        # the same (thread, revision) also landed 'object' — the anti-self-play
        # antibody; co-objection proves independent agreement to object only.
        stances = first_stance_all.get((tid, rev), {})
        if any(a != author and v == "object" for a, v in stances.items()):
            co_obj += 1
        else:
            lone += 1

    judgment = {
        "first_verdicts": total_firsts,
        "object_firsts": object_firsts,
        "object_rate": f"{object_firsts}/{total_firsts}",
        "outcomes": outcomes,
        "stance_flips": {"cross_revision_flips": flips_cross,
                         "same_revision_flips": flips_same},
        "co_objection": {"co_objected": co_obj, "lone": lone},
        "insufficient": total_firsts < INSUFFICIENT_AT,
    }

    latency["contract"] = meta.get("contract") if meta.get("contract") in ("a", "b") else None
    latency["note"] = ("revision 开始→首判,带宽化展示(无中位数,防速判激励);"
                       "契约 (a)/(b) 分层读 meta.contract(未记录为 null);"
                       "无闸门期归因(2.2-r3 起移除 gate_excluded——last_wake_error "
                       "为当前态,历史归因不可靠)")
    return {
        "author": author,
        "metric_version": METRIC_VERSION,
        "derived_only": True,
        "governance_weight": 0,
        "participation": participation,
        "judgment": judgment,
        "latency": latency,
    }


def _profile_brief(prof: dict) -> str:
    """One-line board summary — raw components only, NEVER a composite score
    (protocol v2.2: ✓78%-style rankable scalars are the red line made UI).
    Defensive .get access so a stubbed/broken aggregation can never 500 the
    board — display layer states facts, it does not propagate profile errors."""
    j = prof.get("judgment") if isinstance(prof, dict) else None
    if not isinstance(j, dict) or j.get("first_verdicts", 0) < INSUFFICIENT_AT:
        return "画像:数据不足"
    o = j.get("outcomes") or {}
    return (f"首判{j.get('first_verdicts', 0)}"
            f" 吸收{o.get('revision_absorbed', 0)}"
            f"/否决{o.get('active_overridden', 0)}"
            f"/冻结{o.get('frozen_unadjudicated', 0)} v{prof.get('metric_version')}")


@mcp.tool
def reliability_profile(author: str) -> str:
    """Read-only derived behavior profile for a member (v2.2, metric 2.2-r3).

    An OBSERVATIONAL statistic, not a verdict on anyone: `participation`
    (observable facts — heartbeat/gate state, awaiting verdicts,
    comment_to_verdict & vote_coverage rates; no endogenous ground truth) and
    `judgment` (episode-local five-way classification of every objection:
    revision_absorbed / active_overridden / frozen_unadjudicated / wontfix /
    continued, self-loops excluded from the positive; object_rate with the
    numerator pinned to first-verdict stance per (author, revision); stance
    flips split by whether they cross a revision; co_objection vs lone).
    verdict_free and verdict are the same judgment event (billing differs).

    Raw components only — NO composite score, by protocol. Zero governance
    weight: never enters any decision path (behavior-invariance tested);
    pull-only; derived on read over a query_only connection (no writes
    possible at the SQLite layer, 附录 F2).

    Args:
        author: the member to profile (your own name or another's).
    """
    conn = _connect_ro()
    try:
        data = _profile_numbers(conn, author)
    finally:
        conn.close()
    return json.dumps(data, ensure_ascii=False, indent=1)


# --- read-only HTML board --------------------------------------------------
def _board_html() -> str:
    with _connect() as conn:
        threads = conn.execute(
            """SELECT t.*, (SELECT COUNT(*) FROM comments c WHERE c.thread_id = t.id) AS n
               FROM threads t ORDER BY t.id DESC"""
        ).fetchall()
        by_thread: dict[int, list[sqlite3.Row]] = {}
        for c in conn.execute("SELECT * FROM comments ORDER BY created_at ASC").fetchall():
            by_thread.setdefault(c["thread_id"], []).append(c)
        participants = conn.execute(
            "SELECT author, last_seen, meta FROM participants ORDER BY last_seen DESC"
        ).fetchall()
        # awaiting map: open quorum threads -> members with no verdict yet (D2 filter:
        # the single same filter the tool path uses; display reuses it, no drift)
        awaiting_map: dict[int, list[str]] = {}
        for t in threads:
            if t["quorum"] and t["status"] == "open":
                names = json.loads(t["quorum"])
                have = {
                    r["author"]
                    for r in conn.execute(
                        "SELECT author FROM verdicts WHERE thread_id=?", (t["id"],)
                    ).fetchall()
                }
                active = _active_quorum(conn, names)
                miss = [n for n in names if n not in have and n in active]
                if miss:
                    awaiting_map[t["id"]] = miss
        # v2.2: per-participant profile briefs — same aggregation the tool uses
        # (_profile_numbers), raw components only, on a query_only connection
        # (附录 F2): a broken/hostile aggregation can never mutate data from
        # the display path either.
        briefs: dict[str, str] = {pp["author"]: "" for pp in participants}
        try:
            roc = _connect_ro()
            try:
                for pp in participants:
                    try:
                        briefs[pp["author"]] = _profile_brief(
                            _profile_numbers(roc, pp["author"]))
                    except Exception:
                        briefs[pp["author"]] = ""  # display never breaks on profile errors
            finally:
                roc.close()
        except Exception:
            pass

    strip = []
    for p in participants:
        try:
            meta = json.loads(p["meta"] or "{}")
        except (ValueError, TypeError):
            meta = {}
        last = p["last_seen"]
        try:
            then = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            idle = (datetime.now(timezone.utc) - then).total_seconds()
        except ValueError:
            idle = float("inf")
        mark = "🟢" if idle < DISPLAY_IDLE_MIN * 60 else "⚠️"
        err = ""
        if meta.get("last_wake_error"):
            err = f" <span title='watcher wake failures'>🔧</span>"
        brief = briefs.get(p["author"], "")
        brief_html = f" <span class='meta'>{escape(brief)}</span>" if brief else ""
        strip.append(
            f"<span class='p'>{mark} {escape(p['author'])} {_relative(last)}{err}{brief_html}</span>"
        )

    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<title>MCP Review Board</title>",
        "<style>",
        "body{font-family:system-ui,sans-serif;max-width:1000px;margin:24px auto;padding:0 16px;color:#222}",
        "h1{font-size:1.4rem} h2{font-size:1.1rem;margin-top:1.5em;border-bottom:1px solid #ddd;padding-bottom:4px}",
        ".meta{color:#888;font-size:.85rem} .badge{font-size:.75rem;padding:1px 6px;border-radius:3px;background:#eee;margin-left:6px}",
        ".open{color:#b35900}.resolved{color:#1a7f37}.wontfix{color:#888}.blocker{background:#ffe0e0}.major{background:#fff3e0}",
        "pre{background:#f6f8fa;padding:8px 12px;border-radius:6px;white-space:pre-wrap;word-wrap:break-word}",
        ".c{margin:8px 0}.reply{margin-left:24px;border-left:2px solid #eee;padding-left:12px}",
        ".p{margin-right:14px;font-size:.85rem} .await{background:#fff3cd;padding:1px 8px;border-radius:10px;font-size:.75rem}",
        "details{margin-top:2em} summary{cursor:pointer;color:#888;font-size:.85rem}",
        "</style></head><body>",
        "<h1>📋 MCP Review Board</h1>",
        f"<p class='meta'>{len(threads)} thread(s) · auto-refreshes 20s · "
        f"<a href='/'>refresh</a> · probe: <code>/attention?author=NAME</code></p>",
        f"<p>{' '.join(strip) if strip else '<span class=meta>(no participants yet)</span>'}</p>",
    ]
    if not threads:
        parts.append("<p>No threads yet. Have an agent call <code>create_thread</code>.</p>")
    for t in threads:
        aw = awaiting_map.get(t["id"])
        aw_html = (
            f" <span class='await'>⏳ awaiting verdict: {', '.join(escape(a) for a in aw)}</span>"
            if aw else ""
        )
        parts.append(
            f"<h2>#{t['id']} {escape(t['title'])} "
            f"<span class='badge {t['status']}'>{t['status']}</span>{aw_html}</h2>"
        )
        parts.append(f"<div class='meta'>{t['n']} comment(s) · created {t['created_at']}</div>")
        if t["context"]:
            parts.append(f"<pre>{escape(t['context'])}</pre>")

        comments = by_thread.get(t["id"], [])
        by_parent: dict[int | None, list[sqlite3.Row]] = {}
        for c in comments:
            by_parent.setdefault(c["parent_id"], []).append(c)

        def render(c: sqlite3.Row, depth: int) -> None:
            loc = ""
            if c["file"]:
                loc = f" · <code>{escape(str(c['file']))}" + (
                    f":{c['line']}" if c["line"] else ""
                ) + "</code>"
            sev = (
                f" <span class='badge {c['severity']}'>{c['severity']}</span>"
                if c["severity"]
                else ""
            )
            st = (
                f" <span class='badge {c['status']}'>{c['status']}</span>"
                if c["status"] != "open"
                else ""
            )
            cls = "c reply" if depth > 0 else "c"
            parts.append(
                f"<div class='{cls}'>"
                f"<div><b>#{c['id']} {escape(c['author'])}</b>{sev}{st}{loc} "
                f"<span class='meta'>{c['created_at']}</span></div>"
                f"<pre>{escape(c['body'])}</pre></div>"
            )
            for child in by_parent.get(c["id"], []):
                render(child, depth + 1)

        for top in by_parent.get(None, []):
            render(top, 0)
        if not comments:
            parts.append("<p class='meta'>(no comments yet)</p>")

    parts.append(
        "<details><summary>协议 protocol v" + PROTOCOL_VERSION + "</summary>"
        "<pre style='font-size:.8rem'>" + escape(PROTOCOL) + "</pre></details>"
    )
    parts.append(
        "<script>setInterval(function(){if(!document.hidden){location.reload();}},20000);</script>"
    )
    parts.append("</body></html>")
    return "".join(parts)


# --- v2 A4/5: the lightweight heartbeat probe -------------------------------
@mcp.custom_route("/ping", methods=["GET"])
async def ping(request: Request):
    """Health/liveness endpoint (Glama listing check + generic watchers):
    plain 200 as long as the process serves — no auth, no db touch. v2.8:
    carries the search capability face (in-memory startup state only)."""
    return JSONResponse({
        "status": "ok",
        "version": PROTOCOL_VERSION,
        "search": {"enabled": SEARCH_STATE["enabled"],
                   "reason": SEARCH_STATE["reason"],
                   "sqlite": SEARCH_STATE["sqlite"]},
    })


_STARTED_AT = time.time()  # v2.5 observability: /attention uptime face
_PROBE_ERRORS = 0          # probe-path exception counter (best-effort; no heavy monitoring)


@mcp.custom_route("/guard", methods=["GET"])
async def guard_endpoint(request: Request):
    """v2.9.2 (#47 ③/dsh H2): read-only guard data for L2 hooks — endpoint-first,
    no DB file dependency (the R6 死路), no WAL ambiguity. Zero writes: never
    touches participants/cursors/heartbeats (C3). `forms` is a null placeholder
    until L1.4 meta adoption fills it (contract per pi C2: consistency only)."""
    tid_raw = request.query_params.get("thread")
    try:
        tid = int(tid_raw or "")
    except ValueError:
        return JSONResponse({"error": "thread query param must be an integer"}, status_code=400)
    with _connect() as conn:
        conn.execute("PRAGMA query_only=1")
        t = conn.execute(
            "SELECT id, title, author, quorum, revision, status FROM threads WHERE id=?",
            (tid,),
        ).fetchone()
        if t is None:
            return JSONResponse({"error": f"thread {tid} not found"}, status_code=404)
        verdicts = {
            r["author"]: {"verdict": r["verdict"], "updated_at": r["updated_at"]}
            for r in conn.execute(
                "SELECT author, verdict, updated_at FROM verdicts WHERE thread_id=?", (tid,)
            ).fetchall()
        }
        quorum = json.loads(t["quorum"]) if t["quorum"] else []
        participants = {}
        for n in quorum:
            r = conn.execute(
                "SELECT last_seen FROM participants WHERE author=?", (n,)
            ).fetchone()
            participants[n] = {"last_seen": r["last_seen"] if r else None}
        if t["author"]:
            r = conn.execute(
                "SELECT last_seen, last_delivered_id FROM participants WHERE author=?",
                (t["author"],),
            ).fetchone()
            participants[t["author"]] = {
                "last_seen": r["last_seen"] if r else None,
                "last_delivered_id": r["last_delivered_id"] if r else None,
            }
    return JSONResponse({
        "thread": {"id": t["id"], "creator": t["author"], "quorum": quorum,
                   "revision": t["revision"], "status": t["status"]},
        "verdicts": verdicts,
        "participants": participants,
        "forms": None,
    })


@mcp.custom_route("/attention", methods=["GET"])
async def attention_probe(request: Request):
    """GET /attention?author=NAME — the dumb watcher pre-check (dsh-3).

    One curl + one boolean branch, no MCP envelope. C1.1: runs the FULL
    heartbeat transaction via the SAME heartbeat_and_recompute code path as
    MCP polls (touch + C' unfreeze recompute, single SQLite write txn, idempotent).
    C3: NEVER advances last_read — the signal stays level-triggered (attention
    stays 1 across wake failures until list_comments_since actually delivers).

    v2.5 (thread #33) response and semantics:
    - "reason": "verdict_stale" — the prober's standing object has >=2 of
      their OWN newer comments (priority: awaiting > stale > mention > new).
    - "budget": {tid: {used, cap}} — the REAL quota face (comments + costed
      flips), the same _author_usage the gates use.
    - "my_verdicts": {tid: {verdict, revision}} — lets a member tell
      "bumped, re-vote" from "never voted" (awaiting alone cannot).
    - "uptime_secs" / "errors" — liveness faces for the watcher kit.
    - The prober's OWN comments are excluded from the undelivered scan
      (B-2, relocated server-side — the watcher payload never carried
      comment authors, so watcher-side filtering was impossible): a
      member's own post must not re-raise their attention; others'
      comments still wake them. list_comments_since (the read face) still
      returns own comments untouched, and last_read semantics are
      unchanged (C3 single write point).
    """
    author = request.query_params.get("author")
    if not author:
        return JSONResponse({"error": "author query param required"}, status_code=400)
    global _PROBE_ERRORS
    try:
        with db() as conn:
            heartbeat_and_recompute(conn, author)
            # v2.9.3 票3 (#53): the probe is a zero-LLM-cost presence signal —
            # it feeds S1 freshness and the channel-2 probe ring, never the
            # read-delivery ring (S2/S3 stay proof-of-read).
            _probe(conn, author)
        with _connect() as conn:
            row = conn.execute(
                "SELECT last_read, last_delivered_id FROM participants WHERE author=?",
                (author,),
            ).fetchone()
            cutoff = (row["last_read"] if row and row["last_read"] else "1970-01-01 00:00:00")
            cursor_id = (row["last_delivered_id"] if row else 0) or 0
            open_quorum = [
                r["id"]
                for r in conn.execute(
                    "SELECT id, quorum FROM threads WHERE status='open' AND quorum IS NOT NULL"
                ).fetchall()
                if author in json.loads(r["quorum"])
            ]
            # v2.9.4 结构性修复：可动作 / 被 L1.5 阻塞 两族分开（见 _awaiting_split）。
            # **attention 只由可动作项触发**——否则创建者会被"自己投不了的票"反复唤醒。
            awaiting, blocked_verdict = _awaiting_split(conn, author)
            # v2.5 (thread #33): the holder's OWN continued posting over a standing
            # object wakes them to reconsider — priority BELOW awaiting_verdict
            # (an uncast vote outranks a stale one), ABOVE mentions/new_comments.
            stale_self = [
                tid for tid in open_quorum
                if author in _stale_object_holders(conn, tid)
            ]
            # v2.5 (thread #33 ④-1): the real quota face — members were billed
            # blind; the only visible number was comments-only (wrong face).
            budget_face = {}
            my_state = {}
            for tid in open_quorum:
                trow = conn.execute(
                    "SELECT per_author_budget, revision FROM threads WHERE id=?", (tid,)
                ).fetchone()
                v = conn.execute(
                    "SELECT verdict FROM verdicts WHERE thread_id=? AND author=?",
                    (tid, author),
                ).fetchone()
                budget_face[str(tid)] = {
                    "used": _author_usage(conn, tid, author),
                    "cap": trow["per_author_budget"],
                }
                my_state[str(tid)] = {
                    "verdict": v["verdict"] if v else None,
                    "revision": trow["revision"],
                }
            undelivered = conn.execute(
                # v2.6 (issue #3): ids above the monotonic watermark, not
                # created_at > last_read — same-second comments used to fall
                # through the strict > on truncated timestamps. The v2.5
                # own-comment exclusion (author != ?) carries over unchanged.
                "SELECT thread_id, id, body FROM comments"
                " WHERE id > ? AND author != ? ORDER BY id",
                (cursor_id, author),
            ).fetchall()
            mention_threads = sorted(
                {r["thread_id"] for r in undelivered if _mentioned(r["body"], author)}
            )
        if awaiting:
            reason, threads = "awaiting_verdict", awaiting
        elif stale_self:
            reason, threads = "verdict_stale", stale_self
        elif mention_threads:
            reason, threads = "mentioned", mention_threads
        elif undelivered:
            reason, threads = "new_comments", sorted({r["thread_id"] for r in undelivered})
        else:
            reason, threads = "idle", []
        return JSONResponse({
            "attention": 1 if reason != "idle" else 0,
            "reason": reason,
            "threads": threads,
            "open_threads": open_quorum,
            "budget": budget_face,
            "my_verdicts": my_state,
            # v2.9.4：我欠票但被 L1.5 挡住的那些帖，附"在等谁"。**不参与 attention 判定**
            # （不触发唤醒、不烧钱），只作为可见事实供成员去催票（协议 §W peer 唤起）。
            "blocked_my_verdict": blocked_verdict,
            # thread #31 batch A: effective cutoff (NULL→epoch sentinel, the same
            # value the probe itself uses above) for the watcher delivery judgment
            # (delivered = fingerprint moved OR last_read advanced). Advanced ONLY
            # by list_comments_since(author) — never by this probe (C3, single
            # write point), so a watcher cannot fake its own member's delivery.
            "last_read": cutoff,
        # v2.6 (issue #3): the delivery-truth watermark (monotonic comment
        # id). Watcher kits prefer this face for delivery judgment — it can
        # advance within the same wall-clock second where last_read cannot.
        "last_delivered_id": cursor_id,
            "board_id": BOARD_INSTANCE_ID,  # stable instance identity (thread #27):
            # a watcher pinned to EXPECTED_BOARD_ID can detect that this port now
            # serves a DIFFERENT board and refuse to wake members against it.
            "uptime_secs": int(time.time() - _STARTED_AT),
            "errors": _PROBE_ERRORS,
        })
    except Exception:
        _PROBE_ERRORS += 1
        raise


@mcp.custom_route("/", methods=["GET"])
async def homepage(request: Request) -> HTMLResponse:
    """Read-only HTML board served at the root. FastMCP serves the MCP
    endpoint itself at /mcp; @custom_route adds this page alongside it without
    any manual Starlette mounting (which caused a /mcp → /mcp/ redirect trap)."""
    return HTMLResponse(_board_html())


def main() -> None:
    init_db()
    print(f"MCP Review Board → http://{HOST}:{PORT}/  (MCP endpoint: /mcp)")
    print(f"DB: {DB_PATH}")
    if SEARCH_STATE["enabled"]:
        print(f"Search: enabled (FTS5 trigram, sqlite {SEARCH_STATE['sqlite']})")
    else:
        print(f"Search: DISABLED ({SEARCH_STATE['reason']}) — core board unaffected")
    # stateless_http: no server-side session state — restarts/upgrades never
    # invalidate live client connections (all state lives in SQLite).
    # mcp.run manages uvicorn + the MCP session manager + custom routes together.
    mcp.run(transport="http", host=HOST, port=PORT, stateless_http=True)


if __name__ == "__main__":
    main()
