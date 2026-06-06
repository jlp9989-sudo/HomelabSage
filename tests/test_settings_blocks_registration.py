"""Tests for v0.9.5: GUI surface expansion — every config block reachable."""

from __future__ import annotations

from fastapi.testclient import TestClient

from homelabsage.config import Config
from homelabsage.web import create_app
from homelabsage.web.routes_settings import SETTING_BLOCKS


def _client(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    return TestClient(create_app(cfg))


def test_every_block_registered_resolves_via_api(tmp_path):
    """Each entry in SETTING_BLOCKS must return 200 from `/api/settings/{block}`."""
    client = _client(tmp_path)
    for block in SETTING_BLOCKS:
        r = client.get(f"/api/settings/{block}")
        assert r.status_code == 200, (
            f"{block} returned {r.status_code}: {r.text[:200]}"
        )
        body = r.json()
        # Each response carries `current` + `overlay_keys` + `schema`.
        assert "schema" in body, f"{block} missing schema"
        assert "current" in body, f"{block} missing current"


def test_block_index_lists_all_blocks(tmp_path):
    """`GET /api/settings` returns the same set as SETTING_BLOCKS."""
    client = _client(tmp_path)
    r = client.get("/api/settings")
    assert r.status_code == 200
    blocks = {b["id"] for b in r.json()["blocks"]}
    assert blocks == set(SETTING_BLOCKS), (
        f"missing: {set(SETTING_BLOCKS) - blocks}, "
        f"extra: {blocks - set(SETTING_BLOCKS)}"
    )


def test_html_settings_index_renders_all_blocks(tmp_path):
    """The `/settings` HTML index should mention every block by name."""
    client = _client(tmp_path)
    r = client.get("/settings")
    assert r.status_code == 200
    body = r.text
    for block in SETTING_BLOCKS:
        # The block index renders `<a href="/settings/{block}">` cards.
        assert f"/settings/{block}" in body, (
            f"{block} card not on /settings page"
        )


def test_every_block_html_form_renders(tmp_path):
    """`GET /settings/{block}` returns HTML 200 for every block.

    Catches: a config block whose JSON Schema trips the form renderer
    (nested objects, weird defaults). Previous discovery: schema-driven
    renderer auto-handles new blocks — this test guards that promise.
    """
    client = _client(tmp_path)
    for block in SETTING_BLOCKS:
        r = client.get(f"/settings/{block}")
        assert r.status_code == 200, (
            f"{block} HTML form 500: {r.text[:200]}"
        )
        # Should render the block name somewhere (heading + breadcrumb).
        # Use a loose check — the block title may differ from the URL slug.
        assert "<form" in r.text or block in r.text


def test_v05plus_blocks_are_registered():
    """Defensive: confirm the v0.6+/v0.7+/v0.8+ config blocks landed."""
    expected = {
        # outputs
        "outputs/apprise", "outputs/smtp", "outputs/slack",
        "outputs/msteams", "outputs/pushover", "outputs/webhook",
        "outputs/batching",
        # runtime
        "scan_window", "llm_health_gate", "auto_apply", "image_pins",
        "compose_lint", "tag_lag", "tls_check", "disk_pressure",
        "audit_alerts", "backup_health", "health_check",
        "log_anomaly", "i18n",
        # sources / web auth
        "sources/fedora", "sources/scripts", "web/auth",
    }
    missing = expected - set(SETTING_BLOCKS)
    assert not missing, f"missing blocks: {missing}"
