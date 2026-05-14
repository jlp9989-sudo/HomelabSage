"""Smoke + behaviour tests for the interview routes.

We intentionally do NOT exercise the re-curate path (it would require a
mocked LLM and a fake Docker daemon — out of scope for a smoke). The
test asserts that:

  - GET /interview renders without auth differences vs the dashboard,
  - POST /interview/{id}/dismiss flips status in the DB and redirects,
  - POST /interview/{id}/answer with empty text 400s,
  - GET /interview/banner returns empty when no pending,
  - GET /interview/banner returns markup when there's at least one pending,
  - the dashboard injects the HTMX banner slot.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.config import Config, NotesConfig, StorageConfig, WebConfig
from homelabsage.db import Database
from homelabsage.models import InterviewQuestion, InterviewStatus


def _cfg(tmp_path) -> Config:
    return Config(
        web=WebConfig(enabled=True, host="127.0.0.1", port=0),
        storage=StorageConfig(database_path=str(tmp_path / "state.sqlite")),
        notes=NotesConfig(notes_dir=str(tmp_path / "notes")),
    )


def _seed_question(cfg: Config, **overrides) -> int:
    db = Database(cfg.storage.database_path)
    base = {
        "container_name": "openclaw",
        "image_digest_short": "abc123",
        "question_text": "What is openclaw for?",
    }
    base.update(overrides)
    qid = db.add_interview_question(InterviewQuestion(**base))
    db.close()
    return qid


def test_interview_routes_register(tmp_path):
    cfg = _cfg(tmp_path)
    app = web.create_app(cfg)
    paths = {(r.path, frozenset(r.methods or [])) for r in app.routes if hasattr(r, "methods")}
    assert ("/interview", frozenset({"GET"})) in paths
    assert ("/interview/banner", frozenset({"GET"})) in paths
    assert ("/interview/{question_id}/answer", frozenset({"POST"})) in paths
    assert ("/interview/{question_id}/dismiss", frozenset({"POST"})) in paths


def test_interview_list_page_renders(tmp_path):
    cfg = _cfg(tmp_path)
    qid = _seed_question(cfg)
    app = web.create_app(cfg)
    with TestClient(app) as client:
        r = client.get("/interview")
    assert r.status_code == 200
    assert "openclaw" in r.text
    assert f"/interview/{qid}/answer" in r.text


def test_banner_empty_when_no_pending(tmp_path):
    cfg = _cfg(tmp_path)
    app = web.create_app(cfg)
    with TestClient(app) as client:
        r = client.get("/interview/banner")
    assert r.status_code == 200
    assert r.text.strip() == ""


def test_banner_populated_when_pending_present(tmp_path):
    cfg = _cfg(tmp_path)
    _seed_question(cfg, container_name="a")
    _seed_question(cfg, container_name="b")
    app = web.create_app(cfg)
    with TestClient(app) as client:
        r = client.get("/interview/banner")
    assert r.status_code == 200
    assert "2 interview questions pending" in r.text


def test_banner_singular_when_one_pending(tmp_path):
    cfg = _cfg(tmp_path)
    _seed_question(cfg, container_name="a")
    app = web.create_app(cfg)
    with TestClient(app) as client:
        r = client.get("/interview/banner")
    assert "1 interview question pending" in r.text
    # No stray plural
    assert "1 interview questions" not in r.text


def test_dashboard_injects_banner_slot(tmp_path):
    cfg = _cfg(tmp_path)
    app = web.create_app(cfg)
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 200
    assert 'hx-get="/interview/banner"' in r.text


def test_dismiss_flips_status_and_redirects(tmp_path):
    cfg = _cfg(tmp_path)
    qid = _seed_question(cfg)
    app = web.create_app(cfg)
    with TestClient(app) as client:
        r = client.post(f"/interview/{qid}/dismiss", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/interview"

    db = Database(cfg.storage.database_path)
    back = db.get_interview_question(qid)
    db.close()
    assert back is not None
    assert back.status is InterviewStatus.DISMISSED


def test_answer_empty_text_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    qid = _seed_question(cfg)
    app = web.create_app(cfg)
    with TestClient(app) as client:
        r = client.post(f"/interview/{qid}/answer", data={"text": "   "})
    assert r.status_code == 400


def test_answer_unknown_id_404(tmp_path):
    cfg = _cfg(tmp_path)
    app = web.create_app(cfg)
    with TestClient(app) as client:
        r = client.post("/interview/9999/answer", data={"text": "anything"})
    assert r.status_code == 404


def test_dismiss_unknown_id_404(tmp_path):
    cfg = _cfg(tmp_path)
    app = web.create_app(cfg)
    with TestClient(app) as client:
        r = client.post("/interview/9999/dismiss")
    assert r.status_code == 404


def test_nav_includes_interview_link(tmp_path):
    cfg = _cfg(tmp_path)
    app = web.create_app(cfg)
    with TestClient(app) as client:
        r = client.get("/")
    assert 'href="/interview"' in r.text
