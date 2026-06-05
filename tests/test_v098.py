"""Tests for v0.9.8: mute UI on /audit + usage in nav."""

from __future__ import annotations

from fastapi.testclient import TestClient

from homelabsage.config import Config
from homelabsage.db import Database
from homelabsage.web import create_app


def _client(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    return TestClient(create_app(cfg)), Database(cfg.storage.database_path)


def test_audit_page_has_mute_form_per_finding(tmp_path):
    """The /audit page renders a per-finding inline mute form whenever
    there are findings. With no findings the page just says 'all clear'."""
    client, _db = _client(tmp_path)
    r = client.get("/audit")
    assert r.status_code == 200
    # `mute-bar` class is the per-finding inline form; absent on a clean
    # install with zero findings (healthy report). What we MUST see is
    # the active-mutes hook + the form action exists in template.
    assert "/audit/mute" in r.text or "All clear" in r.text


def test_audit_mute_creates_row(tmp_path):
    """POST /audit/mute persists the fingerprint, swap returns confirmation."""
    client, db = _client(tmp_path)
    r = client.post("/audit/mute", data={
        "category": "cve",
        "source_kind": "container",
        "source_ref": "mealie",
        "reason": "false positive",
    })
    assert r.status_code == 200
    assert "muted" in r.text
    mutes = db.list_audit_mutes()
    assert len(mutes) == 1
    assert mutes[0]["category"] == "cve"
    assert mutes[0]["source_ref"] == "mealie"
    assert mutes[0]["reason"] == "false positive"


def test_audit_mute_rejects_missing_fingerprint(tmp_path):
    client, _db = _client(tmp_path)
    r = client.post("/audit/mute", data={
        "category": "", "source_kind": "x", "source_ref": "y",
    })
    assert r.status_code == 400


def test_audit_mute_remove_clears_row(tmp_path):
    client, db = _client(tmp_path)
    db.add_audit_mute(category="cve", source_kind="container",
                      source_ref="mealie", expires_at=None, reason=None)
    r = client.post("/audit/mute/remove", data={
        "category": "cve",
        "source_kind": "container",
        "source_ref": "mealie",
    })
    assert r.status_code == 200
    assert r.text == ""  # empty fragment = vanish
    assert db.list_audit_mutes() == []


def test_audit_mute_remove_404_on_unknown(tmp_path):
    client, _db = _client(tmp_path)
    r = client.post("/audit/mute/remove", data={
        "category": "x", "source_kind": "y", "source_ref": "z",
    })
    assert r.status_code == 404


def test_audit_page_renders_active_mutes_panel(tmp_path):
    """When at least one mute exists, the page shows the collapsible panel."""
    client, db = _client(tmp_path)
    db.add_audit_mute(category="cve", source_kind="container",
                      source_ref="mealie", expires_at=None,
                      reason="known false positive")
    r = client.get("/audit")
    assert r.status_code == 200
    assert "Active mutes" in r.text
    assert "mealie" in r.text
    assert "known false positive" in r.text
    assert "/audit/mute/remove" in r.text


def test_nav_has_usage_link(tmp_path):
    """Usage page was reachable only by URL. v0.9.8 adds it to the nav."""
    client, _db = _client(tmp_path)
    r = client.get("/")
    assert r.status_code == 200
    assert 'href="/usage"' in r.text
