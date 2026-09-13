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

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse

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


@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- MCP server ------------------------------------------------------------
mcp = FastMCP("ReviewBoard")


@mcp.tool
def create_thread(title: str, context: str | None = None) -> str:
    """Start a new review topic.

    Args:
        title: short name of the review topic (e.g. "三方 review: auth 模块").
        context: optional background — a code snippet, PR description, or link
            that all reviewers should read first.

    Returns: the new thread id (an integer), which the other agents use in
        post_comment / get_thread / set_status.
    """
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO threads(title, context) VALUES (?, ?)",
            (title, context),
        )
        tid = cur.lastrowid
    return f"Created thread #{tid}: {title}\nShare this id with the other reviewers."


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

    by_parent: dict[int | None, list[sqlite3.Row]] = {}
    for c in all_comments:
        by_parent.setdefault(c["parent_id"], []).append(c)

    out = [
        f"Thread #{t['id']}: {t['title']}  [{t['status']}]",
        f"  created: {t['created_at']}   comments: {len(all_comments)}/{THREAD_CAP}",
    ]
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
) -> str:
    """Mark a thread or a single comment as resolved or wontfix (set back to open).

    Pass exactly one of thread_id / comment_id.

    Args:
        status: the new status — 'open' | 'resolved' | 'wontfix'.
        author: who is changing the status (your tool name), recorded for clarity.
        thread_id: set the WHOLE thread to this status.
        comment_id: set just this comment to this status.

    Returns: a short confirmation.
    """
    if status not in VALID_STATUSES:
        return f"ERROR: status must be one of {VALID_STATUSES}, got {status!r}"
    if (thread_id is None) == (comment_id is None):
        return "ERROR: pass exactly one of thread_id or comment_id"
    with db() as conn:
        if thread_id is not None:
            cur = conn.execute("UPDATE threads SET status=? WHERE id=?", (status, thread_id))
            if cur.rowcount == 0:
                return f"ERROR: thread #{thread_id} does not exist"
            return f"Thread #{thread_id} set to '{status}' by {author}."
        else:
            cur = conn.execute("UPDATE comments SET status=? WHERE id=?", (status, comment_id))
            if cur.rowcount == 0:
                return f"ERROR: comment #{comment_id} does not exist"
            return f"Comment #{comment_id} set to '{status}' by {author}."


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
def list_comments_since(since: str = "1h") -> str:
    """List comments posted after a given time — lets an agent poll for "what
    did the OTHER agents say since I last looked".

    Args:
        since: either an absolute timestamp matching the server's format
            (e.g. '2026-08-07 21:30:00'), or a relative spec: '30m', '2h',
            '1d', or 'now'. Defaults to '1h' (last hour).

    Returns: one line per comment with id, thread, author, file:line, and body.
    """
    cutoff = _parse_since(since)
    with db() as conn:
        rows = conn.execute(
            """SELECT c.id, c.thread_id, c.parent_id, c.author, c.body,
                      c.file, c.line, c.severity, c.created_at
               FROM comments c
               WHERE c.created_at > ?
               ORDER BY c.created_at ASC""",
            (cutoff,),
        ).fetchall()
    if not rows:
        return f"No comments since {cutoff}."
    lines = [f"Comments since {cutoff} ({len(rows)}):"]
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
    # mcp.run manages uvicorn + the MCP session manager + custom routes together.
    mcp.run(transport="http", host=HOST, port=PORT)


if __name__ == "__main__":
    main()
