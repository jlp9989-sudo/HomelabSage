"""SQLite schema + forward-only migrations.

Kept in its own module so the per-domain operation modules can import the
DDL string without circular dependencies on `Database`. Schema strings live
here as a single block to make diffs across releases readable.
"""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS updates (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    subject         TEXT NOT NULL,
    current_version TEXT NOT NULL,
    new_version     TEXT NOT NULL,
    release_url     TEXT,
    release_notes   TEXT,
    context_json    TEXT NOT NULL DEFAULT '{}',
    severity        TEXT,
    summary         TEXT,
    analysis_json   TEXT,
    status          TEXT NOT NULL,
    detected_at     TEXT NOT NULL,
    analyzed_at     TEXT,
    notion_page_id  TEXT
);

CREATE INDEX IF NOT EXISTS idx_updates_status   ON updates(status);
CREATE INDEX IF NOT EXISTS idx_updates_source   ON updates(source);
CREATE INDEX IF NOT EXISTS idx_updates_severity ON updates(severity);

CREATE TABLE IF NOT EXISTS interview_questions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    container_name     TEXT NOT NULL,
    image_digest_short TEXT NOT NULL,
    question_text      TEXT NOT NULL,
    suggested_text     TEXT,
    answer_text        TEXT,
    status             TEXT NOT NULL DEFAULT 'pending',
    created_at         TEXT NOT NULL,
    answered_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_interview_status    ON interview_questions(status);
CREATE INDEX IF NOT EXISTS idx_interview_container ON interview_questions(container_name);

CREATE TABLE IF NOT EXISTS watched_repos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repo            TEXT NOT NULL UNIQUE,
    nickname        TEXT,
    current_version TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    added_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_watched_active ON watched_repos(active);

-- Pending push dispatches: each row marks "this (update, push_output) pair
-- still needs to fire". Populated when the parity gate skips a real-time
-- push; flushed by the engine on the next scan that finds the gate clear.
CREATE TABLE IF NOT EXISTS pending_dispatches (
    update_id  TEXT NOT NULL,
    output_id  TEXT NOT NULL,
    queued_at  TEXT NOT NULL,
    PRIMARY KEY (update_id, output_id)
);

CREATE INDEX IF NOT EXISTS idx_pending_output ON pending_dispatches(output_id);

-- Auditable AI: one row per successful LLM analyze() call. Stores the
-- exact prompt sent and the raw response received so the user can
-- inspect WHY a verdict landed where it did. Off when the LLM is
-- disabled; never written for failed calls (we don't store empty/error
-- responses — they'd just be noise).
CREATE TABLE IF NOT EXISTS analysis_explainers (
    update_id      TEXT PRIMARY KEY,
    prompt         TEXT NOT NULL,
    raw_response   TEXT NOT NULL,
    notes_used     TEXT,
    provider       TEXT,
    model          TEXT,
    created_at     TEXT NOT NULL
);

-- Per-LLM-call usage stats. One row per successful or failed call so
-- the user can answer "where did my Groq free-tier quota go?" / "is
-- the Gemini paid tier worth it?". Token counts come straight from the
-- provider response when available; otherwise we estimate via a
-- 4-chars-per-token rule of thumb.
CREATE TABLE IF NOT EXISTS llm_usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    update_id     TEXT,
    tokens_in     INTEGER NOT NULL DEFAULT 0,
    tokens_out    INTEGER NOT NULL DEFAULT 0,
    estimated     INTEGER NOT NULL DEFAULT 0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    succeeded     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_provider ON llm_usage(provider);
CREATE INDEX IF NOT EXISTS idx_usage_created  ON llm_usage(created_at);
"""


def migrate(conn: sqlite3.Connection) -> None:
    """Forward-only migrations for older databases.

    `CREATE IF NOT EXISTS` handles the never-seen-before case; this function
    bolts on columns that arrived later. Each branch is a single ALTER and
    runs at most once because the new column is verified against
    `PRAGMA table_info`.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(updates)").fetchall()}
    if "notion_page_id" not in cols:
        conn.execute("ALTER TABLE updates ADD COLUMN notion_page_id TEXT")

    iq_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(interview_questions)").fetchall()
    }
    if iq_cols and "suggested_text" not in iq_cols:
        conn.execute("ALTER TABLE interview_questions ADD COLUMN suggested_text TEXT")
