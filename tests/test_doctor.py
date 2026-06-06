"""Doctor: CLI + HTTP + MCP + module-level build_report + watch + floor.

Consolidates the doctor surface from 4 release-pinned files in v0.11.7:
  - v0.7.0 (test_v070.py): CLI with --skip-llm + TLS reporting
  - v0.7.1 (test_v071.py): module build_report + /api/doctor + MCP
  - v0.7.4 (test_v074.py): --watch 0 oneshot behaviour
  - v0.7.6 (test_v076.py): --severity-floor + _audit_has_findings_at_or_above helper
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from homelabsage.cli import app
from homelabsage.config import Config
from homelabsage.db import Database
from homelabsage.doctor import build_report


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


# ─── module-level build_report ────────────────────────────────────


def test_doctor_report_skips_when_unconfigured(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    report = build_report(cfg, db, skip_llm=True)
    # Most sections are unconfigured → all `skipped: True`
    sections = report["sections"]
    assert sections["llm"]["skipped"] is True
    assert sections["tls"]["skipped"] is True
    assert sections["dns"]["skipped"] is True
    assert sections["disk"]["skipped"] is True
    assert sections["compose"]["skipped"] is True
    # Audit always runs (always available)
    assert "audit" in sections
    # Overall healthy because nothing complained
    assert report["healthy"] is True


def test_doctor_report_skip_llm_flag(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    report = build_report(cfg, db, skip_llm=True)
    assert report["sections"]["llm"]["skipped"]
    assert report["llm_unreachable"] is False


def test_doctor_report_llm_unreachable_flag(tmp_path, monkeypatch):
    cfg = Config()
    cfg.llm.endpoint = "http://127.0.0.1:1"   # nothing listens on port 1
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    report = build_report(cfg, db, skip_llm=False)
    assert report["llm_unreachable"] is True
    assert report["healthy"] is False


# ─── CLI: homelabsage doctor ──────────────────────────────────────


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
    # Skipped sections render with the `· ` prefix + the reason
    assert "LLM" in result.stdout
    assert "skip_llm flag" in result.stdout


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


# ─── CLI: doctor --watch ──────────────────────────────────────────


def test_doctor_watch_zero_acts_as_oneshot(tmp_path):
    """--watch 0 (default) keeps the single-run behaviour."""
    cfg_path = tmp_path / "config.yaml"
    db_path = tmp_path / "h.db"
    cfg_path.write_text(
        f"""storage:
  database_path: {db_path}
llm:
  endpoint: ""
""",
    )
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "doctor", "--config", str(cfg_path), "--skip-llm",
            "--watch", "0",
        ],
    )
    # Exit 0 healthy (nothing configured = nothing to fail)
    assert result.exit_code == 0
    assert "Verdict" in result.stdout


# ─── CLI: doctor --severity-floor ─────────────────────────────────


def test_doctor_severity_floor_rejects_unknown(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"""storage:
  database_path: {tmp_path / 'h.db'}
llm:
  endpoint: ""
""",
    )
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "doctor", "--config", str(cfg_path), "--skip-llm",
            "--severity-floor", "banana",
        ],
    )
    assert result.exit_code != 0


def test_doctor_audit_has_findings_at_or_above_helper():
    from homelabsage.cli.doctor import _audit_has_findings_at_or_above
    report = {
        "sections": {
            "audit": {
                "counts_by_severity": {"info": 5, "medium": 0, "high": 0},
            },
        },
    }
    assert _audit_has_findings_at_or_above(report, "info") is True
    assert _audit_has_findings_at_or_above(report, "medium") is False
    report2 = {
        "sections": {
            "audit": {
                "counts_by_severity": {"high": 1, "info": 4},
            },
        },
    }
    assert _audit_has_findings_at_or_above(report2, "high") is True
    assert _audit_has_findings_at_or_above(report2, "critical") is False


def test_doctor_default_floor_on_empty_config(tmp_path):
    """No probes configured + no findings → exit 0 regardless of floor."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"""storage:
  database_path: {tmp_path / 'h.db'}
llm:
  endpoint: ""
""",
    )
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "doctor", "--config", str(cfg_path), "--skip-llm",
            "--severity-floor", "high",
        ],
    )
    assert result.exit_code == 0


# ─── HTTP: /api/doctor ────────────────────────────────────────────


def test_api_doctor_returns_json(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    app_inst = create_app(cfg)
    client = TestClient(app_inst)
    r = client.get("/api/doctor?skip_llm=1")
    assert r.status_code == 200
    body = r.json()
    assert "healthy" in body
    assert "sections" in body
    assert body["sections"]["llm"]["skipped"]


# ─── MCP: doctor tool ─────────────────────────────────────────────


def test_mcp_doctor_tool(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    impl = TOOLS["doctor"]["impl"]
    out = impl(cfg, db, {"skip_llm": True})
    assert "healthy" in out
    assert "sections" in out
    assert out["sections"]["llm"]["skipped"]
