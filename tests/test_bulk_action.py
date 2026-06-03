"""Tests for the POST /api/updates/bulk endpoint."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.config import load_config
from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
    UpdateStatus,
)


def _make_cfg(tmp_path: Path) -> Path:
    body = (
        "llm:\n"
        "  provider: disabled\n"
        "  model: stub\n"
        "  endpoint: http://stub\n"
        "scheduler:\n"
        "  enabled: false\n"
        "storage:\n"
        f"  database_path: {tmp_path}/state.sqlite\n"
        "web:\n"
        "  enabled: true\n"
        "  host: 127.0.0.1\n"
        "  port: 0\n"
        "  auth:\n"
        "    enabled: false\n"
    )
    (tmp_path / "config.yaml").write_text(body)
    return tmp_path / "config.yaml"


def _seed(db, *, count: int = 3) -> list[str]:
    ids: list[str] = []
    for i in range(count):
        item = AnalyzedUpdate(
            update=Update(source="docker", subject=f"c{i}",
                          current_version="1", new_version=f"{i + 2}"),
            analysis=Analysis(severity=Severity.MEDIUM, summary="x"),
            status=UpdateStatus.ANALYZED,
        )
        db.upsert(item)
        ids.append(item.id)
    return ids


def _db_from_app(app):
    """Pull the Database instance the route closures captured."""
    return next(
        r for r in app.router.routes if hasattr(r, "endpoint")
        and r.endpoint.__name__ == "set_status"
    ).endpoint.__closure__[0].cell_contents


def test_bulk_apply_marks_all(tmp_path: Path):
    cfg_path = _make_cfg(tmp_path)
    cfg = load_config(cfg_path)
    app = web.create_app(cfg, cfg_path=cfg_path)
    db = _db_from_app(app)
    ids = _seed(db)
    with TestClient(app) as client:
        r = client.post(
            "/api/updates/bulk",
            json={"ids": ids, "status": "applied"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "applied"
        assert body["applied"] == 3
        assert body["not_found"] == []
        for uid in ids:
            assert db.get(uid).status == UpdateStatus.APPLIED


def test_bulk_dismiss(tmp_path: Path):
    cfg_path = _make_cfg(tmp_path)
    cfg = load_config(cfg_path)
    app = web.create_app(cfg, cfg_path=cfg_path)
    db = _db_from_app(app)
    ids = _seed(db, count=2)
    with TestClient(app) as client:
        r = client.post(
            "/api/updates/bulk",
            json={"ids": ids, "status": "dismissed"},
        )
        assert r.status_code == 200
        assert r.json()["applied"] == 2
        for uid in ids:
            assert db.get(uid).status == UpdateStatus.DISMISSED


def test_bulk_reports_not_found_ids(tmp_path: Path):
    cfg_path = _make_cfg(tmp_path)
    cfg = load_config(cfg_path)
    app = web.create_app(cfg, cfg_path=cfg_path)
    db = _db_from_app(app)
    ids = _seed(db, count=1)
    with TestClient(app) as client:
        r = client.post(
            "/api/updates/bulk",
            json={"ids": ids + ["bogus:x:1", "also:bogus"], "status": "applied"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["applied"] == 1
        assert body["not_found_count"] == 2
        assert "bogus:x:1" in body["not_found"]


def test_bulk_rejects_invalid_status(tmp_path: Path):
    cfg_path = _make_cfg(tmp_path)
    cfg = load_config(cfg_path)
    app = web.create_app(cfg, cfg_path=cfg_path)
    with TestClient(app) as client:
        r = client.post(
            "/api/updates/bulk",
            json={"ids": ["x"], "status": "exploded"},
        )
        assert r.status_code == 400


def test_bulk_rejects_missing_payload(tmp_path: Path):
    cfg_path = _make_cfg(tmp_path)
    cfg = load_config(cfg_path)
    app = web.create_app(cfg, cfg_path=cfg_path)
    with TestClient(app) as client:
        r = client.post("/api/updates/bulk", json={})
        assert r.status_code == 400


def test_bulk_skips_non_string_ids(tmp_path: Path):
    cfg_path = _make_cfg(tmp_path)
    cfg = load_config(cfg_path)
    app = web.create_app(cfg, cfg_path=cfg_path)
    db = _db_from_app(app)
    ids = _seed(db, count=1)
    with TestClient(app) as client:
        r = client.post(
            "/api/updates/bulk",
            json={"ids": ids + [42, None, {"x": 1}], "status": "dismissed"},
        )
        assert r.status_code == 200
        assert r.json()["applied"] == 1
