"""End-to-end tests for the HTML settings UI.

Exercises GET (index + form), POST update (form-encoded), POST revert
(block + key), HTMX partial vs full-page, secret handling, and the
type-coercion edge cases.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.config import load_config
from homelabsage.config_overlay import user_overlay_path
from homelabsage.web.routes_settings_html import (
    _coerce_form_value,
    _form_to_block_patch,
)


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "llm:\n"
        "  provider: openai\n"
        "  model: Qwen3.6-35B-Abl\n"
        "  endpoint: http://halo:11434\n"
        "  api_key: deploy-default-secret\n"
        "  timeout: 240\n"
        "  strict_json: true\n"
        "sources:\n"
        "  docker:\n"
        "    enabled: true\n"
        "    skip:\n"
        "      - foo\n"
        "      - bar\n"
        "    overrides:\n"
        "      Forgejo: go-gitea/gitea\n"
        "    orphan_min_days: 30\n"
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


# ─── type coercion (pure unit) ────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("true", True), ("True", True), ("on", True), ("1", True), ("yes", True),
    ("false", False), ("off", False), ("0", False), ("", False), ("nope", False),
])
def test_coerce_boolean(raw, expected):
    assert _coerce_form_value(raw, {"type": "boolean"}) is expected


def test_coerce_integer():
    assert _coerce_form_value("42", {"type": "integer"}) == 42


def test_coerce_integer_rejects_garbage():
    with pytest.raises(ValueError):
        _coerce_form_value("not a number", {"type": "integer"})


def test_coerce_array_one_per_line():
    out = _coerce_form_value("foo\nbar\n\n  baz  \n", {"type": "array"})
    assert out == ["foo", "bar", "baz"]


def test_coerce_array_empty():
    assert _coerce_form_value("", {"type": "array"}) == []


def test_coerce_object_yaml_mapping():
    out = _coerce_form_value("Forgejo: go-gitea/gitea\nFoo: bar/baz",
                             {"type": "object"})
    assert out == {"Forgejo": "go-gitea/gitea", "Foo": "bar/baz"}


def test_coerce_object_rejects_non_mapping():
    with pytest.raises(ValueError):
        _coerce_form_value("- 1\n- 2", {"type": "object"})


def test_coerce_falls_back_to_string_for_unknown_type():
    assert _coerce_form_value("anything", {}) == "anything"


def test_coerce_handles_optional_anyof():
    """Pydantic emits anyOf:[{type:X},{type:null}] for Optional[X]."""
    schema = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    assert _coerce_form_value("99", schema) == 99


# ─── form → patch transformation ──────────────────────────────────────────

_LLM_SCHEMA_FRAG = {
    "properties": {
        "model": {"type": "string"},
        "timeout": {"type": "integer"},
        "strict_json": {"type": "boolean"},
        "api_key": {"type": "string"},
    }
}


def test_patch_drops_unchanged_fields():
    """Round-tripping current values must not bloat the overlay."""
    current = {"model": "X", "timeout": 240, "strict_json": True, "api_key": ""}
    form = {"model": "X", "timeout": "240", "strict_json": "true"}
    # api_key empty + already empty → secret-skip handles it
    out = _form_to_block_patch(form, _LLM_SCHEMA_FRAG, current)
    assert out == {}


def test_patch_includes_changed_fields():
    current = {"model": "X", "timeout": 240, "strict_json": True}
    form = {"model": "Y", "timeout": "120", "strict_json": "true"}
    out = _form_to_block_patch(form, _LLM_SCHEMA_FRAG, current)
    assert out == {"model": "Y", "timeout": 120}


def test_patch_secret_empty_is_skipped():
    """The whole point: blank secret form ≠ "wipe the existing secret"."""
    current = {"api_key": "***"}
    form = {"api_key": ""}
    out = _form_to_block_patch(form, _LLM_SCHEMA_FRAG, current)
    assert "api_key" not in out


def test_patch_secret_nonempty_is_persisted():
    current = {"api_key": "***"}
    form = {"api_key": "sk-NEW"}
    out = _form_to_block_patch(form, _LLM_SCHEMA_FRAG, current)
    assert out["api_key"] == "sk-NEW"


def test_patch_missing_checkbox_becomes_false():
    """Unchecked HTML checkbox doesn't even appear in the form data."""
    current = {"strict_json": True}
    form: dict = {}  # strict_json absent
    out = _form_to_block_patch(form, _LLM_SCHEMA_FRAG, current)
    assert out == {"strict_json": False}


