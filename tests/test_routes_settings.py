"""End-to-end tests for `/api/settings/*`.

Real FastAPI TestClient against a real on-disk `config.yaml` + overlay so we
exercise the full GET → PATCH → DELETE round-trip plus the Pydantic
validation gate on every write.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.config import load_config
from homelabsage.config_overlay import user_overlay_path


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    """A scratch directory with a minimal `config.yaml` ready for editing."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "llm:\n"
        "  provider: openai\n"
        "  model: Qwen3.6-35B-Abl\n"
        "  endpoint: http://halo:11434\n"
        "  api_key: deploy-default-secret\n"
        "sources:\n"
        "  docker:\n"
        "    enabled: true\n"
        "    skip:\n"
        "      - foo\n"
        "scheduler:\n"
        "  enabled: false\n"
        "storage:\n"
        f"  database_path: {tmp_path}/state.sqlite\n"
        "notes:\n"
        f"  notes_dir: {tmp_path}/notes\n"
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


# ─── GET ──────────────────────────────────────────────────────────────────

def test_index_lists_known_blocks(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    block_ids = [b["id"] for b in body["blocks"]]
    assert "llm" in block_ids
    assert "sources/docker" in block_ids
    assert "outputs/notion" in block_ids
    assert body["overlay_writable"] is True
    assert body["overrides"] == []  # fresh — no overlay yet


def test_get_llm_returns_current_with_secret_masked(client):
    r = client.get("/api/settings/llm")
    assert r.status_code == 200
    body = r.json()
    assert body["current"]["model"] == "Qwen3.6-35B-Abl"
    # api_key is a secret-shaped field; must be masked even when populated
    assert body["current"]["api_key"] == "***"
    # No overlay yet
    assert body["overrides"] == []


def test_get_returns_schema(client):
    r = client.get("/api/settings/llm")
    schema = r.json()["schema"]
    # JSONSchema shape from Pydantic v2
    assert "properties" in schema
    assert "model" in schema["properties"]


def test_get_unknown_block_404(client):
    r = client.get("/api/settings/nosuchthing")
    assert r.status_code == 404


def test_get_nested_block(client):
    r = client.get("/api/settings/sources/docker")
    assert r.status_code == 200
    assert r.json()["current"]["enabled"] is True


# ─── PATCH ─────────────────────────────────────────────────────────────────

def test_patch_writes_overlay_and_reflects_in_get(client, cfg_dir):
    r = client.patch("/api/settings/llm", json={"model": "Qwen3.5-4B-Compact"})
    assert r.status_code == 200
    body = r.json()
    assert body["current"]["model"] == "Qwen3.5-4B-Compact"
    assert "model" in body["overrides"]

    # The overlay file must exist on disk in the conventional location.
    overlay_path = user_overlay_path(cfg_dir / "config.yaml")
    assert overlay_path.exists()
    on_disk = yaml.safe_load(overlay_path.read_text())
    assert on_disk == {"llm": {"model": "Qwen3.5-4B-Compact"}}


def test_patch_preserves_other_overrides(client):
    client.patch("/api/settings/llm", json={"model": "Qwen3.5-4B-Compact"})
    client.patch("/api/settings/llm", json={"endpoint": "http://newhost:11434"})

    r = client.get("/api/settings/llm")
    body = r.json()
    assert body["current"]["model"] == "Qwen3.5-4B-Compact"
    assert body["current"]["endpoint"] == "http://newhost:11434"
    assert set(body["overrides"]) == {"model", "endpoint"}


def test_patch_invalid_value_returns_400(client):
    """Pydantic validation must run on the *merged* config before the overlay
    is written; an invalid type rejects the write and leaves the file alone."""
    r = client.patch(
        "/api/settings/scheduler", json={"enabled": "not-a-bool-or-similar"}
    )
    assert r.status_code == 400
    # And no overlay file was created
    r2 = client.get("/api/settings")
    assert r2.json()["overrides"] == []


def test_patch_nested_block(client, cfg_dir):
    r = client.patch(
        "/api/settings/sources/docker",
        json={"orphan_min_days": 14},
    )
    assert r.status_code == 200
    overlay = yaml.safe_load(
        user_overlay_path(cfg_dir / "config.yaml").read_text()
    )
    assert overlay == {"sources": {"docker": {"orphan_min_days": 14}}}


def test_patch_secret_field_persists_real_value(client, cfg_dir):
    """The wire-level masking is for GET only — PATCHing a secret must
    persist the actual value to the overlay."""
    r = client.patch("/api/settings/llm", json={"api_key": "sk-NEW-KEY-1234567890"})
    assert r.status_code == 200
    overlay = yaml.safe_load(
        user_overlay_path(cfg_dir / "config.yaml").read_text()
    )
    assert overlay["llm"]["api_key"] == "sk-NEW-KEY-1234567890"
    # Subsequent GET still masks it though.
    body = client.get("/api/settings/llm").json()
    assert body["current"]["api_key"] == "***"


def test_patch_body_must_be_dict(client):
    r = client.patch("/api/settings/llm", json=["not", "a", "dict"])
    assert r.status_code in (400, 422)  # FastAPI rejects at parse time


# ─── DELETE ────────────────────────────────────────────────────────────────

def test_delete_single_key_reverts_to_default(client):
    client.patch("/api/settings/llm", json={"model": "Qwen3.5-4B-Compact"})
    # Now revert just that one key.
    r = client.delete("/api/settings/llm/model")
    assert r.status_code == 200
    body = client.get("/api/settings/llm").json()
    assert body["current"]["model"] == "Qwen3.6-35B-Abl"  # back to deploy default
    assert "model" not in body["overrides"]


def test_delete_whole_block_reverts_everything(client):
    client.patch("/api/settings/llm", json={"model": "Q4", "endpoint": "http://new"})
    r = client.delete("/api/settings/llm")
    assert r.status_code == 200
    body = client.get("/api/settings/llm").json()
    assert body["current"]["model"] == "Qwen3.6-35B-Abl"
    assert body["current"]["endpoint"] == "http://halo:11434"
    assert body["overrides"] == []


def test_delete_missing_key_does_not_error(client):
    """Idempotent revert — DELETE of a key that isn't in the overlay returns 200."""
    r = client.delete("/api/settings/llm/model")
    assert r.status_code == 200


def test_delete_unknown_block_404(client):
    r = client.delete("/api/settings/nosuchblock")
    assert r.status_code == 404


def test_delete_nested_block_key(client):
    client.patch("/api/settings/sources/docker", json={"orphan_min_days": 14})
    r = client.delete("/api/settings/sources/docker/orphan_min_days")
    assert r.status_code == 200
    body = client.get("/api/settings/sources/docker").json()
    # 30 is the documented default in DockerSourceConfig
    assert body["current"]["orphan_min_days"] == 30


# ─── read-only mode (cfg_path is None) ────────────────────────────────────

def test_read_only_mode_rejects_writes(tmp_path: Path):
    """Tests / programmatic callers can use create_app without a cfg_path;
    GETs still work, but PATCH/DELETE return 503."""
    cfg_yaml = tmp_path / "config.yaml"
    cfg_yaml.write_text(
        "llm:\n  model: x\n"
        f"storage:\n  database_path: {tmp_path}/state.sqlite\n"
        f"notes:\n  notes_dir: {tmp_path}/notes\n"
        "scheduler:\n  enabled: false\n"
    )
    cfg = load_config(cfg_yaml)
    app = web.create_app(cfg, cfg_path=None)
    c = TestClient(app)

    r = c.get("/api/settings/llm")
    assert r.status_code == 200

    r2 = c.patch("/api/settings/llm", json={"model": "y"})
    assert r2.status_code == 503

    r3 = c.delete("/api/settings/llm/model")
    assert r3.status_code == 503
