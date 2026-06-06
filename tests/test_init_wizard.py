"""Tests for v0.7.3: homelabsage init wizard.

audit_history + /api/audit/diff tests moved to test_audit_history.py
in v0.11.8.
"""

from __future__ import annotations

import yaml
from typer.testing import CliRunner

from homelabsage.cli import app

# ─── homelabsage init ─────────────────────────────────────────────


def test_init_non_interactive_writes_config(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    runner = CliRunner()
    result = runner.invoke(
        app, ["init", str(cfg_path), "--non-interactive"],
    )
    assert result.exit_code == 0, result.stdout
    assert cfg_path.exists()
    body = cfg_path.read_text()
    assert "homelabsage" in body.lower()
    assert "database_path" in body
    assert "Wrote" in result.stdout


def test_init_refuses_overwrite_without_force(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("# existing\n")
    runner = CliRunner()
    result = runner.invoke(
        app, ["init", str(cfg_path), "--non-interactive"],
    )
    assert result.exit_code == 2
    assert cfg_path.read_text() == "# existing\n"


def test_init_force_overwrites(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("# existing\n")
    runner = CliRunner()
    result = runner.invoke(
        app, ["init", str(cfg_path), "--non-interactive", "--force"],
    )
    assert result.exit_code == 0
    assert "existing" not in cfg_path.read_text()


def test_init_generates_valid_yaml(tmp_path):
    """Generated config must be valid YAML and parseable by load_config."""
    cfg_path = tmp_path / "config.yaml"
    runner = CliRunner()
    result = runner.invoke(
        app, ["init", str(cfg_path), "--non-interactive"],
    )
    assert result.exit_code == 0
    parsed = yaml.safe_load(cfg_path.read_text())
    assert "llm" in parsed
    assert "storage" in parsed
    assert isinstance(parsed["llm"], dict)
