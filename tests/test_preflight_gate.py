"""Tests for the pre-flight breaking-change gate on `POST /updates/<id>/status`."""

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


def _make_cfg(tmp_path: Path, *, preflight: bool) -> Path:
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
        f"  preflight_gate: {'true' if preflight else 'false'}\n"
        "  auth:\n"
        "    enabled: false\n"
    )
    (tmp_path / "config.yaml").write_text(body)
    return tmp_path / "config.yaml"


def _seed_breaking(db) -> str:
    item = AnalyzedUpdate(
        update=Update(source="docker", subject="mealie",
                      current_version="1.0", new_version="2.0",
                      release_url="https://example/release"),
        analysis=Analysis(
            severity=Severity.HIGH,
            summary="Major upgrade",
            breaking_changes=["env: OLD → NEW", "data dir moved"],
            recommended_action="back up DB before applying",
        ),
        status=UpdateStatus.ANALYZED,
    )
    db.upsert(item)
    return item.id


def test_preflight_gate_redirects_when_breaking(tmp_path: Path):
    cfg_path = _make_cfg(tmp_path, preflight=True)
    cfg = load_config(cfg_path)
    app = web.create_app(cfg, cfg_path=cfg_path)
    # Pull the db that the app wired up so we can seed it directly.
    db = next(
        r for r in app.router.routes if hasattr(r, "endpoint")
        and r.endpoint.__name__ == "set_status"
    ).endpoint.__closure__[0].cell_contents  # type: ignore[attr-defined]
    update_id = _seed_breaking(db)
    with TestClient(app) as client:
        # No ack → redirect to preflight
        r = client.post(
            f"/updates/{update_id}/status",
            data={"status": "applied"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == f"/updates/{update_id}/preflight"

        # GET preflight page renders
        r = client.get(f"/updates/{update_id}/preflight")
        assert r.status_code == 200
        assert "env: OLD" in r.text
        assert "data dir moved" in r.text
        assert "back up DB" in r.text

        # POST with ack=yes → flips status
        r = client.post(
            f"/updates/{update_id}/status",
            data={"status": "applied", "ack": "yes"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        rec = db.get(update_id)
        assert rec is not None
        assert rec.status == UpdateStatus.APPLIED


def test_preflight_gate_off_lets_apply_through(tmp_path: Path):
    cfg_path = _make_cfg(tmp_path, preflight=False)
    cfg = load_config(cfg_path)
    app = web.create_app(cfg, cfg_path=cfg_path)
    db = next(
        r for r in app.router.routes if hasattr(r, "endpoint")
        and r.endpoint.__name__ == "set_status"
    ).endpoint.__closure__[0].cell_contents  # type: ignore[attr-defined]
    update_id = _seed_breaking(db)
    with TestClient(app) as client:
        r = client.post(
            f"/updates/{update_id}/status",
            data={"status": "applied"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        assert db.get(update_id).status == UpdateStatus.APPLIED


def test_preflight_gate_skipped_for_dismiss(tmp_path: Path):
    cfg_path = _make_cfg(tmp_path, preflight=True)
    cfg = load_config(cfg_path)
    app = web.create_app(cfg, cfg_path=cfg_path)
    db = next(
        r for r in app.router.routes if hasattr(r, "endpoint")
        and r.endpoint.__name__ == "set_status"
    ).endpoint.__closure__[0].cell_contents  # type: ignore[attr-defined]
    update_id = _seed_breaking(db)
    with TestClient(app) as client:
        # Dismissing should not gate
        r = client.post(
            f"/updates/{update_id}/status",
            data={"status": "dismissed"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        assert db.get(update_id).status == UpdateStatus.DISMISSED


def test_preflight_page_404_when_unknown(tmp_path: Path):
    cfg_path = _make_cfg(tmp_path, preflight=True)
    cfg = load_config(cfg_path)
    app = web.create_app(cfg, cfg_path=cfg_path)
    with TestClient(app) as client:
        r = client.get("/updates/does:not:exist/preflight")
        assert r.status_code == 404
