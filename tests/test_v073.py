"""Tests for v0.7.3: audit_history + /api/audit/diff + homelabsage init."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from homelabsage.audit_history import (
    FILE_NAME,
    append,
    diff_against_latest,
    load_latest,
)
from homelabsage.cli import app
from homelabsage.config import Config

# ─── audit_history ────────────────────────────────────────────────


def _report(findings: list[dict], *, generated_at: str = "2026-06-04T12:00:00Z"):
    return {
        "generated_at": generated_at,
        "healthy": not findings,
        "counts_by_severity": {"medium": len(findings)},
        "counts_by_category": {},
        "findings": findings,
    }


def test_history_append_creates_file(tmp_path):
    notes = tmp_path / "notes"
    out = append(notes, _report([{
        "category": "cve",
        "source_kind": "cve",
        "source_ref": "mealie@1.2.3",
        "severity": "high",
        "title": "x", "detail": "y", "cite": "z",
    }]))
    assert out is not None
    assert (notes / FILE_NAME).exists()


def test_history_append_is_truly_append_only(tmp_path):
    notes = tmp_path / "notes"
    append(notes, _report([], generated_at="2026-06-01T00:00:00Z"))
    append(notes, _report([], generated_at="2026-06-02T00:00:00Z"))
    contents = (notes / FILE_NAME).read_text().splitlines()
    assert len(contents) == 2


def test_history_load_latest_returns_last_row(tmp_path):
    notes = tmp_path / "notes"
    append(notes, _report([], generated_at="2026-06-01T00:00:00Z"))
    append(notes, _report([{
        "category": "x", "source_kind": "y", "source_ref": "z",
        "severity": "medium", "title": "a", "detail": "b", "cite": "c",
    }], generated_at="2026-06-02T00:00:00Z"))
    latest = load_latest(notes)
    assert latest is not None
    assert latest.generated_at == "2026-06-02T00:00:00Z"
    assert ("x", "y", "z") in latest.finding_keys


def test_history_load_latest_missing_returns_none(tmp_path):
    assert load_latest(tmp_path / "no_dir") is None


def test_history_load_latest_corrupt_skipped(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / FILE_NAME).write_text("not json\n")
    assert load_latest(notes) is None


def test_history_diff_first_run_marks_all_new(tmp_path):
    notes = tmp_path / "notes"
    current = _report([{
        "category": "cve", "source_kind": "cve", "source_ref": "id1",
        "severity": "high", "title": "x", "detail": "y", "cite": "z",
    }])
    diff = diff_against_latest(notes, current)
    assert diff["previous_snapshot"] is None
    assert len(diff["new"]) == 1
    assert diff["resolved"] == []


def test_history_diff_resolves_and_new(tmp_path):
    notes = tmp_path / "notes"
    prev = _report([
        {"category": "a", "source_kind": "a", "source_ref": "old",
         "severity": "medium", "title": "x", "detail": "y", "cite": "z"},
        {"category": "b", "source_kind": "b", "source_ref": "still",
         "severity": "medium", "title": "x", "detail": "y", "cite": "z"},
    ])
    append(notes, prev)
    current = _report([
        {"category": "b", "source_kind": "b", "source_ref": "still",
         "severity": "medium", "title": "x", "detail": "y", "cite": "z"},
        {"category": "c", "source_kind": "c", "source_ref": "new",
         "severity": "medium", "title": "x", "detail": "y", "cite": "z"},
    ])
    diff = diff_against_latest(notes, current)
    assert {f["source_ref"] for f in diff["new"]} == {"new"}
    assert {f["source_ref"] for f in diff["resolved"]} == {"old"}


def test_history_append_handles_unwritable_dir(monkeypatch, tmp_path):
    notes = tmp_path / "notes"
    # Pre-create as a file so mkdir(parents=True, exist_ok=True) fails
    notes.write_text("")
    out = append(notes, _report([]))
    assert out is None


# ─── /api/audit/diff ───────────────────────────────────────────────


def test_api_audit_diff_first_call_no_previous(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    cfg.notes.notes_dir = str(tmp_path / "notes")
    app_inst = create_app(cfg)
    client = TestClient(app_inst)
    r = client.get("/api/audit/diff")
    assert r.status_code == 200
    body = r.json()
    assert body["previous_snapshot"] is None
    assert body["resolved"] == []


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
    import yaml
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


# Keep json referenced so the linter is happy in case nothing imports it.
_ = json