# ─── HTTP: GET ────────────────────────────────────────────────────────────

def test_index_lists_blocks(client):
    r = client.get("/settings")
    assert r.status_code == 200
    assert "LLMConfig" in r.text
    assert "DockerSourceConfig" in r.text
    assert "/settings/llm" in r.text
    assert "/settings/sources/docker" in r.text


def test_index_shows_overlay_warning_in_readonly(tmp_path: Path):
    cfg_yaml = tmp_path / "config.yaml"
    cfg_yaml.write_text(
        "llm:\n  model: x\n"
        f"storage:\n  database_path: {tmp_path}/state.sqlite\n"
        f"notes:\n  notes_dir: {tmp_path}/notes\n"
        "scheduler:\n  enabled: false\n"
    )
    app = web.create_app(load_config(cfg_yaml), cfg_path=None)
    c = TestClient(app)
    r = c.get("/settings")
    assert "read-only" in r.text


def test_block_form_renders_each_field_type(client):
    r = client.get("/settings/llm")
    assert r.status_code == 200
    html = r.text
    # text input for model
    assert 'id="f-model"' in html and 'type="text"' in html
    # number input for timeout
    assert 'id="f-timeout"' in html and 'type="number"' in html
    # checkbox for strict_json (bool)
    assert 'id="f-strict_json"' in html and 'type="checkbox"' in html
    # password (masked) for api_key
    assert 'id="f-api_key"' in html and 'type="password"' in html
    # No real secret leaks into the rendered HTML
    assert "deploy-default-secret" not in html


def test_block_form_renders_array_as_textarea(client):
    r = client.get("/settings/sources/docker")
    html = r.text
    assert 'id="f-skip"' in html
    assert '<textarea' in html
    # Each list item rendered on its own line in the textarea
    assert "foo" in html and "bar" in html


def test_enum_field_renders_as_select(client):
    """Pydantic Literal[...] fields produce `enum:[...]` in the JSON schema;
    the renderer picks a <select> over a free text input."""
    r = client.get("/settings/llm")
    html = r.text
    # provider is Literal[ollama, openai, groq, gemini, openrouter, anthropic, disabled]
    assert 'id="f-provider"' in html
    # Should be a <select> not a text input
    assert "<select" in html
    for opt in ("ollama", "openai", "groq", "gemini", "openrouter", "anthropic", "disabled"):
        assert f'<option value="{opt}"' in html
    # The current value must be marked selected
    assert 'value="openai"' in html and "selected" in html


def test_provider_select_carries_presets_for_autofill(client):
    """The LLM block exposes the preset map on `data-provider-presets` so
    the inline script can auto-fill endpoint+model on provider change.

    Critical detail: the attribute MUST be single-quoted because Jinja's
    `tojson` filter escapes `'` but not `"` (JSON delimiters). If we used
    double quotes the JSON's `"` would break the attribute, the browser
    would silently truncate `data-provider-presets` to `{`, JSON.parse
    would throw, and the auto-fill would do nothing. Tested here so we
    don't regress that.
    """
    r = client.get("/settings/llm")
    html = r.text
    # Single-quoted attribute opener — the regression we just fixed.
    assert "data-provider-presets='" in html, (
        "data-provider-presets must use single-quoted attribute value; "
        "double quotes break the embedded JSON's `\"` delimiters."
    )
    # Spot-check a few preset URLs are embedded
    assert "api.groq.com" in html
    assert "generativelanguage.googleapis.com" in html
    assert "localhost:11434" in html
    # And the JSON itself must round-trip: extract the attribute value and
    # parse it. If this works, the browser's JSON.parse will too.
    import json as _json
    import re as _re
    m = _re.search(r"data-provider-presets='([^']+)'", html)
    assert m is not None, "data-provider-presets attribute not found"
    parsed = _json.loads(m.group(1))
    assert "groq" in parsed
    assert parsed["groq"]["endpoint"] == "https://api.groq.com/openai"


