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
