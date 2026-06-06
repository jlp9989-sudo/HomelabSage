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


# clear_all_snoozes db + endpoint + MCP tests moved to test_snooze.py
# in v0.11.6.


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


# doctor --severity-floor tests moved to test_doctor.py in v0.11.7.
