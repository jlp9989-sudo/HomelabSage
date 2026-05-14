"""SQLite persistence — pure stdlib, no ORM.

Schema is tiny: one table `updates` keyed by `(source, subject, new_version)`.
That triple is stable across runs, so re-detecting the same update is idempotent.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import (
    Analysis,
    AnalyzedUpdate,
    InterviewQuestion,
    InterviewStatus,
    Severity,
    Update,
    UpdateStatus,
)

_SCHEMA = """
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
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Forward-only migrations for older databases."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(updates)").fetchall()}
    if "notion_page_id" not in cols:
        conn.execute("ALTER TABLE updates ADD COLUMN notion_page_id TEXT")

    # interview_questions may not exist yet on very old DBs (CREATE IF NOT
    # EXISTS in _SCHEMA above already covers that). Once it exists, ensure
    # the suggested_text column was added by the v0.4.2 migration — silent
    # add for installs upgrading from the first interview build.
    iq_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(interview_questions)").fetchall()
    }
    if iq_cols and "suggested_text" not in iq_cols:
        conn.execute("ALTER TABLE interview_questions ADD COLUMN suggested_text TEXT")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # `check_same_thread=False`: the connection is created on the main
        # thread (via `create_app`) but closed and read from APScheduler's
        # worker threads and from FastAPI's shutdown event loop. WAL +
        # autocommit (`isolation_level=None`) already make concurrent reads
        # safe; we serialise writes at the engine level.
        self._conn = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        _migrate(self._conn)

    def close(self) -> None:
        self._conn.close()

    # ─── upsert / read ──────────────────────────────────────────

    def upsert(self, item: AnalyzedUpdate) -> None:
        u = item.update
        a = item.analysis
        # `notion_page_id` is preserved across upserts: if the caller hasn't
        # set it on this AnalyzedUpdate (None), the COALESCE keeps the
        # previously-stored value. The output layer is the only writer.
        self._conn.execute(
            """
            INSERT INTO updates (
                id, source, subject, current_version, new_version,
                release_url, release_notes, context_json,
                severity, summary, analysis_json,
                status, detected_at, analyzed_at, notion_page_id
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                release_url    = excluded.release_url,
                release_notes  = excluded.release_notes,
                context_json   = excluded.context_json,
                severity       = excluded.severity,
                summary        = excluded.summary,
                analysis_json  = excluded.analysis_json,
                status         = excluded.status,
                analyzed_at    = excluded.analyzed_at,
                notion_page_id = COALESCE(excluded.notion_page_id, updates.notion_page_id)
            """,
            (
                item.id, u.source, u.subject, u.current_version, u.new_version,
                u.release_url, u.release_notes, json.dumps(u.context),
                a.severity.value if a else None,
                a.summary if a else None,
                a.model_dump_json() if a else None,
                item.status.value,
                item.detected_at.isoformat(),
                item.analyzed_at.isoformat() if item.analyzed_at else None,
                item.notion_page_id,
            ),
        )

    def set_notion_page_id(self, update_id: str, page_id: str) -> None:
        """Record the Notion page id for an update (idempotent)."""
        self._conn.execute(
            "UPDATE updates SET notion_page_id = ? WHERE id = ?",
            (page_id, update_id),
        )

    def get(self, update_id: str) -> AnalyzedUpdate | None:
        row = self._conn.execute("SELECT * FROM updates WHERE id = ?", (update_id,)).fetchone()
        return _row_to_item(row) if row else None

    def list(
        self,
        status: UpdateStatus | None = None,
        source: str | None = None,
        limit: int = 200,
    ) -> list[AnalyzedUpdate]:
        sql = "SELECT * FROM updates WHERE 1=1"
        args: list[object] = []
        if status is not None:
            sql += " AND status = ?"
            args.append(status.value)
        if source is not None:
            sql += " AND source = ?"
            args.append(source)
        sql += " ORDER BY detected_at DESC LIMIT ?"
        args.append(limit)
        return [_row_to_item(r) for r in self._conn.execute(sql, args).fetchall()]

    def set_status(self, update_id: str, status: UpdateStatus) -> None:
        self._conn.execute(
            "UPDATE updates SET status = ? WHERE id = ?", (status.value, update_id)
        )

    # ─── interview questions ────────────────────────────────────

    def add_interview_question(self, q: InterviewQuestion) -> int:
        """Insert a pending question and return its assigned id.

        If a pending question already exists for the same `(container_name,
        image_digest_short)` pair, return its id instead of creating a
        duplicate. The caller should not have to dedupe.
        """
        existing = self._conn.execute(
            """
            SELECT id FROM interview_questions
            WHERE container_name = ? AND image_digest_short = ? AND status = 'pending'
            ORDER BY id DESC LIMIT 1
            """,
            (q.container_name, q.image_digest_short),
        ).fetchone()
        if existing is not None:
            return int(existing["id"])
        cursor = self._conn.execute(
            """
            INSERT INTO interview_questions
                (container_name, image_digest_short, question_text,
                 suggested_text, answer_text, status, created_at, answered_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                q.container_name,
                q.image_digest_short,
                q.question_text,
                q.suggested_text,
                q.answer_text,
                q.status.value,
                q.created_at.isoformat(),
                q.answered_at.isoformat() if q.answered_at else None,
            ),
        )
        # `lastrowid` is set on the connection for the most recent INSERT.
        return int(cursor.lastrowid or 0)

    def list_interview_questions(
        self,
        status: InterviewStatus | None = InterviewStatus.PENDING,
        limit: int = 200,
    ) -> list[InterviewQuestion]:
        sql = "SELECT * FROM interview_questions WHERE 1=1"
        args: list[object] = []
        if status is not None:
            sql += " AND status = ?"
            args.append(status.value)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [_row_to_question(r) for r in self._conn.execute(sql, args).fetchall()]

    def get_interview_question(self, question_id: int) -> InterviewQuestion | None:
        row = self._conn.execute(
            "SELECT * FROM interview_questions WHERE id = ?", (question_id,)
        ).fetchone()
        return _row_to_question(row) if row else None

    def answer_interview_question(self, question_id: int, answer_text: str) -> None:
        """Mark a question as answered. Idempotent — re-answering overwrites."""
        self._conn.execute(
            """
            UPDATE interview_questions
               SET answer_text = ?,
                   status      = 'answered',
                   answered_at = ?
             WHERE id = ?
            """,
            (answer_text, datetime.utcnow().isoformat(), question_id),
        )

    def dismiss_interview_question(self, question_id: int) -> None:
        self._conn.execute(
            "UPDATE interview_questions SET status = 'dismissed' WHERE id = ?",
            (question_id,),
        )

    def count_interview_questions(self, status: InterviewStatus) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM interview_questions WHERE status = ?",
            (status.value,),
        ).fetchone()
        return int(row["n"]) if row else 0


def _row_to_item(row: sqlite3.Row) -> AnalyzedUpdate:
    update = Update(
        source=row["source"],
        subject=row["subject"],
        current_version=row["current_version"],
        new_version=row["new_version"],
        release_url=row["release_url"],
        release_notes=row["release_notes"],
        context=json.loads(row["context_json"]),
    )
    analysis: Analysis | None = None
    if row["analysis_json"]:
        analysis = Analysis.model_validate_json(row["analysis_json"])
    elif row["severity"]:
        analysis = Analysis(severity=Severity(row["severity"]), summary=row["summary"] or "")
    # `notion_page_id` is only present after the migration; older DBs without
    # the column will raise IndexError on key access. sqlite3.Row does not
    # implement __contains__, so `key in row` would iterate values — must
    # check explicitly against keys().
    cols = row.keys()
    page_id = row["notion_page_id"] if "notion_page_id" in cols else None
    return AnalyzedUpdate(
        update=update,
        analysis=analysis,
        status=UpdateStatus(row["status"]),
        detected_at=datetime.fromisoformat(row["detected_at"]),
        analyzed_at=datetime.fromisoformat(row["analyzed_at"]) if row["analyzed_at"] else None,
        notion_page_id=page_id,
    )


def _row_to_question(row: sqlite3.Row) -> InterviewQuestion:
    answered_at = (
        datetime.fromisoformat(row["answered_at"]) if row["answered_at"] else None
    )
    # `suggested_text` only exists after the v0.4.2 migration; older rows on
    # an upgraded DB return None for it. Guard the key access the same way
    # the updates table does for `notion_page_id`.
    cols = row.keys()
    suggested = row["suggested_text"] if "suggested_text" in cols else None
    return InterviewQuestion(
        id=int(row["id"]),
        container_name=row["container_name"],
        image_digest_short=row["image_digest_short"],
        question_text=row["question_text"],
        suggested_text=suggested,
        answer_text=row["answer_text"],
        status=InterviewStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        answered_at=answered_at,
    )
