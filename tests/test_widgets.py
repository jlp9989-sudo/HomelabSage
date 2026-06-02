"""Tests for the gethomepage / Homarr widget endpoints."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.config import Config
from homelabsage.db import Database
from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
    UpdateStatus,
)


def _seed(db: Database) -> None:
    # 1 critical pending, 1 high pending, 1 applied (excluded from pending count)
    items = [
        ("docker", "openclaw", Severity.CRITICAL, UpdateStatus.ANALYZED),
        ("docker", "tintes", Severity.HIGH, UpdateStatus.ANALYZED),
        ("docker", "mealie", Severity.INFO, UpdateStatus.APPLIED),
    ]
    for source, subject, sev, status in items:
        item = AnalyzedUpdate(
            update=Update(source=source, subject=subject,
                          current_version="1", new_version="2"),
            analysis=Analysis(severity=sev, summary="—"),
            status=status,
            detected_at=datetime(2026, 6, 1, 12, 0, 0),
        )
        db.upsert(item)


def test_homepage_widget_returns_label_value_rows(tmp_path: Path):
    cfg = Config()
    cfg.web.auth.enabled = False
    cfg.scheduler.enabled = False
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    db = Database(cfg.storage.database_path)
    try:
        _seed(db)
    finally:
        db.close()
    app = web.create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/widget/homepage")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list)
        rows = {row["label"]: row["value"] for row in body}
        assert rows["Pending"] == 2  # applied is excluded
        assert rows["Critical"] == 1
        assert rows["High"] == 1


def test_homarr_widget_returns_flat_json(tmp_path: Path):
    cfg = Config()
    cfg.web.auth.enabled = False
    cfg.scheduler.enabled = False
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    db = Database(cfg.storage.database_path)
    try:
        _seed(db)
    finally:
        db.close()
    app = web.create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/widget/homarr")
        assert r.status_code == 200
        body = r.json()
        assert body["pending"] == 2
        assert body["critical"] == 1
        assert body["high"] == 1
        assert body["parity_active"] is False
        assert body["queued_dispatches"] == 0
        assert body["last_scan_at"] is not None


def test_widget_endpoints_bypass_basic_auth(tmp_path: Path):
    """gethomepage/Homarr scrape these — they MUST be reachable without creds."""
    cfg = Config()
    cfg.web.auth.enabled = True
    cfg.web.auth.username = "javi"
    cfg.web.auth.password = "secret"
    cfg.scheduler.enabled = False
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    app = web.create_app(cfg)
    with TestClient(app) as c:
        # No Authorization header → other routes 401, widget endpoints 200
        assert c.get("/").status_code == 401
        assert c.get("/widget/homepage").status_code == 200
        assert c.get("/widget/homarr").status_code == 200


def test_homarr_widget_handles_empty_db(tmp_path: Path):
    cfg = Config()
    cfg.web.auth.enabled = False
    cfg.scheduler.enabled = False
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    app = web.create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/widget/homarr")
        body = r.json()
        assert body["pending"] == 0
        assert body["last_scan_at"] is None
