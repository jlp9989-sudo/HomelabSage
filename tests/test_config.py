import os

import pytest
from pydantic import ValidationError

from homelabsage.config import _expand, load_config


def test_expand_simple(monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    assert _expand("${FOO}") == "bar"


def test_expand_with_default_when_unset(monkeypatch):
    monkeypatch.delenv("MISSING", raising=False)
    assert _expand("${MISSING:-fallback}") == "fallback"


def test_expand_recurses_into_dicts_and_lists(monkeypatch):
    monkeypatch.setenv("X", "X-val")
    monkeypatch.setenv("Y", "Y-val")
    src = {"a": "${X}", "b": ["x", "${Y}", {"nested": "${X}"}]}
    out = _expand(src)
    assert out == {"a": "X-val", "b": ["x", "Y-val", {"nested": "X-val"}]}


def test_expand_leaves_unknown_var_empty(monkeypatch):
    monkeypatch.delenv("UNSET", raising=False)
    # No default → expands to empty string (lets pydantic apply field default)
    assert _expand("${UNSET}") == ""


def test_load_config_uses_yaml_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_SECRET", "s3cr3t")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
        llm:
          provider: openai
          api_key: ${MY_SECRET}
        sources:
          docker:
            enabled: true
        """
    )
    cfg = load_config(cfg_file)
    assert cfg.llm.provider == "openai"
    assert cfg.llm.api_key == "s3cr3t"
    assert cfg.sources.docker.enabled is True


def test_load_config_picks_up_dotenv(tmp_path):
    (tmp_path / ".env").write_text("FROM_ENVFILE=hi\n")
    (tmp_path / "config.yaml").write_text("llm:\n  api_key: ${FROM_ENVFILE}\n")
    # Ensure os.environ doesn't already have it from a previous test
    os.environ.pop("FROM_ENVFILE", None)
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.llm.api_key == "hi"


def test_telegram_min_severity_validation(tmp_path):
    (tmp_path / "config.yaml").write_text(
        """
        outputs:
          telegram:
            min_severity: bogus
        """
    )
    with pytest.raises(ValidationError):
        load_config(tmp_path / "config.yaml")
