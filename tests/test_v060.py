"""Tests for v0.6.0: generic webhook output, /metrics endpoint, user notes."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.config import WebhookOutputConfig, load_config
from homelabsage.db import Database
from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
    UpdateStatus,
)
from homelabsage.outputs.webhook import WebhookOutput

# ─── webhook output ─────────────────────────────────────────────────


def test_webhook_skips_when_disabled():
    cfg = WebhookOutputConfig(enabled=False, url="http://x")
    out = WebhookOutput(cfg)
    item = AnalyzedUpdate(
        update=Update(source="x", subject="y",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.HIGH, summary="m"),
    )
    asyncio.run(out.send(item))  # no raise


def test_webhook_skips_when_no_url():
    cfg = WebhookOutputConfig(enabled=True, url="")
    out = WebhookOutput(cfg)
    item = AnalyzedUpdate(
        update=Update(source="x", subject="y",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.HIGH, summary="m"),
    )
    assert not out._should_send(item)


def test_webhook_respects_min_severity():
    cfg = WebhookOutputConfig(enabled=True, url="http://x", min_severity="high")
    out = WebhookOutput(cfg)
    low = AnalyzedUpdate(
        update=Update(source="x", subject="y",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.INFO, summary="m"),
    )
    assert not out._should_send(low)


def test_webhook_envelope_shape():
    cfg = WebhookOutputConfig(enabled=True, url="http://x")
    out = WebhookOutput(cfg)
    item = AnalyzedUpdate(
        update=Update(
            source="docker", subject="mealie",
            current_version="1.0", new_version="2.0",
            release_url="https://example/release",
            context={"image": "mealie:2.0"},
        ),
        analysis=Analysis(
            severity=Severity.HIGH, summary="upgrade",
            breaking_changes=["env: A → B"],
            recommended_action="back up",
        ),
    )
    envelope = out._envelope(item)
    assert envelope["type"] == "homelabsage.update"
    assert envelope["severity"] == "high"
    assert envelope["subject"] == "mealie"
    assert envelope["breaking_changes"] == ["env: A → B"]
    assert envelope["context"]["image"] == "mealie:2.0"


def test_webhook_calls_httpx_with_bearer(monkeypatch):
    cfg = WebhookOutputConfig(
        enabled=True, url="http://x.example/hook",
        bearer_token="abc123",
        headers={"X-Custom": "v"},
    )
    out = WebhookOutput(cfg)
    item = AnalyzedUpdate(
        update=Update(source="x", subject="y",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.HIGH, summary="m"),
    )
    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    import homelabsage.outputs.webhook as wh
    monkeypatch.setattr(wh.httpx, "AsyncClient", FakeClient)
    asyncio.run(out.send(item))
    assert captured["url"] == "http://x.example/hook"
    assert captured["headers"]["Authorization"] == "Bearer abc123"
    assert captured["headers"]["X-Custom"] == "v"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["json"]["subject"] == "y"


# ─── /metrics endpoint ──────────────────────────────────────────────


def _bare_app(tmp_path: Path):
    body = (
        "llm:\n  provider: disabled\n  model: stub\n  endpoint: http://stub\n"
        "scheduler:\n  enabled: false\n"
        f"storage:\n  database_path: {tmp_path}/state.sqlite\n"
        "web:\n  enabled: true\n  host: 127.0.0.1\n  port: 0\n"
        "  auth:\n    enabled: false\n"
    )
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(body)
    cfg = load_config(cfg_path)
    return web.create_app(cfg, cfg_path=cfg_path)


def test_metrics_endpoint_returns_prometheus_text(tmp_path: Path):
    app = _bare_app(tmp_path)
    with TestClient(app) as client:
        r = client.get("/metrics")
        assert r.status_code == 200
        text = r.text
        # Stable structure
        assert "homelabsage_info" in text
        assert "homelabsage_updates_total" in text
        assert "homelabsage_pending_dispatches" in text
        assert "homelabsage_interview_questions" in text
        # Content-type follows the Prometheus text-exposition convention
        assert "text/plain" in r.headers["content-type"]


def test_metrics_escapes_label_values():
    """The escape helper produces valid Prometheus label values."""
    from homelabsage.web.routes_metrics import _esc_label
    assert _esc_label('hi "world"\n') == 'hi \\"world\\"\\n'
    assert _esc_label("path\\with\\back") == "path\\\\with\\\\back"


def test_metrics_bypasses_auth(tmp_path: Path):
    body = (
        "llm:\n  provider: disabled\n  model: stub\n  endpoint: http://stub\n"
        "scheduler:\n  enabled: false\n"
        f"storage:\n  database_path: {tmp_path}/state.sqlite\n"
        "web:\n  enabled: true\n  host: 127.0.0.1\n  port: 0\n"
        "  auth:\n    enabled: true\n    username: u\n    password: p\n"
    )
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(body)
    cfg = load_config(cfg_path)
    app = web.create_app(cfg, cfg_path=cfg_path)
    with TestClient(app) as client:
        r = client.get("/metrics")
        assert r.status_code == 200


# ─── user notes ─────────────────────────────────────────────────────


def test_user_note_db_round_trip(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        item = AnalyzedUpdate(
            update=Update(source="docker", subject="x",
                          current_version="1", new_version="2"),
            analysis=Analysis(severity=Severity.HIGH, summary="m"),
            status=UpdateStatus.ANALYZED,
        )
        db.upsert(item)
        ok = db.set_user_note(item.id, "remember to back up the DB")
        assert ok
        assert db.get_user_note(item.id) == "remember to back up the DB"
    finally:
        db.close()


def test_user_note_clearing_with_empty_string(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        item = AnalyzedUpdate(
            update=Update(source="docker", subject="x",
                          current_version="1", new_version="2"),
            analysis=Analysis(severity=Severity.HIGH, summary="m"),
            status=UpdateStatus.ANALYZED,
        )
        db.upsert(item)
        db.set_user_note(item.id, "first")
        db.set_user_note(item.id, "")
        assert db.get_user_note(item.id) in (None, "")
    finally:
        db.close()


def test_user_note_unknown_id_returns_false(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        assert db.set_user_note("nope:x:1", "hi") is False
        assert db.get_user_note("nope:x:1") is None
    finally:
        db.close()


def test_user_note_api_round_trip(tmp_path: Path):
    app = _bare_app(tmp_path)
    db = next(
        r for r in app.router.routes if hasattr(r, "endpoint")
        and r.endpoint.__name__ == "set_status"
    ).endpoint.__closure__[0].cell_contents
    item = AnalyzedUpdate(
        update=Update(source="docker", subject="x",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.HIGH, summary="m"),
        status=UpdateStatus.ANALYZED,
    )
    db.upsert(item)
    with TestClient(app) as client:
        # GET — initially empty
        r = client.get(f"/api/updates/{item.id}/note")
        assert r.status_code == 200
        assert r.json()["note"] == ""
        # POST — set
        r = client.post(
            f"/api/updates/{item.id}/note",
            json={"note": "watch out for migration"},
        )
        assert r.status_code == 200
        # GET — round trip
        r = client.get(f"/api/updates/{item.id}/note")
        assert r.json()["note"] == "watch out for migration"


def test_user_note_api_404(tmp_path: Path):
    app = _bare_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/api/updates/nope:x:1/note", json={"note": "x"})
        assert r.status_code == 404


def test_user_note_api_rejects_non_string(tmp_path: Path):
    app = _bare_app(tmp_path)
    db = next(
        r for r in app.router.routes if hasattr(r, "endpoint")
        and r.endpoint.__name__ == "set_status"
    ).endpoint.__closure__[0].cell_contents
    item = AnalyzedUpdate(
        update=Update(source="docker", subject="x",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.HIGH, summary="m"),
        status=UpdateStatus.ANALYZED,
    )
    db.upsert(item)
    with TestClient(app) as client:
        r = client.post(
            f"/api/updates/{item.id}/note", json={"note": 42},
        )
        assert r.status_code == 400
