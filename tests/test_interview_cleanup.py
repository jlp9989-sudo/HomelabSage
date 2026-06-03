"""Tests for stale-interview-question cleanup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from homelabsage.db import Database
from homelabsage.models import InterviewQuestion, InterviewStatus


def _seed(db: Database, *, days_old_list: list[int]) -> list[int]:
    """Insert one PENDING question per `days_old`. Returns the ids."""
    ids: list[int] = []
    now = datetime.now(UTC)
    for i, days in enumerate(days_old_list):
        q = InterviewQuestion(
            container_name=f"c{i}",
            image_digest_short=f"d{i}",
            question_text="why?",
            created_at=now - timedelta(days=days),
        )
        new_id = db.add_interview_question(q)
        # `add_interview_question` always uses utcnow() for created_at;
        # we need to BACK-DATE the row to simulate an old question.
        db._conn.execute(
            "UPDATE interview_questions SET created_at = ? WHERE id = ?",
            ((now - timedelta(days=days)).isoformat(), new_id),
        )
        ids.append(new_id)
    return ids


def test_dismiss_stale_dismisses_old_only(tmp_path: Path):
    """5d row kept; 31d/60d rows dismissed. The 30d-exact row is borderline
    (microsecond drift between seed and SQL utcnow() decides it) — we test
    the clearly-stale and clearly-fresh cases only."""
    db = Database(tmp_path / "s.sqlite")
    try:
        ids = _seed(db, days_old_list=[5, 31, 60])
        n = db.dismiss_stale_interview_questions(older_than_days=30)
        assert n == 2
        rows = [db.get_interview_question(i) for i in ids]
        assert rows[0].status == InterviewStatus.PENDING       # 5 days
        assert rows[1].status == InterviewStatus.DISMISSED     # 31 days
        assert rows[2].status == InterviewStatus.DISMISSED     # 60 days
    finally:
        db.close()


def test_dismiss_stale_is_idempotent(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        _seed(db, days_old_list=[40])
        n1 = db.dismiss_stale_interview_questions(older_than_days=30)
        n2 = db.dismiss_stale_interview_questions(older_than_days=30)
        assert n1 == 1
        assert n2 == 0
    finally:
        db.close()


def test_dismiss_stale_only_touches_pending(tmp_path: Path):
    """Already-answered or already-dismissed rows must not be touched."""
    db = Database(tmp_path / "s.sqlite")
    try:
        ids = _seed(db, days_old_list=[40, 40])
        # Answer the first one
        db.answer_interview_question(ids[0], "yes")
        n = db.dismiss_stale_interview_questions(older_than_days=30)
        assert n == 1  # only the un-answered one
        assert db.get_interview_question(ids[0]).status == InterviewStatus.ANSWERED
        assert db.get_interview_question(ids[1]).status == InterviewStatus.DISMISSED
    finally:
        db.close()


def test_dismiss_stale_zero_when_no_pending(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        n = db.dismiss_stale_interview_questions(older_than_days=30)
        assert n == 0
    finally:
        db.close()
