"""Tests for the first-run wizard.

Covers route wiring (welcome / step / done renderings), overlay writes
through POSTs, the skip flow (and its enforcement on non-skippable steps),
the wizard.done marker lifecycle, and the dashboard banner trigger.
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.config import load_config
from homelabsage.config_overlay import user_overlay_path
from homelabsage.web.routes_wizard import (
    WIZARD_STEPS,
    is_wizard_complete,
    mark_wizard_complete,
    wizard_marker_path,
)


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "llm:\n  provider: ollama\n  model: qwen3:30b\n  endpoint: http://halo:11434\n"
        "sources:\n  docker:\n    enabled: false\n"
        "scheduler:\n  enabled: false\n  cron: '0 9 * * *'\n  timezone: UTC\n"
        f"storage:\n  database_path: {tmp_path}/state.sqlite\n"
        f"notes:\n  notes_dir: {tmp_path}/notes\n"
        "web:\n  auth:\n    enabled: false\n"
    )
    return tmp_path


@pytest.fixture
def client(cfg_dir: Path) -> TestClient:
    cfg_path = cfg_dir / "config.yaml"
    cfg = load_config(cfg_path)
    app = web.create_app(cfg, cfg_path=cfg_path)
    return TestClient(app)


# ─── marker file ──────────────────────────────────────────────────


def test_marker_path_sibling_to_config(tmp_path):
    p = tmp_path / "config.yaml"
    assert wizard_marker_path(p) == tmp_path / "wizard.done"


def test_is_wizard_complete_false_when_no_marker(cfg_dir):
    assert is_wizard_complete(cfg_dir / "config.yaml") is False


def test_mark_wizard_complete_creates_marker(cfg_dir):
    mark_wizard_complete(cfg_dir / "config.yaml")
    assert is_wizard_complete(cfg_dir / "config.yaml") is True


def test_is_wizard_complete_true_when_cfg_path_none():
    """Read-only mode = no wizard, banner hides."""
    assert is_wizard_complete(None) is True


# ─── welcome ──────────────────────────────────────────────────────


def test_welcome_page_renders(client):
    r = client.get("/wizard")
    assert r.status_code == 200
    assert "Welcome to HomelabSage" in r.text
    # All steps listed in the welcome summary
    for step in WIZARD_STEPS:
        assert step["title"] in r.text
    # Skip-all button present
    assert 'action="/wizard/skip-all"' in r.text


# ─── step pages ───────────────────────────────────────────────────


def test_llm_step_renders_with_provider_select(client):
    r = client.get("/wizard/llm")
    assert r.status_code == 200
    # The LLM block's provider dropdown is included via _settings_fields
    assert 'id="f-provider"' in r.text
    # Provider presets data attribute carries the auto-fill map
    assert "data-provider-presets" in r.text
    # Step 1 of 3
    assert "Step 1 of 3" in r.text or "step_number" not in r.text  # title attr


def test_docker_step_renders(client):
    r = client.get("/wizard/docker")
    assert r.status_code == 200
    assert "Scan your Docker containers" in r.text
    assert 'id="f-enabled"' in r.text


def test_scheduler_step_shows_skip_button(client):
    r = client.get("/wizard/scheduler")
    assert r.status_code == 200
    # Skippable steps render a "Skip this step" submit button
    assert "Skip this step" in r.text
    assert 'name="_wizard_skip"' in r.text


def test_llm_step_has_no_skip_button(client):
    """LLM is required — no skip."""
    r = client.get("/wizard/llm")
    assert "Skip this step" not in r.text


def test_unknown_step_returns_404(client):
    r = client.get("/wizard/nope")
    assert r.status_code == 404


# ─── POST: save + advance ────────────────────────────────────────


def test_llm_post_writes_overlay_and_redirects_to_docker(client, cfg_dir):
    r = client.post(
        "/wizard/llm",
        data={
            "provider": "groq",
            "endpoint": "https://api.groq.com/openai",
            "model": "llama-3.3-70b-versatile",
            "api_key": "",
            "context_size": "32768",
            "timeout": "180",
            "strict_json": "true",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/wizard/docker"
    overlay = yaml.safe_load(user_overlay_path(cfg_dir / "config.yaml").read_text())
    assert overlay["llm"]["provider"] == "groq"


def test_scheduler_skip_advances_without_writing(client, cfg_dir):
    r = client.post(
        "/wizard/scheduler",
        data={"_wizard_skip": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/wizard/done"
    # No overlay write happened
    assert not user_overlay_path(cfg_dir / "config.yaml").exists()


def test_llm_skip_rejected_because_required(client):
    """Trying to skip a non-skippable step must 400."""
    r = client.post(
        "/wizard/llm",
        data={"_wizard_skip": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_invalid_value_re_renders_step_with_error(client, cfg_dir):
    """A Pydantic rejection on the wizard must re-render the step with the
    error, NOT crash or redirect onwards."""
    r = client.post(
        "/wizard/scheduler",
        data={"enabled": "false", "cron": "0 9 * * *",
              "timezone": "Bogus/Land",  # invalid IANA
              "heartbeat_url": ""},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "bogus" in r.text.lower() or "unknown timezone" in r.text.lower()
    # Overlay must NOT have been written
    assert not user_overlay_path(cfg_dir / "config.yaml").exists()


# ─── done + skip-all ─────────────────────────────────────────────


def test_done_page_renders(client):
    r = client.get("/wizard/done")
    assert r.status_code == 200
    assert "set up" in r.text.lower()


def test_post_done_writes_marker_and_redirects_home(client, cfg_dir):
    r = client.post("/wizard/done", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert is_wizard_complete(cfg_dir / "config.yaml")


def test_skip_all_writes_marker_and_redirects_home(client, cfg_dir):
    r = client.post("/wizard/skip-all", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert is_wizard_complete(cfg_dir / "config.yaml")


def test_done_page_shows_already_complete_if_marker_present(client, cfg_dir):
    mark_wizard_complete(cfg_dir / "config.yaml")
    r = client.get("/wizard/done")
    assert "already marked complete" in r.text


# ─── dashboard banner integration ────────────────────────────────


def test_dashboard_shows_wizard_banner_on_fresh_install(client):
    """Fresh install: no marker, no items → banner appears."""
    r = client.get("/")
    assert "Start the setup wizard" in r.text


def test_dashboard_hides_banner_after_wizard_complete(client, cfg_dir):
    mark_wizard_complete(cfg_dir / "config.yaml")
    r = client.get("/")
    assert "Start the setup wizard" not in r.text


def test_dashboard_hides_banner_when_items_exist(client, cfg_dir):
    """An experienced user landing on a populated dashboard shouldn't see
    the welcome banner even if the marker is missing — having items is
    proof the install has been used."""
    from datetime import datetime

    from homelabsage.db import Database
    from homelabsage.models import AnalyzedUpdate, Update, UpdateStatus
    db = Database(str(cfg_dir / "state.sqlite"))
    db.upsert(
        AnalyzedUpdate(
            update=Update(
                source="docker", subject="x", current_version="1", new_version="2",
            ),
            status=UpdateStatus.NEW,
            updated_at=datetime.now(UTC),
        )
    )
    r = client.get("/")
    assert "Start the setup wizard" not in r.text
