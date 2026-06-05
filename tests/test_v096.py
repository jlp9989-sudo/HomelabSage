"""Tests for v0.9.6: star/snooze HTMX buttons + Starred/Snoozed filter views."""

from __future__ import annotations

from fastapi.testclient import TestClient

from homelabsage.config import Config
from homelabsage.db import Database
from homelabsage.models import AnalyzedUpdate, Update
from homelabsage.web import create_app


def _seed(db, *, subject="mealie"):
    item = AnalyzedUpdate(
        update=Update(
            source="docker", subject=subject,
            current_version="1", new_version="2",
        ),
    )
    db.upsert(item)
    return item


def _client(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    return TestClient(create_app(cfg)), Database(cfg.storage.database_path)


# ─── star toggle ──────────────────────────────────────────────────


def test_star_toggle_flips_state(tmp_path):
    client, db = _client(tmp_path)
    item = _seed(db)
    # First click → star on
    r = client.post(f"/updates/{item.id}/star/toggle")
    assert r.status_code == 200
    assert "★" in r.text
    assert db.is_starred(item.id) is True
    # Second click → star off
    r = client.post(f"/updates/{item.id}/star/toggle")
    assert r.status_code == 200
    assert "☆" in r.text
    assert db.is_starred(item.id) is False


def test_star_toggle_returns_button_with_swap_endpoint(tmp_path):
    """Defensive: the HTMX response includes a self-targeting hx-post
    so a second click swaps the same node in-place."""
    client, db = _client(tmp_path)
    item = _seed(db)
    r = client.post(f"/updates/{item.id}/star/toggle")
    assert f"/updates/{item.id}/star/toggle" in r.text
    assert "hx-post=" in r.text
    assert "hx-swap=" in r.text


# ─── snooze quick ─────────────────────────────────────────────────


def test_snooze_quick_sets_future_timestamp(tmp_path):
    client, db = _client(tmp_path)
    item = _seed(db)
    r = client.post(
        f"/updates/{item.id}/snooze/quick",
        data={"days": "7"},
    )
    assert r.status_code == 200
    until = db.get_snooze(item.id)
    assert until is not None
    # The future ISO timestamp should start with 202x or later
    assert until[:2] == "20"
    # Response shows the snooze cell with a clear button
    assert "💤" in r.text
    assert "clear" in r.text


def test_snooze_quick_zero_days_clears(tmp_path):
    client, db = _client(tmp_path)
    item = _seed(db)
    db.set_snooze(item.id, "2199-01-01T00:00:00+00:00")
    r = client.post(
        f"/updates/{item.id}/snooze/quick",
        data={"days": "0"},
    )
    assert r.status_code == 200
    assert db.get_snooze(item.id) is None
    # Cleared cell shows the snooze-select form again
    assert "snooze…" in r.text


def test_snooze_quick_default_days_when_unset(tmp_path):
    """No `days` form field → default 7 (FastAPI Form default)."""
    client, db = _client(tmp_path)
    item = _seed(db)
    r = client.post(f"/updates/{item.id}/snooze/quick", data={})
    assert r.status_code == 200
    assert db.get_snooze(item.id) is not None


# ─── filter views ────────────────────────────────────────────────


def test_index_filter_starred(tmp_path):
    client, db = _client(tmp_path)
    a = _seed(db, subject="alpha")
    _seed(db, subject="beta")
    db.set_starred(a.id, True)
    r = client.get("/?filter=starred")
    assert r.status_code == 200
    assert "alpha" in r.text
    assert "beta" not in r.text
    # The all-state pill on the same view shows full counts
    assert "Starred (1)" in r.text


def test_index_filter_snoozed(tmp_path):
    client, db = _client(tmp_path)
    a = _seed(db, subject="alpha")
    _seed(db, subject="beta")
    db.set_snooze(a.id, "2199-01-01T00:00:00+00:00")
    r = client.get("/?filter=snoozed")
    assert r.status_code == 200
    assert "alpha" in r.text
    assert "beta" not in r.text


def test_index_default_filter_shows_all(tmp_path):
    client, db = _client(tmp_path)
    a = _seed(db, subject="alpha")
    db.set_starred(a.id, True)
    _seed(db, subject="beta")
    r = client.get("/")
    assert r.status_code == 200
    assert "alpha" in r.text
    assert "beta" in r.text


def test_index_unknown_filter_falls_back_to_all(tmp_path):
    """Defensive: an unrecognised `filter` value just shows everything."""
    client, db = _client(tmp_path)
    _seed(db, subject="alpha")
    r = client.get("/?filter=garbage")
    assert r.status_code == 200
    assert "alpha" in r.text


def test_index_starred_pill_count_reflects_db(tmp_path):
    """The pill count tracks total starred in DB, not filtered view."""
    client, db = _client(tmp_path)
    for n in ("a", "b", "c"):
        item = _seed(db, subject=n)
        db.set_starred(item.id, True)
    r = client.get("/")
    assert "Starred (3)" in r.text


def test_index_renders_star_button_for_each_row(tmp_path):
    client, db = _client(tmp_path)
    _seed(db, subject="x")
    r = client.get("/")
    assert "/star/toggle" in r.text
    assert "☆" in r.text  # unstarred icon
