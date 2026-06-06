"""Tests for v0.10.7: punch-list Nits N3 (pending TTL) + N4 (regex anchor) + N5 (LIMIT)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homelabsage.curator.helpers import is_purpose_fallback
from homelabsage.db import Database


def _now_iso():
    return datetime.now(UTC).isoformat()


# ─── N5: list_pending_dispatches has a LIMIT ──────────────────────


def test_list_pending_dispatches_caps_default_at_1000(tmp_path):
    db = Database(tmp_path / "t.db")
    # Insert 1200 distinct rows. The PRIMARY KEY (update_id, output_id)
    # means each pair is one row.
    for i in range(1200):
        db.queue_pending_dispatch(f"u{i}", "telegram")
    rows = db.list_pending_dispatches()
    assert len(rows) == 1000


def test_list_pending_dispatches_respects_explicit_limit(tmp_path):
    db = Database(tmp_path / "t.db")
    for i in range(50):
        db.queue_pending_dispatch(f"u{i}", "telegram")
    rows = db.list_pending_dispatches(limit=10)
    assert len(rows) == 10


def test_list_pending_dispatches_returns_oldest_first(tmp_path):
    db = Database(tmp_path / "t.db")
    # Insert with controlled timestamps to verify the sort.
    db._conn.execute(
        "INSERT INTO pending_dispatches (update_id, output_id, queued_at) VALUES (?, ?, ?)",
        ("u_new", "x", "2026-06-05T12:00:00+00:00"),
    )
    db._conn.execute(
        "INSERT INTO pending_dispatches (update_id, output_id, queued_at) VALUES (?, ?, ?)",
        ("u_old", "x", "2026-06-01T08:00:00+00:00"),
    )
    rows = db.list_pending_dispatches()
    assert rows[0]["update_id"] == "u_old"
    assert rows[1]["update_id"] == "u_new"


# ─── N3: purge_pending_dispatches_older_than ──────────────────────


def test_purge_old_pending_drops_aged_rows(tmp_path):
    db = Database(tmp_path / "t.db")
    old = (datetime.now(UTC) - timedelta(days=15)).isoformat()
    recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    db._conn.execute(
        "INSERT INTO pending_dispatches (update_id, output_id, queued_at) VALUES (?, ?, ?)",
        ("u_old", "telegram", old),
    )
    db._conn.execute(
        "INSERT INTO pending_dispatches (update_id, output_id, queued_at) VALUES (?, ?, ?)",
        ("u_recent", "telegram", recent),
    )
    purged = db.purge_pending_dispatches_older_than(days=7)
    assert purged == 1
    remaining = {r["update_id"] for r in db.list_pending_dispatches()}
    assert remaining == {"u_recent"}


def test_purge_old_pending_zero_days_is_noop(tmp_path):
    db = Database(tmp_path / "t.db")
    db.queue_pending_dispatch("u1", "x")
    assert db.purge_pending_dispatches_older_than(days=0) == 0
    assert db.purge_pending_dispatches_older_than(days=-3) == 0
    assert len(db.list_pending_dispatches()) == 1


def test_purge_old_pending_returns_zero_on_empty_table(tmp_path):
    db = Database(tmp_path / "t.db")
    assert db.purge_pending_dispatches_older_than(days=30) == 0


# ─── N4: _FALLBACK_RE now anchored via fullmatch ──────────────────


def test_fallback_regex_matches_clean_fallback_line():
    assert is_purpose_fallback("(no purpose stated yet — fill in)") is True
    assert is_purpose_fallback("no purpose stated yet — fill in") is True


def test_fallback_regex_matches_markdown_decorated_line():
    assert is_purpose_fallback("> (no purpose stated yet — fill in)") is True
    assert is_purpose_fallback("# no purpose stated yet — fill in") is True


def test_fallback_regex_rejects_mention_in_coherent_sentence():
    """Before N4: a note that just mentioned the bailout phrase as part
    of a longer sentence was incorrectly classified as fallback."""
    coherent = (
        "Sonarr container holds the TV library scanner — fixed the "
        "'no purpose stated yet — fill in' confusion last week."
    )
    assert is_purpose_fallback(coherent) is False


def test_fallback_regex_rejects_real_note():
    assert is_purpose_fallback("Self-hosted RSS reader for Javier.") is False


def test_fallback_regex_handles_empty():
    assert is_purpose_fallback("") is False
    assert is_purpose_fallback("\n\n   \n") is False
