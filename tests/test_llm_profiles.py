"""End-to-end tests for multi-LLM profiles.

Covers:
  - Data model (`Config.llm_profiles` + `llm_active`) + the resolver helper.
  - LLMClient hot-reload via the callable provider.
  - Engine resolution reading from disk each LLM call.
  - HTML routes: list / add / edit / activate / delete.
  - Dashboard `/llm/active` switch endpoint.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.config import (
    Config,
    LLMConfig,
    get_active_llm_config,
    load_config,
)
from homelabsage.config_overlay import user_overlay_path
from homelabsage.llm import LLMClient

# ─── resolver helper ──────────────────────────────────────────────────────

def test_resolver_uses_active_profile():
    cfg = Config(
        llm=LLMConfig(model="legacy"),
        llm_profiles={
            "halo": LLMConfig(model="Q35"),
            "groq": LLMConfig(model="llama3"),
        },
        llm_active="groq",
    )
    assert get_active_llm_config(cfg).model == "llama3"


def test_resolver_falls_back_to_legacy_when_active_empty():
    cfg = Config(
        llm=LLMConfig(model="legacy"),
        llm_profiles={"halo": LLMConfig(model="Q35")},
        llm_active="",
    )
    assert get_active_llm_config(cfg).model == "legacy"


def test_resolver_falls_back_when_active_does_not_exist():
    """Permissive: a typo in `llm_active` silently falls back rather than
    failing the run. The dashboard surfaces the effective name so the
    user notices."""
    cfg = Config(
        llm=LLMConfig(model="legacy"),
        llm_profiles={"halo": LLMConfig(model="Q35")},
        llm_active="typo",
    )
    assert get_active_llm_config(cfg).model == "legacy"


# ─── LLMClient hot-reload ────────────────────────────────────────────────

def test_llm_client_accepts_static_config_for_backward_compat():
    """Tests + simple scripts pass a fixed LLMConfig directly."""
    cfg = LLMConfig(model="X", provider="openai")
    client = LLMClient(cfg)
    assert client.cfg.model == "X"


def test_llm_client_resolves_via_callable_provider():
    """Engine wires a callable so changes to active profile take effect mid-run."""
    state = {"current": LLMConfig(model="A")}
    client = LLMClient(lambda: state["current"])
    assert client.cfg.model == "A"
    state["current"] = LLMConfig(model="B")
    assert client.cfg.model == "B"


# ─── load_config integration ─────────────────────────────────────────────

def test_load_config_picks_up_profiles_from_overlay(tmp_path: Path):
    base = tmp_path / "config.yaml"
    base.write_text("llm:\n  model: Z\n")
    (tmp_path / "config.user.yaml").write_text(
        "llm_active: halo\n"
        "llm_profiles:\n"
        "  halo:\n"
        "    provider: openai\n"
        "    model: Q35\n"
        "  groq:\n"
        "    provider: openai\n"
        "    model: llama3\n"
    )
    cfg = load_config(base)
    assert set(cfg.llm_profiles.keys()) == {"halo", "groq"}
    assert cfg.llm_active == "halo"
    assert get_active_llm_config(cfg).model == "Q35"


# ─── HTML routes ─────────────────────────────────────────────────────────

@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "llm:\n"
        "  provider: openai\n"
        "  model: legacy\n"
        "  endpoint: http://legacy:11434\n"
        f"storage: {{database_path: {tmp_path}/state.sqlite}}\n"
        f"notes: {{notes_dir: {tmp_path}/notes}}\n"
        "scheduler: {enabled: false}\n"
        "web: {auth: {enabled: false}}\n"
    )
    return tmp_path


@pytest.fixture
def client(cfg_dir: Path) -> TestClient:
    cfg_path = cfg_dir / "config.yaml"
    return TestClient(web.create_app(load_config(cfg_path), cfg_path=cfg_path))


def _origin_header(c: TestClient) -> dict[str, str]:
    """CSRF guard requires Origin to match Host. TestClient uses `testserver`."""
    return {"Origin": "http://testserver"}


def test_profiles_index_renders_empty(client):
    r = client.get("/settings/llm/profiles")
    assert r.status_code == 200
    assert "No profiles yet" in r.text


def test_add_profile_writes_overlay(client, cfg_dir):
    r = client.post(
        "/settings/llm/profiles/save",
        data={
            "__profile_name": "halo",
            "provider": "openai",
            "endpoint": "http://halo:11434",
            "model": "Q35",
            "context_size": "32768",
            "api_key": "",
            "timeout": "180",
            "strict_json": "true",
        },
        headers=_origin_header(client),
        follow_redirects=False,
    )
    # Either an HTMX-style re-render (200) or a redirect (303) — both fine
    assert r.status_code in (200, 303)
    overlay = yaml.safe_load(
        user_overlay_path(cfg_dir / "config.yaml").read_text()
    )
    assert overlay["llm_profiles"]["halo"]["model"] == "Q35"
    assert overlay["llm_profiles"]["halo"]["endpoint"] == "http://halo:11434"


def test_add_profile_rejects_bad_name(client):
    r = client.post(
        "/settings/llm/profiles/save",
        data={"__profile_name": "has spaces!", "provider": "openai", "model": "x",
              "endpoint": "http://x", "context_size": "32768",
              "timeout": "180", "strict_json": "true"},
        headers=_origin_header(client),
    )
    # Should re-render with an error, not crash
    assert "only letters, digits" in r.text.lower() or "name" in r.text.lower()


def test_activate_profile_writes_llm_active(client, cfg_dir):
    # First add a profile
    client.post(
        "/settings/llm/profiles/save",
        data={"__profile_name": "halo", "provider": "openai",
              "endpoint": "http://halo:11434", "model": "Q35",
              "context_size": "32768", "timeout": "180", "strict_json": "true"},
        headers=_origin_header(client),
    )
    # Now activate it
    r = client.post(
        "/settings/llm/profiles/halo/activate",
        headers=_origin_header(client),
    )
    assert r.status_code == 200
    overlay = yaml.safe_load(
        user_overlay_path(cfg_dir / "config.yaml").read_text()
    )
    assert overlay["llm_active"] == "halo"


def test_delete_profile_removes_it_and_clears_active(client, cfg_dir):
    client.post(
        "/settings/llm/profiles/save",
        data={"__profile_name": "halo", "provider": "openai",
              "endpoint": "http://halo:11434", "model": "Q35",
              "context_size": "32768", "timeout": "180", "strict_json": "true"},
        headers=_origin_header(client),
    )
    client.post(
        "/settings/llm/profiles/halo/activate",
        headers=_origin_header(client),
    )
    r = client.post(
        "/settings/llm/profiles/halo/delete",
        headers=_origin_header(client),
    )
    assert r.status_code == 200
    overlay = yaml.safe_load(
        user_overlay_path(cfg_dir / "config.yaml").read_text()
    )
    assert "halo" not in overlay.get("llm_profiles", {})
    # Active pointer must also be cleared so the engine doesn't carry a
    # dangling reference; fall back to legacy `llm`.
    assert overlay.get("llm_active", "") == ""


def test_dashboard_switch_changes_active(client, cfg_dir):
    """The dashboard <select> hits /llm/active with form-encoded `active`."""
    client.post(
        "/settings/llm/profiles/save",
        data={"__profile_name": "groq", "provider": "openai",
              "endpoint": "https://api.groq.com/openai", "model": "llama3",
              "context_size": "32768", "timeout": "180", "strict_json": "true"},
        headers=_origin_header(client),
    )
    r = client.post(
        "/llm/active",
        data={"active": "groq"},
        headers={**_origin_header(client), "HX-Request": "true"},
    )
    assert r.status_code == 200
    overlay = yaml.safe_load(
        user_overlay_path(cfg_dir / "config.yaml").read_text()
    )
    assert overlay["llm_active"] == "groq"
    # The HTMX fragment confirms the switch label
    assert "groq" in r.text


def test_dashboard_switch_to_empty_clears_active(client, cfg_dir):
    # First set an active profile, then switch back to "(legacy llm)" via empty.
    client.post(
        "/settings/llm/profiles/save",
        data={"__profile_name": "halo", "provider": "openai",
              "endpoint": "http://halo:11434", "model": "Q35",
              "context_size": "32768", "timeout": "180", "strict_json": "true"},
        headers=_origin_header(client),
    )
    client.post("/llm/active", data={"active": "halo"}, headers=_origin_header(client))
    client.post("/llm/active", data={"active": ""}, headers=_origin_header(client))
    overlay = yaml.safe_load(
        user_overlay_path(cfg_dir / "config.yaml").read_text()
    )
    assert overlay["llm_active"] == ""


def test_dashboard_rejects_unknown_profile(client):
    r = client.post(
        "/llm/active",
        data={"active": "nonexistent"},
        headers={**_origin_header(client), "HX-Request": "true"},
    )
    assert r.status_code == 400


def test_index_renders_profile_dropdown(client):
    client.post(
        "/settings/llm/profiles/save",
        data={"__profile_name": "halo", "provider": "openai",
              "endpoint": "http://halo:11434", "model": "Q35",
              "context_size": "32768", "timeout": "180", "strict_json": "true"},
        headers=_origin_header(client),
    )
    r = client.get("/")
    assert "Active LLM" in r.text
    assert "halo" in r.text
    assert "/llm/active" in r.text


def test_index_no_dropdown_when_no_profiles(client):
    r = client.get("/")
    assert "Active LLM" not in r.text


# ─── Engine reads the active profile on every LLM call ────────────────────

def test_engine_hot_reload_picks_new_active(cfg_dir, monkeypatch):
    """End-to-end-ish: write a new active to the overlay, the engine's
    LLMClient.cfg reads it on next access."""
    from homelabsage.db import Database
    from homelabsage.engine import Engine

    cfg_path = cfg_dir / "config.yaml"
    (cfg_dir / "config.user.yaml").write_text(
        "llm_profiles:\n"
        "  halo:\n"
        "    provider: openai\n"
        "    model: Q35\n"
        "    endpoint: http://halo:11434\n"
        "  groq:\n"
        "    provider: openai\n"
        "    model: llama3\n"
        "    endpoint: https://api.groq.com/openai\n"
        "llm_active: halo\n"
    )
    cfg = load_config(cfg_path)
    db = Database(cfg.storage.database_path)
    try:
        engine = Engine(cfg, db, cfg_path=cfg_path)
        assert engine.llm.cfg.model == "Q35"

        # Rewrite overlay to flip active to groq (simulating the dashboard
        # `/llm/active` write) and re-read.
        (cfg_dir / "config.user.yaml").write_text(
            "llm_profiles:\n"
            "  halo:\n"
            "    provider: openai\n"
            "    model: Q35\n"
            "    endpoint: http://halo:11434\n"
            "  groq:\n"
            "    provider: openai\n"
            "    model: llama3\n"
            "    endpoint: https://api.groq.com/openai\n"
            "llm_active: groq\n"
        )
        assert engine.llm.cfg.model == "llama3"
    finally:
        db.close()


def test_engine_resolver_swallows_bad_overlay_read(cfg_dir):
    """If config.user.yaml goes briefly unreadable, fall back to startup cfg
    rather than crashing the next LLM call."""
    from homelabsage.db import Database
    from homelabsage.engine import Engine

    cfg_path = cfg_dir / "config.yaml"
    cfg = load_config(cfg_path)
    db = Database(cfg.storage.database_path)
    try:
        engine = Engine(cfg, db, cfg_path=cfg_path)
        # Write garbage; load_config will fail or load_overlay will log+ignore.
        (cfg_dir / "config.user.yaml").write_text(":::not yaml")
        # Should not raise — falls back through `pass` in the resolver
        _ = engine.llm.cfg
    finally:
        db.close()
