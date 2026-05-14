"""SQLite layer must be idempotent and round-trip every field.

The Notion duplicates incident (10-may-2026) was caused by a missing
notion_page_id column: the output kept POSTing because the engine had no
way to remember "we already created the page". These tests pin the
contract so it doesn't regress.
"""

import sqlite3

from homelabsage.db import Database
from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    InterviewQuestion,
    InterviewStatus,
    Severity,
    Update,
    UpdateStatus,
)


def _make_update(**overrides):
    base = {
        "source": "docker",
        "subject": "mealie",
        "current_version": "3.16.0",
        "new_version": "3.17.0",
    }
    base.update(overrides)
    return Update(**base)


def test_upsert_and_get_round_trips_all_fields(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    item = AnalyzedUpdate(
        update=_make_update(release_notes="changelog body"),
        analysis=Analysis(severity=Severity.HIGH, summary="security fixes"),
        status=UpdateStatus.ANALYZED,
    )
    db.upsert(item)

    back = db.get(item.id)
    assert back is not None
    assert back.update.subject == "mealie"
    assert back.analysis is not None
    assert back.analysis.severity is Severity.HIGH
    assert back.analysis.summary == "security fixes"
    assert back.status is UpdateStatus.ANALYZED


def test_notion_page_id_round_trip(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    item = AnalyzedUpdate(update=_make_update(), notion_page_id="abc-123")
    db.upsert(item)

    back = db.get(item.id)
    assert back is not None
    assert back.notion_page_id == "abc-123"


def test_set_notion_page_id_persists(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    item = AnalyzedUpdate(update=_make_update())
    db.upsert(item)
    assert db.get(item.id).notion_page_id is None

    db.set_notion_page_id(item.id, "page-xyz")
    assert db.get(item.id).notion_page_id == "page-xyz"


def test_upsert_preserves_notion_page_id_on_reupsert(tmp_path):
    """The engine re-runs upsert on each scan. If the caller didn't set
    notion_page_id (e.g. analyzed object built fresh from plugin output),
    we must NOT wipe the value already stored by the output layer.
    """
    db = Database(tmp_path / "state.sqlite")
    item = AnalyzedUpdate(update=_make_update())
    db.upsert(item)
    db.set_notion_page_id(item.id, "page-xyz")

    # Re-emit the same id with a fresh AnalyzedUpdate (page_id=None)
    fresh = AnalyzedUpdate(
        update=_make_update(),
        analysis=Analysis(severity=Severity.INFO, summary="re-analyzed"),
        status=UpdateStatus.ANALYZED,
    )
    db.upsert(fresh)

    back = db.get(item.id)
    assert back.notion_page_id == "page-xyz"  # not None'd out
    assert back.analysis.summary == "re-analyzed"  # new analysis applied


def test_migration_adds_notion_page_id_to_old_db(tmp_path):
    """Older deployments have a state.sqlite without the column. Opening
    the DB must add it transparently — without dropping data.
    """
    path = tmp_path / "state.sqlite"
    # Simulate an old schema by creating the DB without notion_page_id
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE updates (
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
            analyzed_at     TEXT
        );
        """
    )
    raw.execute(
        "INSERT INTO updates(id,source,subject,current_version,new_version,status,detected_at) "
        "VALUES('docker:x:1','docker','x','0','1','new','2026-05-10T00:00:00')"
    )
    raw.commit()
    raw.close()

    # Opening via Database() should migrate
    db = Database(path)
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(updates)").fetchall()}
    assert "notion_page_id" in cols
    back = db.get("docker:x:1")
    assert back is not None
    assert back.notion_page_id is None  # column exists but unset, as expected


def test_list_filters_by_status(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    db.upsert(AnalyzedUpdate(update=_make_update(subject="a"), status=UpdateStatus.NEW))
    db.upsert(AnalyzedUpdate(update=_make_update(subject="b"), status=UpdateStatus.ANALYZED))
    db.upsert(AnalyzedUpdate(update=_make_update(subject="c"), status=UpdateStatus.DISMISSED))

    assert {i.update.subject for i in db.list(status=UpdateStatus.ANALYZED)} == {"b"}
    assert len(db.list()) == 3


# ─── interview_questions ────────────────────────────────────────


def _make_question(**overrides) -> InterviewQuestion:
    base = {
        "container_name": "openclaw",
        "image_digest_short": "abc123def456",
        "question_text": "What is the purpose of openclaw in your homelab?",
    }
    base.update(overrides)
    return InterviewQuestion(**base)


def test_add_interview_question_round_trip(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    qid = db.add_interview_question(_make_question())
    assert qid > 0

    back = db.get_interview_question(qid)
    assert back is not None
    assert back.container_name == "openclaw"
    assert back.status is InterviewStatus.PENDING
    assert back.answer_text is None
    assert back.answered_at is None


def test_add_interview_question_dedupes_pending_for_same_container_image(tmp_path):
    """If the same `(container, image_digest)` already has a PENDING question,
    re-adding returns the existing id — no duplicate row."""
    db = Database(tmp_path / "state.sqlite")
    q = _make_question()
    first = db.add_interview_question(q)
    second = db.add_interview_question(q)

    assert first == second
    assert len(db.list_interview_questions()) == 1


def test_add_creates_new_row_when_image_digest_changes(tmp_path):
    """A rebuilt image is a different question — the user might have new context."""
    db = Database(tmp_path / "state.sqlite")
    first = db.add_interview_question(_make_question(image_digest_short="aaa"))
    second = db.add_interview_question(_make_question(image_digest_short="bbb"))
    assert first != second
    assert len(db.list_interview_questions()) == 2


def test_add_creates_new_row_when_previous_was_answered(tmp_path):
    """An answered question doesn't block a fresh one for the same container —
    user might want to re-answer for a new image build."""
    db = Database(tmp_path / "state.sqlite")
    first = db.add_interview_question(_make_question())
    db.answer_interview_question(first, "It's a Telegram bot.")
    second = db.add_interview_question(_make_question())
    assert first != second
    assert db.count_interview_questions(InterviewStatus.PENDING) == 1
    assert db.count_interview_questions(InterviewStatus.ANSWERED) == 1


def test_answer_interview_question_marks_answered(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    qid = db.add_interview_question(_make_question())
    db.answer_interview_question(qid, "It's the Hermes Telegram bot.")

    back = db.get_interview_question(qid)
    assert back is not None
    assert back.status is InterviewStatus.ANSWERED
    assert back.answer_text == "It's the Hermes Telegram bot."
    assert back.answered_at is not None


def test_dismiss_interview_question(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    qid = db.add_interview_question(_make_question())
    db.dismiss_interview_question(qid)

    back = db.get_interview_question(qid)
    assert back.status is InterviewStatus.DISMISSED
    assert db.count_interview_questions(InterviewStatus.PENDING) == 0


def test_list_interview_filters_by_status_default_pending(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    pending_id = db.add_interview_question(_make_question(container_name="a"))
    answered_id = db.add_interview_question(_make_question(container_name="b"))
    db.answer_interview_question(answered_id, "answer")
    db.add_interview_question(_make_question(container_name="c"))

    pending = db.list_interview_questions()  # default status=PENDING
    assert {q.container_name for q in pending} == {"a", "c"}
    assert all(q.status is InterviewStatus.PENDING for q in pending)
    assert pending_id in {q.id for q in pending}


def test_list_interview_status_none_returns_all(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    db.add_interview_question(_make_question(container_name="a"))
    second = db.add_interview_question(_make_question(container_name="b"))
    db.dismiss_interview_question(second)

    everything = db.list_interview_questions(status=None)
    assert len(everything) == 2


def test_count_interview_questions_per_status(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    a = db.add_interview_question(_make_question(container_name="a"))
    b = db.add_interview_question(_make_question(container_name="b"))
    c = db.add_interview_question(_make_question(container_name="c"))
    db.answer_interview_question(b, "answer")
    db.dismiss_interview_question(c)
    _ = a

    assert db.count_interview_questions(InterviewStatus.PENDING) == 1
    assert db.count_interview_questions(InterviewStatus.ANSWERED) == 1
    assert db.count_interview_questions(InterviewStatus.DISMISSED) == 1


def test_interview_table_idempotent_open(tmp_path):
    """Opening Database twice on the same path must not error nor lose data."""
    path = tmp_path / "state.sqlite"
    db1 = Database(path)
    qid = db1.add_interview_question(_make_question())
    db1.close()

    db2 = Database(path)
    back = db2.get_interview_question(qid)
    assert back is not None
    assert back.container_name == "openclaw"