def test_other_blocks_dont_get_provider_presets(client):
    """Only the LLM block should expose the preset map. Other blocks must
    NOT pick it up just because they happen to have a `provider`-named
    field in the future."""
    r = client.get("/settings/outputs/notion")
    assert "data-provider-presets=" not in r.text


def test_new_providers_accept_at_save(client, cfg_dir):
    """The widened enum (groq/gemini/openrouter) must round-trip via the form."""
    r = client.post(
        "/settings/llm/update",
        data={
            "provider": "groq",
            "endpoint": "https://api.groq.com/openai",
            "model": "llama-3.3-70b-versatile",
            "api_key": "",
            "context_size": "32768",
            "timeout": "180",
            "strict_json": "true",
        },
    )
    assert r.status_code == 200
    from homelabsage.config_overlay import user_overlay_path
    overlay = yaml.safe_load(user_overlay_path(cfg_dir / "config.yaml").read_text())
    assert overlay["llm"]["provider"] == "groq"
    assert overlay["llm"]["model"] == "llama-3.3-70b-versatile"


def test_enum_renders_for_notion_write_policy(client):
    r = client.get("/settings/outputs/notion")
    html = r.text
    assert 'id="f-write_policy"' in html
    assert '<option value="always"' in html
    assert '<option value="only_action_required"' in html


def test_enum_rejects_invalid_value_at_save(client, cfg_dir):
    """Pydantic Literal validation catches enum violations server-side."""
    r = client.post(
        "/settings/outputs/telegram/update",
        data={"enabled": "false", "bot_token": "", "chat_id": "",
              "min_severity": "BOGUS"},
    )
    assert r.status_code == 200
    assert "min_severity" in r.text.lower() or "input" in r.text.lower()
    # Overlay NOT written
    from homelabsage.config_overlay import user_overlay_path
    overlay = user_overlay_path(cfg_dir / "config.yaml")
    if overlay.exists():
        import yaml
        data = yaml.safe_load(overlay.read_text()) or {}
        assert data.get("outputs", {}).get("telegram", {}).get("min_severity") != "BOGUS"


def test_scheduler_renders_timezone_widget(client):
    """`scheduler.timezone` is annotated `ui_widget='timezone'` and rolls into
    a preset select + raw text input via `_widget_timezone.html`."""
    r = client.get("/settings/scheduler")
    html = r.text
    assert 'class="tz-preset"' in html
    assert 'id="f-timezone"' in html
    # A handful of representative presets must be in the option list
    for tz in ["UTC", "Europe/Madrid", "America/New_York", "Asia/Tokyo"]:
        assert tz in html


def test_scheduler_rejects_invalid_timezone(client, cfg_dir):
    """Server-side validator must catch typos before we write the overlay
    and crash the scheduler later."""
    r = client.post(
        "/settings/scheduler/update",
        data={"enabled": "false", "cron": "0 9 * * *",
              "timezone": "Europe/Madri",  # typo
              "heartbeat_url": ""},
    )
    assert r.status_code == 200
    # The form re-renders with the error visible
    assert "unknown timezone" in r.text.lower() or "europe/madri" in r.text.lower()
    # Nothing written to disk
    from homelabsage.config_overlay import user_overlay_path
    assert not user_overlay_path(cfg_dir / "config.yaml").exists()


