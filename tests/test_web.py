"""Smoke tests for the web package after the v0.5 split.

We don't try to test rendering or HTMX — those are visual. The point of
these tests is that `create_app(cfg)` still wires the same set of routes
it did when the whole web layer was one file, and that the public API
surface (`create_app`, `run_web`) is importable from `homelabsage.web`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.config import Config, NotesConfig, StorageConfig, WebAuthConfig, WebConfig


def _cfg(tmp_path, *, auth: bool = False) -> Config:
    return Config(
        web=WebConfig(
            enabled=True,
            host="127.0.0.1",
            port=0,
            auth=WebAuthConfig(enabled=auth, username="admin", password="pw" if auth else ""),
        ),
        storage=StorageConfig(database_path=str(tmp_path / "state.sqlite")),
        notes=NotesConfig(notes_dir=str(tmp_path / "notes")),
    )


def test_public_api_surface_kept():
    """The split must not break `from homelabsage.web import create_app, run_web`."""
    assert callable(web.create_app)
    assert callable(web.run_web)
    assert set(web.__all__) == {"create_app", "run_web"}


def test_all_expected_routes_register(tmp_path):
    cfg = _cfg(tmp_path)
    app = web.create_app(cfg)
    paths = {(r.path, frozenset(r.methods or [])) for r in app.routes if hasattr(r, "methods")}
    assert ("/", frozenset({"GET"})) in paths
    assert ("/run", frozenset({"POST"})) in paths
    assert ("/updates/{update_id:path}/status", frozenset({"POST"})) in paths
    assert ("/api/updates", frozenset({"GET"})) in paths
    assert ("/notes", frozenset({"GET"})) in paths
    assert ("/notes/edit", frozenset({"GET"})) in paths
    assert ("/notes/save", frozenset({"POST"})) in paths
    assert ("/notes/delete", frozenset({"POST"})) in paths
    assert ("/healthz", frozenset({"GET"})) in paths


def test_healthz_responds(tmp_path):
    app = web.create_app(_cfg(tmp_path))
    with TestClient(app) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_auth_blocks_unauthenticated(tmp_path):
    """When auth is on, the dashboard requires creds; healthz stays open."""
    app = web.create_app(_cfg(tmp_path, auth=True))
    with TestClient(app) as client:
        r1 = client.get("/", follow_redirects=False)
        r2 = client.get("/healthz")
    assert r1.status_code == 401
    assert r2.status_code == 200


def test_auth_passes_with_correct_credentials(tmp_path):
    app = web.create_app(_cfg(tmp_path, auth=True))
    with TestClient(app) as client:
        r = client.get("/", auth=("admin", "pw"))
    assert r.status_code == 200


def test_auth_rejects_wrong_password(tmp_path):
    app = web.create_app(_cfg(tmp_path, auth=True))
    with TestClient(app) as client:
        r = client.get("/", auth=("admin", "wrong"))
    assert r.status_code == 401


@pytest.mark.parametrize(
    "missing",
    ["create_app", "run_web"],
)
def test_attr_access_is_stable(missing):
    """Guard against accidentally renaming the public symbols."""
    assert hasattr(web, missing)
