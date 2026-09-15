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
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

# --- paths -----------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = Path(os.environ.get("REVIEWBOARD_DB", str(DATA_DIR / "reviewboard.db")))
SCHEMA_PATH = BASE_DIR / "schema.sql"

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
mcp = FastMCP("ReviewBoard")
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
            " VALUES (?,?,?,NULL,'{}')",
            (author, now, now),
        )


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


# --- v2 A2: the protocol ----------------------------------------------------
PROTOCOL_VERSION = "2.0-stage1"
PROTOCOL = f"""# Review Board 协议 v{PROTOCOL_VERSION}

## 成员
- 开放成员制:首次带 author 的调用即注册(轮询即可,无需发帖)。
- 身份分两层:展示层(发帖/回复)零仪式;治理层(判定/改 quorum,阶段 3 上线)需 token。
- 心跳语义 =「可参与性信号」(备注 D):LLM 调用与带失败闸门的 watcher 探针都续 last_seen;
  连续 3 次唤醒失败后 watcher 停止心跳,成员自然衰减为暂缓(≥24h 无心跳)。
- 暂缓 = 冻结计票参与(非清除),心跳恢复即自动复权。

## 讨论帖
- review 帖建议传 quorum(阶段 2 起生效)并在 context 写明对象/目的;发言表态后须落判定(阶段 3)。
- 预算默认 20/人/线程(阶段 2 强制);线程总上限 {THREAD_CAP} 条。
- resolved 帖不触发回应义务;非 quorum 发言 = advisory(可说服、不可计票)。
- wontfix 为争议终态,仅人类可翻回。

## 轮询契约(每个成员)
- 轮询 = list_comments_since(author=自己, since="now")——游标决定窗口(恰好=全部未投递);
  显式传更长的 since 可重读该窗口(union 语义:窗口 = since ∪ 未投递积压,永不漏)。
  该调用是 last_read 游标的唯一推进者:游标 =「已投递给 LLM 的水位线」。
  get_thread/探针/其它读取永不推进(C3)。
- needs_attention 为空转时一句话终止,不读全帖;优先级 awaiting > mentions > new。
- @点名是送达信号;被点名或被期待判定时应尽快回应(契约 (b) 成员由用户唤起,沉默不计超时)。
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
def _connect() -> sqlite3.Connection:
    """One connection per call. WAL + busy_timeout absorbs the 3 concurrent
    writers (clients hit the single server process; SQLite serializes writes)."""
    conn = sqlite3.connect(DB_PATH, timeout=10)  # 10s fallback for busy
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        # v2 migrations for pre-v2 DBs (SQLite has no ADD COLUMN IF NOT EXISTS)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
        for col, ddl in (
            ("author", "TEXT"),
            ("quorum", "TEXT"),
            ("per_author_budget", "INTEGER NOT NULL DEFAULT 20"),
            ("revision", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if col not in cols:
                conn.execute(f"ALTER TABLE threads ADD COLUMN {col} {ddl}")


@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


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


# --- v2 A5 (3a): two-tier identity / tokens ---------------------------------
REISSUE_AFTER_HOURS = 24  # lost-token reissue needs no governance activity


def _token_ok(conn: sqlite3.Connection, author: str, token: str | None) -> bool:
    if not token:
        return False
    row = conn.execute("SELECT meta FROM participants WHERE author=?", (author,)).fetchone()
    if row is None:
        return False
    try:
        meta = json.loads(row["meta"] or "{}")
    except (ValueError, TypeError):
        return False
    return meta.get("token_hash") == hashlib.sha256(token.encode()).hexdigest()


def _audit(conn, author: str, action: str, thread_id: int | None = None,
           from_v: str | None = None, to_v: str | None = None,
           revision: int | None = None, costed: int = 0, note: str | None = None) -> None:
    conn.execute(
        "INSERT INTO verdict_events(thread_id, author, action, from_v, to_v,"
        " revision, costed, note) VALUES (?,?,?,?,?,?,?,?)",
        (thread_id, author, action, from_v, to_v, revision, costed, note),
    )


@mcp.tool
def claim_token(author: str) -> str:
    """Issue (or reissue) your governance token — first claim is free.

    The FIRST call for a registered name issues the token and shows the
    plaintext EXACTLY ONCE: persist it (watcher-side file, memory dir —
    anywhere that survives your sessions) before any governance call.
    A lost token can be reissued only after 24h without governance activity
    by that name (verdict_events watermark, audited append-only).
    """
    with db() as conn:
        _touch(conn, author)
        row = conn.execute("SELECT meta FROM participants WHERE author=?", (author,)).fetchone()
        if row is None:
            return "ERROR: unknown author"
        try:
            meta = json.loads(row["meta"] or "{}")
        except (ValueError, TypeError):
            meta = {}
        if not meta.get("token_hash"):
            token = secrets.token_hex(16)
            meta["token_hash"] = hashlib.sha256(token.encode()).hexdigest()
            conn.execute("UPDATE participants SET meta=? WHERE author=?",
                         (json.dumps(meta, ensure_ascii=False), author))
            _audit(conn, author, "token_issue")
            return (f"Token issued for '{author}' (shown ONCE, persist it now):\n"
                    f"  {token}\n"
                    f"Governance calls (set_verdict / bump_revision / set_quorum) require it.")
        # already issued: reissue path (dsh-5) — 24h since that author's last
        # governance event (any verdict_events row by them, claude note ①)
        last = conn.execute(
            "SELECT created_at FROM verdict_events WHERE author=?"
            " AND action IN ('verdict','verdict_free','bump_revision','set_quorum')"
            " ORDER BY id DESC LIMIT 1", (author,),
        ).fetchone()
        if last is None or _hours_since(last["created_at"]) >= REISSUE_AFTER_HOURS:
            token = secrets.token_hex(16)
            meta["token_hash"] = hashlib.sha256(token.encode()).hexdigest()
            conn.execute("UPDATE participants SET meta=? WHERE author=?",
                         (json.dumps(meta, ensure_ascii=False), author))
            _audit(conn, author, "token_reissue",
                   note="reissued after governance inactivity window")
            return (f"Token REISSUED for '{author}' (old one invalidated; shown ONCE):\n"
                    f"  {token}")
        return (
            f"ERROR: token for '{author}' already issued. Lost-token reissue unlocks "
            f"after {REISSUE_AFTER_HOURS}h without governance activity by this name "
            f"(last governance event: {last['created_at']})."
        )


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
            Every name must already be registered (any author-carrying call
            registers — a poll is enough). NULL/omitted = free thread
            (creator closes it manually, v1 behaviour).
        per_author_budget: budget per author per thread (default 20; covers
            posts + replies + costed verdict flips).

    Returns: the new thread id and a nudge to get_protocol for new members.
    """
    if per_author_budget is not None and (per_author_budget < 1 or per_author_budget > 100):
        return "ERROR: per_author_budget must be in [1, 100]"
    with db() as conn:
        _touch(conn, author)  # ORDER: register the creator BEFORE quorum check
        if author:
            if _count_today(conn, "threads", author) >= THREADS_PER_DAY:
                return f"ERROR: daily thread-creation limit reached ({THREADS_PER_DAY}/day)."
        if quorum is not None:
            if not isinstance(quorum, list) or not all(isinstance(q, str) and q.strip() for q in quorum):
                return "ERROR: quorum must be a list of non-empty name strings"
            names = [q.strip() for q in quorum]
            if len(set(names)) != len(names):
                return "ERROR: quorum contains duplicate names"
            missing = [
                q for q in names
                if conn.execute(
                    "SELECT 1 FROM participants WHERE author=?", (q,)
                ).fetchone() is None
            ]
            if missing:
                return (
                    f"ERROR: quorum name(s) not registered: {missing}. "
                    "A name registers on its first author-carrying call — a bare "
                    "poll (list_comments_since with author) is enough; no post needed."
                )
            quorum_json = json.dumps(names, ensure_ascii=False)
        else:
            quorum_json = None
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
    loc = ""
    if file:
        loc = f" at {file}" + (f":{line}" if line else "")
    sev = f" [{severity}]" if severity else ""
    return f"Posted comment #{cid} in thread #{thread_id} by {author}{sev}{loc}."


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
    return f"Posted reply #{cid} to comment #{comment_id} by {author}."


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
                """SELECT t.id, t.title, t.status, t.created_at,
                          (SELECT COUNT(*) FROM comments c WHERE c.thread_id = t.id) AS n
                   FROM threads t ORDER BY t.id DESC"""
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT t.id, t.title, t.status, t.created_at,
                          (SELECT COUNT(*) FROM comments c WHERE c.thread_id = t.id) AS n
                   FROM threads t WHERE t.status=? ORDER BY t.id DESC""",
                (status,),
            ).fetchall()
    if not rows:
        return f"No threads with status '{status}'."
    lines = ["Threads:"]
    for r in rows:
        lines.append(f"  #{r['id']} [{r['status']}] {r['title']}  ({r['n']} comments, {r['created_at']})")
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
        usage_rows = conn.execute(
            "SELECT author, COUNT(*) AS c FROM comments WHERE thread_id=? GROUP BY author",
            (thread_id,),
        ).fetchall()
    out = [
        f"Thread #{t['id']}: {t['title']}  [{t['status']}]",
        f"  created: {t['created_at']}   comments: {len(all_comments)}/{THREAD_CAP}",
    ]
    if t["quorum"]:
        try:
            names = json.loads(t["quorum"])
        except (ValueError, TypeError):
            names = []
        usage = " · ".join(f"{r['author']} {r['c']}/{t['per_author_budget']}" for r in usage_rows)
        out.append(f"  quorum: {names}   revision: {t['revision']}   budget: {usage or '—'}")

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
    gate to ACTIVE quorum members and adds auto-resolve.

    Args:
        status: the new status — 'open' | 'resolved' | 'wontfix'.
        author: who is changing the status (your tool name), recorded for clarity.
        thread_id: set the WHOLE thread to this status.
        comment_id: set just this comment to this status.
        human_override: bypass gates (incl. wontfix→open). Threat-model bound:
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
            conn.execute("UPDATE threads SET status=? WHERE id=?", (status, thread_id))
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
    - First verdict per (author, revision) is FREE; every flip afterwards costs
      1 from your per-author thread budget (comments + flips share it).
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
        costed = 0 if first_for_revision else 1
        if costed and _author_usage(conn, thread_id, author) >= t["per_author_budget"]:
            return (
                f"ERROR: {author} reached the per-author budget "
                f"({t['per_author_budget']}) in thread #{thread_id} — flips are billed. "
                "Conclude via set_status (wontfix escalates to the human)."
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
        return (
            f"Verdict recorded: {author} → {verdict}"
            f"{'' if first_for_revision else ' (billed 1)'} on thread #{thread_id}"
            f" rev {revision}."
        )


@mcp.tool
def bump_revision(thread_id: int, author: str, token: str | None = None) -> str:
    """Creator-only: bump the thread's revision, clearing ALL verdicts (G2).

    Use after revising the artifact under review — stale passes must not
    auto-resolve a new revision. Costs 1 budget. Every quorum member must
    re-vote (their first verdict on the new revision is free).

    Args:
        thread_id: the thread.
        author: must be the thread creator.
        token: your governance token.
    """
    with db() as conn:
        _touch(conn, author)
        t = conn.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if t is None:
            return f"ERROR: thread #{thread_id} does not exist"
        if t["author"] != author:
            return f"ERROR: bump_revision is creator-only (creator: {t['author']!r})"
        if not _token_ok(conn, author, token):
            return "ERROR: missing/invalid governance token — claim_token first"
        if _author_usage(conn, thread_id, author) >= t["per_author_budget"]:
            return f"ERROR: {author} reached the per-author budget in thread #{thread_id}."
        new_rev = (t["revision"] or 1) + 1
        conn.execute("UPDATE threads SET revision=? WHERE id=?", (new_rev, thread_id))
        conn.execute("DELETE FROM verdicts WHERE thread_id=?", (thread_id,))
        _audit(conn, author, "bump_revision", thread_id=thread_id,
               revision=new_rev, costed=1,
               note="all verdicts cleared; re-vote required")
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
) -> str:
    """Creator-only: amend a thread's quorum (open threads only; floor ≥2).

    Adding requires the names to be registered; shrinking below 2 members is
    rejected (trae 边界6: a 1-member quorum is self-judging).

    Args:
        thread_id: the thread (must be open).
        author: must be the thread creator.
        token: your governance token.
        add: registered names to add.
        remove: current quorum names to remove.
    """
    with db() as conn:
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
        for q in (add or []):
            if q in names:
                return f"ERROR: {q!r} already in quorum"
            if conn.execute("SELECT 1 FROM participants WHERE author=?", (q,)).fetchone() is None:
                return f"ERROR: {q!r} not registered (a poll registers a name)"
        for q in (remove or []):
            if q not in names:
                return f"ERROR: {q!r} not in current quorum"
        new_names = [n for n in names if n not in (remove or [])] + list(add or [])
        if len(new_names) < 2:
            return "ERROR: resulting quorum would be < 2 (self-judging floor)"
        conn.execute("UPDATE threads SET quorum=? WHERE id=?",
                     (json.dumps(new_names, ensure_ascii=False), thread_id))
        _audit(conn, author, "set_quorum", thread_id=thread_id,
               note=f"{names} -> {new_names}")
        recompute_thread(conn, thread_id)
        return f"Quorum updated: {names} → {new_names} (thread #{thread_id})."


# --- v2 A5 (3b): the state machine -------------------------------------------
def _active_quorum(conn: sqlite3.Connection, names: list[str]) -> list[str]:
    active = []
    for n in names:
        row = conn.execute("SELECT last_seen FROM participants WHERE author=?", (n,)).fetchone()
        if row and _is_active(row["last_seen"]):
            active.append(n)
    return active


def recompute_thread(conn: sqlite3.Connection, thread_id: int) -> None:
    """The one invariant enforcer. Called at the end of every author-carrying
    write transaction (claude #48-3 universal mechanism) and from the
    heartbeat path. Idempotent by construction (state transitions only fire
    when the invariant is violated/satisfied, checked inside the txn).

    resolved ⟺ ALL *active* quorum verdicts pass AND ≥2 active members.
    - open + (all active pass, ≥2)         → auto-resolve with tally
    - resolved + standing object (incl. one
      restored by an unfreeze, C')          → reopen, reason recorded
    - resolved + zero verdicts (revision
      bump cleared them)                    → reopen (needs re-vote)
    - wontfix                               → terminal, skipped
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
            tally = " · ".join(f"{n} pass" for n in active)
            conn.execute("UPDATE threads SET status='resolved' WHERE id=?", (thread_id,))
            _audit(conn, "system", "auto_resolve", thread_id=thread_id,
                   revision=t["revision"], note=f"active quorum unanimous: {tally}")
    elif t["status"] == "resolved":
        standing = [n for n in names if verdicts.get(n) == "object"]
        if standing:
            reason = (
                "frozen object re-stood after suspension lift (C', unbilled)"
                if any(n not in active for n in standing) or len(active) < len(names)
                else "object recorded"
            )
            conn.execute("UPDATE threads SET status='open' WHERE id=?", (thread_id,))
            _audit(conn, "system", "auto_reopen", thread_id=thread_id,
                   revision=t["revision"],
                   note=f"standing object by {standing} ({reason})")
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
        "SELECT id, quorum FROM threads"
        " WHERE status IN ('open','resolved') AND quorum IS NOT NULL"
    ).fetchall()
    for r in rows:
        if author in json.loads(r["quorum"]):
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
    threads (cost model §4). awaiting_my_verdict is "not_implemented" until
    stage 3b (explicit sentinel per dsh-6, never a bare []).

    Args:
        since: absolute timestamp or relative '30m'/'2h'/'1d'/'now' (first-round
            fallback when no cursor exists yet).
        author: your tool name — poll WITH it, always.
    """
    snapshot = _now_iso()  # watermark captured BEFORE the read (no swallow race)
    parsed_since = _parse_since(since)
    delivered = None
    if author:
        with _connect() as conn:
            row = conn.execute(
                "SELECT last_read FROM participants WHERE author=?", (author,)
            ).fetchone()
            if row and row["last_read"]:
                delivered = row["last_read"]
    cutoff = min(parsed_since, delivered) if delivered else parsed_since
    with db() as conn:
        rows = conn.execute(
            """SELECT c.id, c.thread_id, c.parent_id, c.author, c.body,
                      c.file, c.line, c.severity, c.created_at
               FROM comments c
               WHERE c.created_at > ?
               ORDER BY c.created_at ASC""",
            (cutoff,),
        ).fetchall()
        if author:
            # C1.1 shared heartbeat path (+ C' unfreeze recompute inside this
            # txn), cursor advance last; the fetch above already fixed delivery.
            heartbeat_and_recompute(conn, author)
            conn.execute(
                "UPDATE participants SET last_read=? WHERE author=?", (snapshot, author)
            )
    mentions = []
    if author:
        marker = f"@{author}"
        mentions = [
            {"thread_id": r["thread_id"], "comment_id": r["id"]}
            for r in rows
            if marker in (r["body"] or "")
        ]
    awaiting: list | str
    with _connect() as conn:
        open_threads = conn.execute(
            "SELECT COUNT(*) FROM threads WHERE status='open'"
        ).fetchone()[0]
        if author:
            # D2: open ∧ 非 wontfix ∧ quorum ∧ no verdict on current revision ∧
            # active. The caller just heartbeated above (order condition,
            # claude #52-3) so is_active(author) holds by construction here.
            awaiting = [
                r["id"]
                for r in conn.execute(
                    "SELECT id, quorum FROM threads WHERE status='open' AND quorum IS NOT NULL"
                ).fetchall()
                if author in json.loads(r["quorum"])
                and conn.execute(
                    "SELECT 1 FROM verdicts WHERE thread_id=? AND author=? LIMIT 1",
                    (r["id"], author),
                ).fetchone() is None
            ]
        else:
            awaiting = "not_implemented"
    needs = {
        "new_comments": len(rows),
        "mentions_me": mentions,
        "awaiting_my_verdict": awaiting,
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
        "</style></head><body>",
        "<h1>📋 MCP Review Board</h1>",
        f"<p class='meta'>{len(threads)} thread(s) · read-only · "
        f"<a href='/'>refresh</a></p>",
    ]
    if not threads:
        parts.append("<p>No threads yet. Have an agent call <code>create_thread</code>.</p>")
    for t in threads:
        parts.append(
            f"<h2>#{t['id']} {escape(t['title'])} "
            f"<span class='badge {t['status']}'>{t['status']}</span></h2>"
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

    parts.append("</body></html>")
    return "".join(parts)


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
    # stateless_http: no server-side session state — restarts/upgrades never
    # invalidate live client connections (all state lives in SQLite).
    # mcp.run manages uvicorn + the MCP session manager + custom routes together.
    mcp.run(transport="http", host=HOST, port=PORT, stateless_http=True)


if __name__ == "__main__":
    main()
