"""Tests for v0.7.0: doctor CLI."""

from __future__ import annotations

from typer.testing import CliRunner

from homelabsage.cli import app


def _minimal_config(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    db_path = tmp_path / "homelabsage.db"
    cfg_path.write_text(
        f"""storage:
  database_path: {db_path}
llm:
  endpoint: ""
tls_check:
  urls: []
""",
    )
    return cfg_path


def test_doctor_runs_with_skip_llm_on_empty_config(tmp_path):
    cfg_path = _minimal_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app, ["doctor", "--config", str(cfg_path), "--skip-llm"],
    )
    # No probes have content to report; verdict is healthy → exit 0.
    assert result.exit_code == 0
    assert "Verdict: healthy" in result.stdout


def test_doctor_skips_llm_check_when_flag_set(tmp_path):
    cfg_path = _minimal_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app, ["doctor", "--config", str(cfg_path), "--skip-llm"],
    )
    assert "LLM check skipped" in result.stdout


def test_doctor_reports_tls_when_urls_configured(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    db_path = tmp_path / "homelabsage.db"
    cfg_path.write_text(
        f"""storage:
  database_path: {db_path}
llm:
  endpoint: ""
tls_check:
  urls:
    - http://example.com/
""",
    )
    runner = CliRunner()
    result = runner.invoke(
        app, ["doctor", "--config", str(cfg_path), "--skip-llm"],
    )
    # http:// returns ok=True with "no TLS" message; treated as healthy.
    assert result.exit_code == 0
    assert "TLS" in result.stdout
