"""Tests for v0.6.9: snooze engine wiring, GitHub webhook, restart-drift."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from homelabsage.config import Config
from homelabsage.restart_drift import (
    RestartDriftFinding,
)
from homelabsage.restart_drift import (
    evaluate as eval_drift,
)

# ─── restart_drift ────────────────────────────────────────────────


def test_restart_drift_recent_no_apply_flags():
    now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)
    started = now - timedelta(minutes=30)
    out = eval_drift(started_at=started, now=now)
    assert out is not None
    assert out.severity == "info"
    assert 0.4 <= out.hours_since_start <= 0.6


def test_restart_drift_too_old_no_flag():
    now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)
    started = now - timedelta(hours=24)
    assert eval_drift(started_at=started, now=now) is None


def test_restart_drift_too_recent_no_flag():
    """Just-started containers race with scan; skip <min_hours."""
    now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)
    started = now - timedelta(minutes=5)
    assert eval_drift(started_at=started, now=now) is None


def test_restart_drift_attributed_to_recent_apply():
    now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)
    started = now - timedelta(minutes=30)
    # apply was 15 min before restart — within 1h window
    applied = started - timedelta(minutes=15)
    assert eval_drift(
        started_at=started, now=now, last_applied_at=applied,
    ) is None


def test_restart_drift_distant_apply_does_not_attribute():
    now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)
    started = now - timedelta(minutes=30)
    # apply was 5 days before restart
    applied = started - timedelta(days=5)
    out = eval_drift(
        started_at=started, now=now, last_applied_at=applied,
    )
    assert out is not None
    assert out.has_recent_apply is False


def test_restart_drift_handles_naive_timestamps():
    now = datetime(2026, 6, 4, 12, 0, 0)
    started = now - timedelta(minutes=30)
    out = eval_drift(started_at=started, now=now)
    assert out is not None


def test_restart_drift_none_started_at_returns_none():
    now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)
    assert eval_drift(started_at=None, now=now) is None


def test_restart_drift_to_context_shape():
    d = RestartDriftFinding(
        started_at="2026-06-04T11:30:00+00:00",
        hours_since_start=0.5,
        has_recent_apply=False,
        severity="info",
    )
    ctx = d.to_context()
    assert ctx["hours_since_start"] == 0.5
    assert ctx["severity"] == "info"


# ─── GitHub release webhook ───────────────────────────────────────
# Snooze engine wiring tests moved to test_snooze.py in v0.11.6.



def _make_release_payload(*, tag="v1.0.0", repo="owner/repo",
                          action="released") -> dict:
    return {
        "action": action,
        "release": {
            "tag_name": tag,
            "html_url": f"https://github.com/{repo}/releases/tag/{tag}",
            "body": "Major release with breaking changes.",
        },
        "repository": {"full_name": repo},
    }


def test_github_release_webhook_creates_update(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RELEASE_WEBHOOK_SECRET", "topsecret")
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    app = create_app_for_test(cfg)
    client = TestClient(app)
    payload_bytes = json.dumps(_make_release_payload()).encode()
    sig = "sha256=" + hmac.new(
        b"topsecret", payload_bytes, hashlib.sha256,
    ).hexdigest()
    r = client.post(
        "/api/webhook/github-release",
        content=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"]
    assert body["subject"] == "owner/repo"
    assert body["tag"] == "v1.0.0"


def test_github_release_webhook_rejects_when_secret_unset(tmp_path):
    """Without GITHUB_RELEASE_WEBHOOK_SECRET, endpoint refuses (503)."""
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    app = create_app_for_test(cfg)
    client = TestClient(app)
    r = client.post(
        "/api/webhook/github-release",
        json=_make_release_payload(),
    )
    assert r.status_code == 503


def test_github_release_webhook_skips_non_released_action(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RELEASE_WEBHOOK_SECRET", "topsecret")
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    app = create_app_for_test(cfg)
    client = TestClient(app)
    payload_bytes = json.dumps(_make_release_payload(action="prereleased")).encode()
    sig = "sha256=" + hmac.new(
        b"topsecret", payload_bytes, hashlib.sha256,
    ).hexdigest()
    r = client.post(
        "/api/webhook/github-release",
        content=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )
    assert r.status_code == 200
    assert r.json().get("skipped") is True


def test_github_release_webhook_rejects_missing_tag(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RELEASE_WEBHOOK_SECRET", "topsecret")
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    app = create_app_for_test(cfg)
    client = TestClient(app)
    payload = _make_release_payload()
    del payload["release"]["tag_name"]
    payload_bytes = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(
        b"topsecret", payload_bytes, hashlib.sha256,
    ).hexdigest()
    r = client.post(
        "/api/webhook/github-release",
        content=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )
    assert r.status_code == 400


def test_github_release_webhook_rejects_bad_signature(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RELEASE_WEBHOOK_SECRET", "topsecret")
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    app = create_app_for_test(cfg)
    client = TestClient(app)
    payload_bytes = json.dumps(_make_release_payload()).encode()
    r = client.post(
        "/api/webhook/github-release",
        content=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
        },
    )
    assert r.status_code == 401


def test_github_release_webhook_accepts_valid_signature(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RELEASE_WEBHOOK_SECRET", "topsecret")
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    app = create_app_for_test(cfg)
    client = TestClient(app)
    payload_bytes = json.dumps(_make_release_payload()).encode()
    sig = "sha256=" + hmac.new(
        b"topsecret", payload_bytes, hashlib.sha256,
    ).hexdigest()
    r = client.post(
        "/api/webhook/github-release",
        content=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )
    assert r.status_code == 200, r.text


def create_app_for_test(cfg):
    from homelabsage.web import create_app
    return create_app(cfg)
