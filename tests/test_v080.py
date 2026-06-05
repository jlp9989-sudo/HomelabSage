"""Tests for v0.8.0 milestone: features CLI."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from homelabsage import __version__
from homelabsage.cli import app


def test_features_cli_plain_output():
    runner = CliRunner()
    result = runner.invoke(app, ["features"])
    assert result.exit_code == 0
    assert __version__ in result.stdout
    assert "doctor" in result.stdout


def test_features_cli_json_output_parses():
    runner = CliRunner()
    result = runner.invoke(app, ["features", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout.strip())
    assert payload["version"] == __version__
    assert isinstance(payload["version_parts"], list)
    assert len(payload["version_parts"]) == 3
    assert payload["features"]["snooze"] is True


def test_features_version_parts_matches_string():
    runner = CliRunner()
    result = runner.invoke(app, ["features", "--json"])
    payload = json.loads(result.stdout.strip())
    expected = [int(p) for p in __version__.split(".")[:3]]
    assert payload["version_parts"] == expected


def test_milestone_version_is_0_8_0():
    """Defensive: confirm the milestone bump landed."""
    assert __version__ == "0.8.0"
