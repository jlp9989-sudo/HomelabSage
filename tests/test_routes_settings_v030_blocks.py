"""Confirm the v0.3.0 config blocks are exposed by /api/settings.

The settings UI renders schema-driven forms from `Config`'s Pydantic
submodels. Each new submodel (digest, parity_gate, outputs/discord, etc.)
must:
  - Appear in /api/settings (the block index).
  - Return its current values via GET /api/settings/<id>.
  - Accept a PATCH and round-trip via GET.
This guards against future Config edits silently dropping a block from the
form list.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.config import load_config


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "llm:\n"
        "  provider: openai\n"
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
    return tmp_path


@pytest.fixture
def client(cfg_dir: Path) -> TestClient:
    cfg_path = cfg_dir / "config.yaml"
    cfg = load_config(cfg_path)
    app = web.create_app(cfg, cfg_path=cfg_path)
    return TestClient(app)


# ─── block index ────────────────────────────────────────────────────────


def test_v030_blocks_appear_in_block_index(client):
    """Each new top-level block lands in `GET /api/settings`."""
    body = client.get("/api/settings").json()
    ids = {b["id"] for b in body["blocks"]}
    for block in (
        "digest",
        "parity_gate",
        "outputs/discord",
        "outputs/ntfy",
        "outputs/gotify",
        "sources/github_watched",
    ):
        assert block in ids, f"missing settings block: {block}"


# ─── digest ─────────────────────────────────────────────────────────────


def test_get_digest_returns_defaults(client):
    body = client.get("/api/settings/digest").json()
    cur = body["current"]
    assert cur["enabled"] is False
    assert cur["cron"] == "0 9 * * 0"
    assert cur["lookback_days"] == 7
    assert cur["channels"] == []
    assert "properties" in body["schema"]


def test_patch_digest_validates_channels(client):
    """Unknown channels must 4xx — `DigestConfig.channels` has a validator."""
    r = client.patch("/api/settings/digest", json={"channels": ["pigeon"]})
    assert 400 <= r.status_code < 500


def test_patch_digest_round_trip(client, cfg_dir):
    r = client.patch(
        "/api/settings/digest",
        json={"enabled": True, "channels": ["telegram", "ntfy"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["current"]["enabled"] is True
    assert body["current"]["channels"] == ["telegram", "ntfy"]


# ─── parity gate ────────────────────────────────────────────────────────


def test_get_parity_gate_defaults(client):
    body = client.get("/api/settings/parity_gate").json()
    cur = body["current"]
    assert cur["enabled"] is False
    assert cur["mdstat_path"] == "/proc/mdstat"
    assert cur["skip_digest_too"] is False


def test_patch_parity_gate_skip_digest_too(client):
    r = client.patch(
        "/api/settings/parity_gate",
        json={"enabled": True, "skip_digest_too": True},
    )
    assert r.status_code == 200
    assert r.json()["current"]["skip_digest_too"] is True


# ─── webhook outputs ────────────────────────────────────────────────────


@pytest.mark.parametrize("block,defaults", [
    ("outputs/discord", {"enabled": False, "min_severity": "high"}),
    ("outputs/ntfy",    {"enabled": False, "server_url": "https://ntfy.sh"}),
    ("outputs/gotify",  {"enabled": False, "server_url": ""}),
])
def test_webhook_output_blocks_return_defaults(client, block, defaults):
    body = client.get(f"/api/settings/{block}").json()
    cur = body["current"]
    for k, v in defaults.items():
        assert cur[k] == v, f"{block}.{k} expected {v!r} got {cur[k]!r}"


def test_patch_discord_webhook_url_round_trip(client, cfg_dir):
    """Webhook URLs are masked secrets — verify PATCH still writes overlay."""
    from homelabsage.config_overlay import user_overlay_path

    client.patch(
        "/api/settings/outputs/discord",
        json={"enabled": True, "webhook_url": "https://discord/hook/abc"},
    )
    overlay = yaml.safe_load(
        user_overlay_path(cfg_dir / "config.yaml").read_text()
    )
    assert overlay["outputs"]["discord"]["enabled"] is True
    assert overlay["outputs"]["discord"]["webhook_url"] == "https://discord/hook/abc"


# ─── github_watched (sources) ───────────────────────────────────────────


def test_get_github_watched_default_disabled(client):
    body = client.get("/api/settings/sources/github_watched").json()
    assert body["current"] == {"enabled": False}


def test_patch_github_watched_enable(client):
    r = client.patch(
        "/api/settings/sources/github_watched",
        json={"enabled": True},
    )
    assert r.status_code == 200
    assert r.json()["current"]["enabled"] is True


# ─── new fields on existing docker block ────────────────────────────────


def test_docker_block_exposes_v030_fields(client):
    """cve_scan + compose_scan_paths + image_size_growth_* are new in v0.3."""
    body = client.get("/api/settings/sources/docker").json()
    schema_props = body["schema"]["properties"]
    for new_field in (
        "cve_scan",
        "compose_scan_paths",
        "image_size_growth_detect",
        "image_size_growth_ratio",
    ):
        assert new_field in schema_props, f"docker schema missing {new_field}"


def test_patch_docker_compose_scan_paths(client):
    r = client.patch(
        "/api/settings/sources/docker",
        json={"compose_scan_paths": ["/mnt/user/appdata/dockge/stacks"]},
    )
    assert r.status_code == 200
    cur = r.json()["current"]
    assert cur["compose_scan_paths"] == ["/mnt/user/appdata/dockge/stacks"]
