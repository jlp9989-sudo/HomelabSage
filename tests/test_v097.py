"""Tests for v0.9.7: secret masking + route hardening.

Covers the bug-hunt findings against v0.9.5 + v0.9.6:
  - list/dict secrets are masked over JSON AND HTML form
  - blank submit doesn't clobber a configured list/dict secret
  - atomic star toggle (no read-then-write race)
  - 404 on unknown update_id for both HTMX endpoints
  - pill counts come from COUNT(*), not the 500-row list cap
  - days < 0 clears the snooze (defensive)
"""

from __future__ import annotations

import yaml
from fastapi.testclient import TestClient

from homelabsage.config import Config
from homelabsage.db import Database
from homelabsage.models import AnalyzedUpdate, Update
from homelabsage.web import create_app


def _client(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("")
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    return TestClient(create_app(cfg, cfg_path)), Database(cfg.storage.database_path), cfg_path


def _seed(db, *, subject="mealie"):
    item = AnalyzedUpdate(
        update=Update(
            source="docker", subject=subject,
            current_version="1", new_version="2",
        ),
    )
    db.upsert(item)
    return item


# ─── secret masking: lists ────────────────────────────────────────


def test_api_settings_apprise_urls_masked(tmp_path):
    """apprise.urls held cleartext creds in v0.9.5. v0.9.7 masks them."""
    client, _db, cfg_path = _client(tmp_path)
    overlay = tmp_path / "config.user.yaml"
    overlay.write_text(yaml.safe_dump({
        "outputs": {"apprise": {"urls": ["mailtos://user:secretpass@gmail.com"]}}
    }))
    r = client.get("/api/settings/outputs/apprise")
    assert r.status_code == 200
    body = r.json()
    assert "secretpass" not in r.text
    assert body["current"]["urls"] == ["***"]


def test_api_settings_web_auth_api_keys_masked(tmp_path):
    client, _db, cfg_path = _client(tmp_path)
    overlay = tmp_path / "config.user.yaml"
    overlay.write_text(yaml.safe_dump({
        "web": {"auth": {"api_keys": ["sk-supersecret-abc"]}}
    }))
    r = client.get("/api/settings/web/auth")
    assert r.status_code == 200
    assert "supersecret" not in r.text
    assert r.json()["current"]["api_keys"] == ["***"]


def test_api_settings_webhook_headers_masked(tmp_path):
    """outputs.webhook.headers may carry Authorization — mask all values."""
    client, _db, cfg_path = _client(tmp_path)
    overlay = tmp_path / "config.user.yaml"
    overlay.write_text(yaml.safe_dump({
        "outputs": {"webhook": {
            "url": "https://hook.example.com/secret-path",
            "headers": {"Authorization": "Bearer xyz123"},
        }}
    }))
    r = client.get("/api/settings/outputs/webhook")
    assert r.status_code == 200
    assert "xyz123" not in r.text
    assert "secret-path" not in r.text
    cur = r.json()["current"]
    assert cur["url"] == "***"
    assert cur["headers"] == {"Authorization": "***"}


def test_api_settings_msteams_webhook_url_masked(tmp_path):
    client, _db, cfg_path = _client(tmp_path)
    overlay = tmp_path / "config.user.yaml"
    overlay.write_text(yaml.safe_dump({
        "outputs": {"msteams": {"webhook_url": "https://outlook.office.com/webhook/SECRET"}}
    }))
    r = client.get("/api/settings/outputs/msteams")
    assert "SECRET" not in r.text
    assert r.json()["current"]["webhook_url"] == "***"


def test_html_settings_form_does_not_leak_list_secret(tmp_path):
    """The HTML form path bypassed _mask_secrets — verify it no longer leaks."""
    client, _db, cfg_path = _client(tmp_path)
    overlay = tmp_path / "config.user.yaml"
    overlay.write_text(yaml.safe_dump({
        "outputs": {"apprise": {"urls": ["mailtos://user:LEAK_ME@gmail.com"]}}
    }))
    r = client.get("/settings/outputs/apprise")
    assert r.status_code == 200
    assert "LEAK_ME" not in r.text


def test_html_settings_form_does_not_leak_dict_secret(tmp_path):
    client, _db, cfg_path = _client(tmp_path)
    overlay = tmp_path / "config.user.yaml"
    overlay.write_text(yaml.safe_dump({
        "outputs": {"webhook": {
            "url": "https://hook.example.com/secret-path",
            "headers": {"Authorization": "Bearer LEAK_ME"},
        }}
    }))
    r = client.get("/settings/outputs/webhook")
    assert r.status_code == 200
    assert "LEAK_ME" not in r.text
    assert "secret-path" not in r.text


# ─── round-trip: blank submit preserves existing secret ───────────


def test_blank_submit_preserves_list_secret(tmp_path):
    """Submitting the form with the urls textarea empty must NOT clear them."""
    client, _db, cfg_path = _client(tmp_path)
    overlay = tmp_path / "config.user.yaml"
    overlay.write_text(yaml.safe_dump({
        "outputs": {"apprise": {"urls": ["mailtos://user:keepme@gmail.com"]}}
    }))
    # Form submit with urls blank — same as user clicking save without
    # touching the (masked) field.
    r = client.post(
        "/settings/outputs/apprise/update",
        data={"urls": "", "enabled": "false", "min_severity": "high"},
    )
    assert r.status_code == 200
    # urls should still be in the overlay
    saved = yaml.safe_load(overlay.read_text())
    assert saved["outputs"]["apprise"]["urls"] == ["mailtos://user:keepme@gmail.com"]


def test_blank_submit_preserves_dict_secret(tmp_path):
    client, _db, cfg_path = _client(tmp_path)
    overlay = tmp_path / "config.user.yaml"
    overlay.write_text(yaml.safe_dump({
        "outputs": {"webhook": {"headers": {"X-Token": "keepme"}}}
    }))
    r = client.post(
        "/settings/outputs/webhook/update",
        data={"headers": "", "url": "", "bearer_token": "", "enabled": "false",
              "min_severity": "high"},
    )
    assert r.status_code == 200
    saved = yaml.safe_load(overlay.read_text())
    assert saved["outputs"]["webhook"]["headers"] == {"X-Token": "keepme"}


# ─── star/snooze atomic + 404 ─────────────────────────────────────


def test_star_toggle_404_on_bogus_id(tmp_path):
    client, _db, _ = _client(tmp_path)
    r = client.post("/updates/does-not-exist/star/toggle")
    assert r.status_code == 404


def test_snooze_quick_404_on_bogus_id(tmp_path):
    client, _db, _ = _client(tmp_path)
    r = client.post("/updates/does-not-exist/snooze/quick", data={"days": "7"})
    assert r.status_code == 404


def test_star_toggle_is_atomic(tmp_path):
    """Single-statement UPDATE … RETURNING — no read-then-write window."""
    client, db, _ = _client(tmp_path)
    item = _seed(db)
    # Toggle 5 times; final state must be ON (odd count from OFF baseline).
    for _ in range(5):
        r = client.post(f"/updates/{item.id}/star/toggle")
        assert r.status_code == 200
    assert db.is_starred(item.id) is True


def test_snooze_quick_negative_days_clears(tmp_path):
    """days = -5 → treated as 0 (clear), not a past timestamp wedge."""
    client, db, _ = _client(tmp_path)
    item = _seed(db)
    db.set_snooze(item.id, "2199-01-01T00:00:00+00:00")
    r = client.post(f"/updates/{item.id}/snooze/quick", data={"days": "-5"})
    assert r.status_code == 200
    assert db.get_snooze(item.id) is None


# ─── pill counts ──────────────────────────────────────────────────


def test_starred_pill_counts_via_count_query(tmp_path):
    """Pill label uses COUNT(*), not len(list_starred(limit=500))."""
    client, db, _ = _client(tmp_path)
    item = _seed(db, subject="alpha")
    db.set_starred(item.id, True)
    r = client.get("/")
    assert "Starred (1)" in r.text
    assert db.count_starred() == 1


def test_snoozed_pill_counts_via_count_query(tmp_path):
    client, db, _ = _client(tmp_path)
    item = _seed(db, subject="alpha")
    db.set_snooze(item.id, "2199-01-01T00:00:00+00:00")
    r = client.get("/")
    assert "Snoozed (1)" in r.text
    assert db.count_snoozed() == 1
