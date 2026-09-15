-- MCP Review Board schema
-- Executed automatically on first start (idempotent via CREATE TABLE IF NOT EXISTS).

-- A review topic, e.g. "三方 review: auth 模块"
CREATE TABLE IF NOT EXISTS threads (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL,
    context    TEXT,                          -- optional: code snippet / PR desc / link under review
    status     TEXT    NOT NULL DEFAULT 'open', -- open | resolved | wontfix
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Comments in a thread. parent_id self-reference forms the reply tree
-- (NULL = top-level comment, non-NULL = reply to that comment id).
CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id  INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    parent_id  INTEGER REFERENCES comments(id) ON DELETE CASCADE, -- NULL = top-level
    author     TEXT    NOT NULL,              -- 'zcode' | 'claude' | 'trae' (free-form string)
    body       TEXT    NOT NULL,
    file       TEXT,                          -- optional: file path being reviewed
    line       INTEGER,                       -- optional: line number
    severity   TEXT,                          -- optional: info | minor | major | blocker
    status     TEXT    NOT NULL DEFAULT 'open', -- open | resolved | wontfix
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_comments_thread ON comments(thread_id);
CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_id);
CREATE INDEX IF NOT EXISTS idx_comments_created ON comments(created_at);

-- v2 A1: participant registry + heartbeat.
-- last_seen  : any author-carrying call (or the /attention probe) upserts this —
--              the "can participate" liveness signal (D1, see DESIGN-V2 备注 D).
-- last_read  : delivery watermark. ONLY list_comments_since advances it (the one
--              call that actually delivers the comment set to an LLM). The probe
--              and every other read must NEVER advance it (C3: level-triggered
--              signal, no swallowed mentions on wake-failure).
-- meta       : JSON (token hash lands here in stage 3a; last_wake_error per C1.2).
CREATE TABLE IF NOT EXISTS participants (
    author     TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    last_read  TEXT,
    meta       TEXT NOT NULL DEFAULT '{}'
);