def test_scheduler_accepts_valid_iana_timezone(client, cfg_dir):
    r = client.post(
        "/settings/scheduler/update",
        data={"enabled": "false", "cron": "0 9 * * *",
              "timezone": "Asia/Tokyo",
              "heartbeat_url": ""},
    )
    assert r.status_code == 200
    # Overlay was written
    import yaml

    from homelabsage.config_overlay import user_overlay_path
    overlay = yaml.safe_load(
        user_overlay_path(cfg_dir / "config.yaml").read_text()
    )
    assert overlay["scheduler"]["timezone"] == "Asia/Tokyo"


def test_storage_renders_path_widget(client):
    """`storage.database_path` opts into ui_widget='path' → text input plus
    a file/directory-aware helper line."""
    r = client.get("/settings/storage")
    html = r.text
    assert 'id="f-database_path"' in html
    # The path widget shows file/directory-specific helper text
    assert "File path." in html
    # The cfg_dir fixture writes an absolute path, so the "Resolves to:"
    # hint is suppressed (resolved == raw). The relative-path case is
    # covered by test_path_widget_resolves_relative_path.


def test_notes_dir_renders_directory_widget(client):
    """`notes.notes_dir` is opted-in too, with ui_path_kind='directory'."""
    r = client.get("/settings/notes")
    html = r.text
    assert 'id="f-notes_dir"' in html
    assert "Directory path." in html


def test_path_widget_resolves_relative_path(client, cfg_dir):
    """A relative path in the overlay shows the absolute-resolved version
    in a "Resolves to:" hint — the value the user typed stays as-is in the
    input."""
    # Write a relative path into the overlay so the form renders it
    from homelabsage.config_overlay import atomic_write_yaml, user_overlay_path
    atomic_write_yaml(
        user_overlay_path(cfg_dir / "config.yaml"),
        {"curator": {"output_dir": "./curator-notes"}},
    )
    r = client.get("/settings/curator")
    html = r.text
    assert 'id="f-output_dir"' in html
    assert 'value="./curator-notes"' in html
    # Resolution is anchored on the test's CWD (whatever pytest used).
    # We only check that the hint is present and contains the leaf name —
    # the prefix varies per environment but is irrelevant for the assertion.
    assert "Resolves to:" in html
    assert "curator-notes" in html


def test_scheduler_renders_cron_widget(client):
    """The cron field has json_schema_extra={'ui_widget':'cron'} → preset select."""
    r = client.get("/settings/scheduler")
    html = r.text
    assert 'class="cron-preset"' in html
    # The default deploy value flows into the text input
    assert 'id="f-cron"' in html
    # A handful of known presets appear in the select options
    assert "Every 6 hours" in html
    assert "0 9 * * *" in html
    # crontab.guru link is the canonical explainer; we surface it
    assert "crontab.guru" in html


def test_field_descriptions_render_as_help_text(client):
    """Pydantic Field(description=...) lands in the schema, then in the form."""
    r = client.get("/settings/llm")
    # An LLM field has a non-empty description in config.py
    assert "OpenAI-compatible" in r.text or "Bearer token" in r.text


def test_block_form_renders_object_as_yaml_textarea(client):
    r = client.get("/settings/sources/docker")
    html = r.text
    assert 'id="f-overrides"' in html
    assert "Forgejo: go-gitea/gitea" in html


def test_unknown_block_returns_404(client):
    assert client.get("/settings/nosuchthing").status_code == 404


# ─── HTTP: POST update ────────────────────────────────────────────────────

