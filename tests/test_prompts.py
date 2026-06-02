"""Tests for the built-in prompt template loader."""

from __future__ import annotations

import pytest

from homelabsage import prompts as prompts_mod
from homelabsage.prompts import clear_cache, load_template


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_cache()
    yield
    clear_cache()


def test_load_template_returns_analyzer_body():
    body = load_template("analyzer")
    assert "analyze software updates" in body
    # Format placeholders the LLM client substitutes at call time
    for placeholder in ("{source}", "{subject}", "{current_version}",
                        "{new_version}", "{context}", "{notes}",
                        "{release_notes}"):
        assert placeholder in body


def test_load_template_cached(monkeypatch):
    """Re-loading the same template name does NOT re-read the file."""
    real_read = prompts_mod.Path.read_text
    calls = {"count": 0}

    def _counting(self, *a, **kw):
        if str(self).endswith("/analyzer.md"):
            calls["count"] += 1
        return real_read(self, *a, **kw)

    monkeypatch.setattr(prompts_mod.Path, "read_text", _counting)
    load_template("analyzer")
    load_template("analyzer")
    load_template("analyzer")
    assert calls["count"] == 1


def test_load_template_missing_raises():
    with pytest.raises(FileNotFoundError):
        load_template("does-not-exist")


def test_clear_cache_forces_reload(monkeypatch):
    real_read = prompts_mod.Path.read_text
    calls = {"count": 0}

    def _counting(self, *a, **kw):
        if str(self).endswith("/analyzer.md"):
            calls["count"] += 1
        return real_read(self, *a, **kw)

    monkeypatch.setattr(prompts_mod.Path, "read_text", _counting)
    load_template("analyzer")
    clear_cache()
    load_template("analyzer")
    assert calls["count"] == 2
