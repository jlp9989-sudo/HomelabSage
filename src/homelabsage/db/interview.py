"""Interview-question CRUD — Rule 7 curator fallbacks persisted for user follow-up."""

from __future__ import annotations

import sqlite3

from .._time import parse_iso, utcnow
from ..models import InterviewQuestion, InterviewStatus


def row_to_question(row: sqlite3.Row) -> InterviewQuestion:
    """Reconstruct an `InterviewQuestion` from an `interview_questions` row.

    `suggested_text` only exists after the v0.4.2 migration; older rows on
    an upgraded DB return None for it. Guard the key access the same way
    the updates table does for `notion_page_id`.
    """
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
        created_at=parse_iso(row["created_at"]),
        answered_at=parse_iso(row["answered_at"]),
    )


class InterviewMixin:
    """Methods touching the `interview_questions` table."""

    _conn: sqlite3.Connection

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
        return [
            row_to_question(r) for r in self._conn.execute(sql, args).fetchall()
        ]

    def get_interview_question(self, question_id: int) -> InterviewQuestion | None:
        row = self._conn.execute(
            "SELECT * FROM interview_questions WHERE id = ?", (question_id,)
        ).fetchone()
        return row_to_question(row) if row else None

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
            (answer_text, utcnow().isoformat(), question_id),
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

    def dismiss_stale_interview_questions(self, *, older_than_days: int) -> int:
        """Auto-dismiss PENDING rows older than `older_than_days` days.

        Returns the number of rows touched. Idempotent: re-runs against
        the same window mark zero new rows. `answered_at` stays NULL —
        auto-dismissal is not an answer, and conflating the two pollutes
        the field's semantics.

        Negative `older_than_days` is treated as a no-op (a 0 return)
        because the alternative would silently dismiss future-dated
        rows that crept in via clock skew.
        """
        if older_than_days < 0:
            return 0
        from datetime import timedelta
        cutoff = (utcnow() - timedelta(days=older_than_days)).isoformat()
        cur = self._conn.execute(
            """
            UPDATE interview_questions
               SET status = 'dismissed'
             WHERE status = 'pending' AND created_at < ?
            """,
            (cutoff,),
        )
        return int(cur.rowcount or 0)
