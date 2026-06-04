"""Tests for v0.7.6: audit-prune, MCP compose_graph_mermaid + clear_all_snoozes,
/api/updates/recurring-failures + DELETE /api/updates/snoozed, doctor --severity-floor."""

from __future__ import annotations

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from homelabsage.audit_history import FILE_NAME, append, prune
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


def _seed_update(db, *, source="x", subject="y"):
    from homelabsage.models import AnalyzedUpdate, Update
    item = AnalyzedUpdate(
        update=Update(
            source=source, subject=subject,
            current_version="1", new_version="2",
        ),
    )
    db.upsert(item)
    return item


# ─── audit_history.prune ──────────────────────────────────────────


def test_prune_drops_oldest(tmp_path):
    notes = tmp_path / "notes"
    for i in range(10):
        append(notes, _report(generated_at=f"2026-06-{i+1:02d}T00:00:00Z"))
    dropped = prune(notes, keep_last=3)
    assert dropped == 7
    body = (notes / FILE_NAME).read_text().strip().splitlines()
    assert len(body) == 3


def test_prune_noop_when_within_cap(tmp_path):
    notes = tmp_path / "notes"
    append(notes, _report())
    assert prune(notes, keep_last=10) == 0


def test_prune_missing_file_returns_zero(tmp_path):
    assert prune(tmp_path / "missing", keep_last=10) == 0


def test_prune_keep_zero_truncates(tmp_path):
    notes = tmp_path / "notes"
    for i in range(3):
        append(notes, _report(generated_at=f"2026-06-{i+1:02d}T00:00:00Z"))
    dropped = prune(notes, keep_last=0)
    assert dropped == 3
    assert (notes / FILE_NAME).read_text() == ""


def test_prune_rejects_negative_keep(tmp_path):
    notes = tmp_path / "notes"
    append(notes, _report())
    assert prune(notes, keep_last=-1) == 0


# ─── audit-prune CLI ──────────────────────────────────────────────


def test_cli_audit_prune(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    notes_dir = tmp_path / "notes"
    db_path = tmp_path / "h.db"
    cfg_path.write_text(
        f"""storage:
  database_path: {db_path}
llm:
  endpoint: ""
notes:
  notes_dir: {notes_dir}
""",
    )
    for i in range(5):
        append(notes_dir, _report(generated_at=f"2026-06-{i+1:02d}T00:00:00Z"))
    runner = CliRunner()
    result = runner.invoke(
        app, ["audit-prune", "--config", str(cfg_path), "--keep", "2"],
    )
    assert result.exit_code == 0
    assert "Pruned 3" in result.stdout
    remaining = (notes_dir / FILE_NAME).read_text().strip().splitlines()
    assert len(remaining) == 2


# ─── MCP compose_graph_mermaid ────────────────────────────────────


def test_mcp_compose_graph_mermaid_with_real_compose(tmp_path):
    from homelabsage.mcp import TOOLS
    compose_dir = tmp_path / "stack"
    compose_dir.mkdir()
    (compose_dir / "docker-compose.yml").write_text(
        "services:\n"
        "  web:\n"
        "    image: nginx\n"
        "    depends_on: [db]\n"
        "  db:\n"
        "    image: postgres\n",
    )
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["compose_graph_mermaid"]["impl"]
    out = impl(cfg, db, {"paths": [str(compose_dir)]})
    assert out["service_count"] == 2
    assert out["edge_count"] == 1
    assert "web --> db" in out["mermaid"]


def test_mcp_compose_graph_mermaid_no_paths():
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["compose_graph_mermaid"]["impl"]
    out = impl(cfg, db, {})
    assert out["service_count"] == 0
    assert "no compose scan paths" in out["reason"]


# ─── clear_all_snoozes (db + endpoint + MCP) ──────────────────────


def test_db_clear_all_snoozes(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    a = _seed_update(db, subject="a")
    b = _seed_update(db, subject="b")
    c = _seed_update(db, subject="c")
    db.set_snooze(a.id, "2199-01-01T00:00:00+00:00")
    db.set_snooze(b.id, "2199-01-01T00:00:00+00:00")
    # c left unsnoozed
    assert db.clear_all_snoozes() == 2
    # idempotent on second call
    assert db.clear_all_snoozes() == 0
    assert db.get_snooze(a.id) is None
    assert db.get_snooze(b.id) is None
    assert db.get_snooze(c.id) is None


def test_api_delete_snoozed(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    item = _seed_update(db)
    db.set_snooze(item.id, "2199-01-01T00:00:00+00:00")
    client = TestClient(create_app(cfg))
    r = client.delete("/api/updates/snoozed")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["cleared"] == 1


def test_mcp_clear_all_snoozes(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    item = _seed_update(db)
    db.set_snooze(item.id, "2199-01-01T00:00:00+00:00")
    impl = TOOLS["clear_all_snoozes"]["impl"]
    out = impl(cfg, db, {})
    assert out["ok"] is True
    assert out["cleared"] == 1


# ─── /api/updates/recurring-failures ──────────────────────────────


def test_api_recurring_failures(tmp_path):
    from homelabsage.models import UpdateStatus
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    item = _seed_update(db, subject="flaky")
    # Need real transitions to bump count after v0.7.1 fix
    db.set_status(item.id, UpdateStatus.FAILED)
    db.set_status(item.id, UpdateStatus.APPLIED)
    db.set_status(item.id, UpdateStatus.FAILED)
    db.set_status(item.id, UpdateStatus.APPLIED)
    db.set_status(item.id, UpdateStatus.FAILED)
    client = TestClient(create_app(cfg))
    r = client.get("/api/updates/recurring-failures?min_count=2")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["items"][0]["failure_count"] == 3


# ─── doctor --severity-floor ──────────────────────────────────────


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
