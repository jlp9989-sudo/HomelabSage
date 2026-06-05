"""Tests for v0.9.9: a11y + custom snooze datepicker + inline explain link."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient

from homelabsage.config import Config
from homelabsage.db import Database
from homelabsage.models import AnalyzedUpdate, Update
from homelabsage.web import create_app


def _client(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    return TestClient(create_app(cfg)), Database(cfg.storage.database_path)


def _seed(db, *, subject="mealie"):
    item = AnalyzedUpdate(
        update=Update(
            source="docker", subject=subject,
            current_version="1", new_version="2",
        ),
    )
    db.upsert(item)
    return item


# ─── star button a11y ─────────────────────────────────────────────


def test_star_button_renders_with_aria_label(tmp_path):
    client, db = _client(tmp_path)
    _seed(db, subject="alpha")
    r = client.get("/")
    assert r.status_code == 200
    assert 'aria-label="Star alpha"' in r.text
    assert 'aria-pressed="false"' in r.text


def test_star_toggle_response_has_aria_label(tmp_path):
    client, db = _client(tmp_path)
    item = _seed(db, subject="beta")
    r = client.post(f"/updates/{item.id}/star/toggle")
    assert r.status_code == 200
    # After toggle on, the response button should carry aria-label "Unstar"
    # and aria-pressed="true"
    assert 'aria-label="Unstar"' in r.text
    assert 'aria-pressed="true"' in r.text


# ─── custom snooze datepicker ─────────────────────────────────────


def test_snooze_until_sets_future_date(tmp_path):
    client, db = _client(tmp_path)
    item = _seed(db)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    r = client.post(
        f"/updates/{item.id}/snooze/until",
        data={"until": tomorrow},
    )
    assert r.status_code == 200
    until = db.get_snooze(item.id)
    assert until is not None
    assert until.startswith(tomorrow)
    assert "💤" in r.text


def test_snooze_until_blank_clears(tmp_path):
    client, db = _client(tmp_path)
    item = _seed(db)
    db.set_snooze(item.id, "2199-01-01T00:00:00+00:00")
    r = client.post(f"/updates/{item.id}/snooze/until", data={"until": ""})
    assert r.status_code == 200
    assert db.get_snooze(item.id) is None


def test_snooze_until_past_date_clears(tmp_path):
    """Picking today/yesterday is defensive: same as clear."""
    client, db = _client(tmp_path)
    item = _seed(db)
    db.set_snooze(item.id, "2199-01-01T00:00:00+00:00")
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    r = client.post(f"/updates/{item.id}/snooze/until", data={"until": yesterday})
    assert r.status_code == 200
    assert db.get_snooze(item.id) is None


def test_snooze_until_invalid_400(tmp_path):
    client, db = _client(tmp_path)
    item = _seed(db)
    r = client.post(f"/updates/{item.id}/snooze/until", data={"until": "not-a-date"})
    assert r.status_code == 400


def test_snooze_until_404_on_bogus_id(tmp_path):
    client, _db = _client(tmp_path)
    r = client.post("/updates/does-not-exist/snooze/until", data={"until": "2099-01-01"})
    assert r.status_code == 404


def test_index_renders_custom_date_input(tmp_path):
    client, db = _client(tmp_path)
    _seed(db, subject="gamma")
    r = client.get("/")
    assert r.status_code == 200
    assert 'type="date"' in r.text
    assert "/snooze/until" in r.text


# ─── inline explain link ──────────────────────────────────────────


def test_explain_link_renders_when_explainer_present(tmp_path):
    client, db = _client(tmp_path)
    item = _seed(db, subject="delta")
    db.upsert_explainer(
        item.id,
        prompt="why?",
        raw_response="because",
        notes_used=None,
        provider="local",
        model="qwen3.6",
    )
    r = client.get("/")
    assert r.status_code == 200
    assert f"/updates/{item.id}/explain" in r.text
    assert ">explain<" in r.text


def test_explain_link_absent_without_explainer(tmp_path):
    client, db = _client(tmp_path)
    _seed(db, subject="epsilon")
    r = client.get("/")
    assert r.status_code == 200
    # No explainer row → no inline link
    assert ">explain<" not in r.text


def test_list_explained_ids_returns_set(tmp_path):
    _, db = _client(tmp_path)
    item = _seed(db, subject="zeta")
    assert db.list_explained_ids() == set()
    db.upsert_explainer(
        item.id, prompt="", raw_response="",
        notes_used=None, provider=None, model=None,
    )
    assert db.list_explained_ids() == {item.id}
