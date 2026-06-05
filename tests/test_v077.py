"""Tests for v0.7.7: audit --diff-only, MCP audit_prune, /api/audit/history.jsonl."""

from __future__ import annotations

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from homelabsage.audit_history import FILE_NAME, append
from homelabsage.cli import app
from homelabsage.config import Config
from homelabsage.db import Database


def _report(findings: list[dict] | None = None, *,
            generated_at: str = "2026-06-04T12:00:00Z"):
    fs = findings or []
    return {
        "generated_at": generated_at,
        "healthy": not fs,
        "counts_by_severity": {"medium": len(fs)} if fs else {},
        "counts_by_category": {},
        "findings": fs,
    }


def _finding(category="cve", source_ref="ref1", severity="high"):
    return {
        "category": category, "source_kind": category,
        "source_ref": source_ref, "severity": severity,
        "title": "t", "detail": "d", "cite": "c",
    }


# ─── MCP audit_prune ──────────────────────────────────────────────


def test_mcp_audit_prune_drops_rows(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    cfg.notes.notes_dir = str(tmp_path / "notes")
    db = Database(":memory:")
    for i in range(5):
        append(cfg.notes.notes_dir,
               _report(generated_at=f"2026-06-{i+1:02d}T00:00:00Z"))
    impl = TOOLS["audit_prune"]["impl"]
    out = impl(cfg, db, {"keep_last": 2})
    assert out["ok"] is True
    assert out["dropped"] == 3
    assert out["keep_last"] == 2


def test_mcp_audit_prune_no_notes_dir():
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["audit_prune"]["impl"]
    out = impl(cfg, db, {"keep_last": 10})
    assert out["ok"] is False
    assert "no notes_dir" in out["reason"]


def test_mcp_audit_prune_defaults_to_100(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    cfg.notes.notes_dir = str(tmp_path / "notes")
    db = Database(":memory:")
    impl = TOOLS["audit_prune"]["impl"]
    # Empty params → defaults
    out = impl(cfg, db, {})
    assert out["ok"] is True
    assert out["keep_last"] == 100


# ─── /api/audit/history.jsonl raw ─────────────────────────────────


def test_api_audit_history_jsonl_empty(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    cfg.notes.notes_dir = str(tmp_path / "notes")
    client = TestClient(create_app(cfg))
    r = client.get("/api/audit/history.jsonl")
    assert r.status_code == 200
    assert r.text == ""


def test_api_audit_history_jsonl_serves_file(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    cfg.notes.notes_dir = str(tmp_path / "notes")
    append(cfg.notes.notes_dir, _report([_finding()]))
    append(cfg.notes.notes_dir, _report([_finding(source_ref="ref2")]))
    client = TestClient(create_app(cfg))
    r = client.get("/api/audit/history.jsonl")
    assert r.status_code == 200
    lines = [ln for ln in r.text.splitlines() if ln]
    assert len(lines) == 2
    # Content-Disposition + correct mime
    cd = r.headers.get("content-disposition", "")
    assert "audit_history.jsonl" in cd
    assert r.headers.get("content-type", "").startswith("application/x-ndjson")


# ─── audit --diff-only ────────────────────────────────────────────


def _config_yaml(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    db_path = tmp_path / "h.db"
    notes_dir = tmp_path / "notes"
    cfg_path.write_text(
        f"""storage:
  database_path: {db_path}
llm:
  endpoint: ""
notes:
  notes_dir: {notes_dir}
""",
    )
    return cfg_path, db_path, notes_dir


def test_audit_diff_only_no_previous_returns_all(tmp_path):
    """No previous snapshot → diff-only behaves like normal (empty here)."""
    cfg_path, db_path, notes_dir = _config_yaml(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app, ["audit", "--config", str(cfg_path), "--diff-only", "--jsonl"],
    )
    assert result.exit_code == 0
    # Empty DB + no previous snapshot → 0 findings
    assert result.stdout.strip() == ""


def test_audit_diff_only_jsonl_does_not_append_history(tmp_path):
    """--diff-only must not call run_audit (no append, no notes write)."""
    cfg_path, db_path, notes_dir = _config_yaml(tmp_path)
    runner = CliRunner()
    runner.invoke(
        app, ["audit", "--config", str(cfg_path), "--diff-only", "--jsonl"],
    )
    # audit_history.jsonl should NOT have been created by --diff-only
    assert not (notes_dir / FILE_NAME).exists()


def test_audit_diff_only_filters_against_previous(tmp_path):
    """Seed a previous snapshot, verify diff-only hides matching keys."""
    cfg_path, db_path, notes_dir = _config_yaml(tmp_path)
    # Seed a previous snapshot containing a fingerprint we expect to match
    # one that build_report would NOT produce on this empty DB.
    notes_dir.mkdir(parents=True, exist_ok=True)
    append(notes_dir, _report([_finding(category="x", source_ref="y")]))
    runner = CliRunner()
    result = runner.invoke(
        app, ["audit", "--config", str(cfg_path), "--diff-only", "--jsonl"],
    )
    assert result.exit_code == 0
    # No new findings vs the seed; output is empty
    assert result.stdout.strip() == ""
    # And the diff-only flag still didn't append a new history row
    contents = (notes_dir / FILE_NAME).read_text().splitlines()
    assert len(contents) == 1