def test_update_writes_overlay_and_redisplays(client, cfg_dir):
    r = client.post(
        "/settings/llm/update",
        data={"model": "Qwen3.5-4B-Compact", "timeout": "180",
              "strict_json": "true", "api_key": ""},
    )
    assert r.status_code == 200
    assert "Saved." in r.text
    overlay = yaml.safe_load(
        user_overlay_path(cfg_dir / "config.yaml").read_text()
    )
    assert overlay == {"llm": {"model": "Qwen3.5-4B-Compact", "timeout": 180}}
    # bool round-trips at default, omitted; api_key empty + secret → skipped


def test_update_returns_partial_for_htmx(client):
    r = client.post(
        "/settings/llm/update",
        data={"model": "Qwen3.5-4B-Compact"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    # Partial doesn't include the page chrome (the <main> wrapper from _base)
    assert "<html" not in r.text.lower()
    assert 'id="block-form"' in r.text


def test_update_full_page_for_no_htmx(client):
    r = client.post(
        "/settings/llm/update",
        data={"model": "Qwen3.5-4B-Compact"},
    )
    # Full page includes the layout chrome
    assert "<html" in r.text.lower()


def test_update_invalid_type_shows_error(client, cfg_dir):
    r = client.post("/settings/llm/update", data={"timeout": "not-a-number"})
    assert r.status_code == 200
    assert "timeout" in r.text and ("invalid" in r.text or "not-a-number" in r.text)
    # Nothing written to disk
    assert not user_overlay_path(cfg_dir / "config.yaml").exists()


def test_update_array_field_replaces_skip_list(client, cfg_dir):
    r = client.post(
        "/settings/sources/docker/update",
        data={"enabled": "true",
              "socket": "/var/run/docker.sock",
              "skip": "alpha\nbeta\ngamma",
              "overrides": "",
              "orphan_min_days": "30",
              "find_alternatives": "false",
              "repo_health": "true"},
    )
    assert r.status_code == 200
    overlay = yaml.safe_load(
        user_overlay_path(cfg_dir / "config.yaml").read_text()
    )
    # `skip` changed; other unchanged fields are pruned
    assert overlay["sources"]["docker"]["skip"] == ["alpha", "beta", "gamma"]


# ─── HTTP: POST revert ────────────────────────────────────────────────────

def test_revert_key(client, cfg_dir):
    client.post("/settings/llm/update", data={"model": "x", "timeout": "9999"})
    r = client.post("/settings/llm/model/revert")
    assert r.status_code == 200
    overlay = yaml.safe_load(
        user_overlay_path(cfg_dir / "config.yaml").read_text()
    )
    # `model` is gone, but `timeout=9999` override survives
    assert "model" not in overlay.get("llm", {})
    assert overlay["llm"]["timeout"] == 9999


def test_revert_whole_block(client, cfg_dir):
    client.post("/settings/llm/update", data={"model": "x", "timeout": "9999"})
    r = client.post("/settings/llm/revert")
    assert r.status_code == 200
    overlay = yaml.safe_load(
        user_overlay_path(cfg_dir / "config.yaml").read_text()
    )
    assert "llm" not in overlay


def test_revert_key_in_nested_block(client, cfg_dir):
    client.post(
        "/settings/sources/docker/update",
        data={"enabled": "true", "socket": "/var/run/docker.sock",
              "skip": "x", "overrides": "", "orphan_min_days": "14",
              "find_alternatives": "false", "repo_health": "true"},
    )
    r = client.post("/settings/sources/docker/orphan_min_days/revert")
    assert r.status_code == 200
    overlay = yaml.safe_load(
        user_overlay_path(cfg_dir / "config.yaml").read_text()
    )
    # The overlay still has the `skip` override but no longer the threshold one
    assert "orphan_min_days" not in overlay.get("sources", {}).get("docker", {})


# ─── static asset mount ───────────────────────────────────────────────────

def test_htmx_is_served_from_static(client):
    r = client.get("/static/htmx.min.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript") \
           or r.headers["content-type"].startswith("application/javascript")
    assert "htmx" in r.text[:200].lower()
