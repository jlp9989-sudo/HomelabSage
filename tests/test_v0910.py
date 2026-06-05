"""Tests for v0.9.10: /search HTML wrapper + CSV download + inline notes."""

from __future__ import annotations

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


# ─── search ───────────────────────────────────────────────────────


def test_search_page_renders_box_when_empty(tmp_path):
    client, _db = _client(tmp_path)
    r = client.get("/search")
    assert r.status_code == 200
    assert 'name="q"' in r.text


def test_search_page_filters_by_query(tmp_path):
    client, db = _client(tmp_path)
    _seed(db, subject="alpha-app")
    _seed(db, subject="beta-app")
    r = client.get("/search?q=alpha")
    assert r.status_code == 200
    assert "alpha-app" in r.text
    assert "beta-app" not in r.text


def test_search_page_preserves_query_in_box(tmp_path):
    client, _db = _client(tmp_path)
    r = client.get("/search?q=zoot")
    assert r.status_code == 200
    assert 'value="zoot"' in r.text


def test_index_has_search_box_in_header(tmp_path):
    client, _db = _client(tmp_path)
    r = client.get("/")
    assert r.status_code == 200
    assert 'action="/search"' in r.text
    assert 'name="q"' in r.text


# ─── CSV download ─────────────────────────────────────────────────


def test_history_csv_downloads(tmp_path):
    client, db = _client(tmp_path)
    _seed(db, subject="cee-ess-vee")
    r = client.get("/api/updates/history.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert "cee-ess-vee" in r.text


def test_history_csv_link_in_index(tmp_path):
    client, _db = _client(tmp_path)
    r = client.get("/")
    assert r.status_code == 200
    assert "/api/updates/history.csv" in r.text


# ─── inline note ──────────────────────────────────────────────────


def test_note_edit_swaps_to_textarea(tmp_path):
    client, db = _client(tmp_path)
    item = _seed(db)
    r = client.get(f"/updates/{item.id}/note/edit")
    assert r.status_code == 200
    assert "<textarea" in r.text
    assert f'/updates/{item.id}/note' in r.text


def test_note_post_saves_and_returns_read_cell(tmp_path):
    client, db = _client(tmp_path)
    item = _seed(db)
    r = client.post(f"/updates/{item.id}/note", data={"note": "watch for breaking changes"})
    assert r.status_code == 200
    assert "watch for breaking changes" in r.text
    assert ">edit<" in r.text  # back to read view with edit link
    assert db.get_user_note(item.id) == "watch for breaking changes"


def test_note_post_clears_with_empty(tmp_path):
    client, db = _client(tmp_path)
    item = _seed(db)
    db.set_user_note(item.id, "old")
    r = client.post(f"/updates/{item.id}/note", data={"note": ""})
    assert r.status_code == 200
    assert ">add note<" in r.text
    # set_user_note("") stores empty/None — semantic "no note"
    note = db.get_user_note(item.id)
    assert not note  # None or ""


def test_note_cancel_returns_read_cell(tmp_path):
    client, db = _client(tmp_path)
    item = _seed(db)
    db.set_user_note(item.id, "kept")
    r = client.get(f"/updates/{item.id}/note/cancel")
    assert r.status_code == 200
    assert "kept" in r.text
    assert ">edit<" in r.text


def test_note_edit_404_on_bogus_id(tmp_path):
    client, _db = _client(tmp_path)
    r = client.get("/updates/nope/note/edit")
    assert r.status_code == 404


def test_note_post_404_on_bogus_id(tmp_path):
    client, _db = _client(tmp_path)
    r = client.post("/updates/nope/note", data={"note": "x"})
    assert r.status_code == 404


def test_note_html_escaping(tmp_path):
    """Notes can contain < and & — must be escaped in the rendered cell."""
    client, db = _client(tmp_path)
    item = _seed(db)
    r = client.post(f"/updates/{item.id}/note", data={"note": "use <script>alert(1)</script> & co"})
    assert r.status_code == 200
    assert "<script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_index_renders_existing_note(tmp_path):
    client, db = _client(tmp_path)
    item = _seed(db, subject="noter")
    db.set_user_note(item.id, "important: pin to 2.4")
    r = client.get("/")
    assert r.status_code == 200
    assert "important: pin to 2.4" in r.text


def test_list_user_notes_returns_only_non_empty(tmp_path):
    _, db = _client(tmp_path)
    a = _seed(db, subject="aa")
    _seed(db, subject="bb")  # no note
    db.set_user_note(a.id, "annotated")
    notes = db.list_user_notes()
    assert notes == {a.id: "annotated"}
