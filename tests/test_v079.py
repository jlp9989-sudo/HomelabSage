"""Tests for v0.7.9: purge + scan_window_check MCP + version_parts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from homelabsage import __version__
from homelabsage.cli import app
from homelabsage.config import Config
from homelabsage.db import Database
from homelabsage.models import AnalyzedUpdate, Update, UpdateStatus


def _seed_with_detected_at(db, *, subject, status, detected_at_iso):
    item = AnalyzedUpdate(
        update=Update(
            source="x", subject=subject,
            current_version="1", new_version="2",
        ),
        detected_at=datetime.fromisoformat(detected_at_iso),
    )
    db.upsert(item)
    db.set_status(item.id, status)
    return item


# ─── db.purge_old_updates ─────────────────────────────────────────


def test_purge_drops_only_old_terminal(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    now = datetime.now(UTC)
    # 100 days old, APPLIED → purged
    _seed_with_detected_at(
        db, subject="old-applied",
        status=UpdateStatus.APPLIED,
        detected_at_iso=(now - timedelta(days=100)).isoformat(),
    )
    # 100 days old, NEW → kept (status not in default list)
    _seed_with_detected_at(
        db, subject="old-new",
        status=UpdateStatus.NEW,
        detected_at_iso=(now - timedelta(days=100)).isoformat(),
    )
    # 5 days old, APPLIED → kept (too recent)
    _seed_with_detected_at(
        db, subject="recent-applied",
        status=UpdateStatus.APPLIED,
        detected_at_iso=(now - timedelta(days=5)).isoformat(),
    )
    n = db.purge_old_updates(older_than_days=90)
    assert n == 1
    remaining = {it.update.subject for it in db.list(limit=10)}
    assert "old-applied" not in remaining
    assert "old-new" in remaining
    assert "recent-applied" in remaining


def test_purge_dry_run_changes_nothing(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    now = datetime.now(UTC)
    _seed_with_detected_at(
        db, subject="dry",
        status=UpdateStatus.DISMISSED,
        detected_at_iso=(now - timedelta(days=200)).isoformat(),
    )
    n = db.purge_old_updates(older_than_days=90, dry_run=True)
    assert n == 1
    assert len(db.list(limit=10)) == 1


# test_purge_skips_snoozed moved to test_snooze.py in v0.11.6.


def test_purge_zero_days_noop(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    assert db.purge_old_updates(older_than_days=0) == 0


# ─── homelabsage purge CLI ────────────────────────────────────────


def test_purge_cli_dry_run(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    db_path = tmp_path / "h.db"
    cfg_path.write_text(
        f"""storage:
  database_path: {db_path}
llm:
  endpoint: ""
""",
    )
    db = Database(str(db_path))
    now = datetime.now(UTC)
    _seed_with_detected_at(
        db, subject="z",
        status=UpdateStatus.APPLIED,
        detected_at_iso=(now - timedelta(days=200)).isoformat(),
    )
    db.close()
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "purge", "--config", str(cfg_path),
            "--days", "90", "--dry-run",
        ],
    )
    assert result.exit_code == 0
    assert "Would purge 1" in result.stdout
    db = Database(str(db_path))
    assert len(db.list(limit=10)) == 1


def test_purge_cli_actually_deletes(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    db_path = tmp_path / "h.db"
    cfg_path.write_text(
        f"""storage:
  database_path: {db_path}
llm:
  endpoint: ""
""",
    )
    db = Database(str(db_path))
    now = datetime.now(UTC)
    _seed_with_detected_at(
        db, subject="z",
        status=UpdateStatus.APPLIED,
        detected_at_iso=(now - timedelta(days=200)).isoformat(),
    )
    db.close()
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "purge", "--config", str(cfg_path), "--days", "90",
        ],
    )
    assert result.exit_code == 0
    assert "Purged 1" in result.stdout
    db = Database(str(db_path))
    assert len(db.list(limit=10)) == 0


# ─── MCP purge_old_updates + scan_window_check ────────────────────


def test_mcp_purge_old_updates(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    now = datetime.now(UTC)
    _seed_with_detected_at(
        db, subject="a",
        status=UpdateStatus.APPLIED,
        detected_at_iso=(now - timedelta(days=300)).isoformat(),
    )
    impl = TOOLS["purge_old_updates"]["impl"]
    out = impl(cfg, db, {"older_than_days": 90, "dry_run": True})
    assert out["ok"] is True
    assert out["purged"] == 1
    assert out["dry_run"] is True
    # Real run actually deletes
    out2 = impl(cfg, db, {"older_than_days": 90})
    assert out2["purged"] == 1


def test_mcp_scan_window_check_disabled():
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["scan_window_check"]["impl"]
    out = impl(cfg, db, {})
    assert out["enabled"] is False
    assert out["blocked"] is False


def test_mcp_scan_window_check_enabled_inside_window():
    from homelabsage.mcp import TOOLS
    cfg = Config()
    cfg.scan_window.enabled = True
    cfg.scan_window.window = "00:00-23:59"
    cfg.scan_window.timezone = "UTC"
    db = Database(":memory:")
    impl = TOOLS["scan_window_check"]["impl"]
    out = impl(cfg, db, {})
    assert out["enabled"] is True
    assert out["blocked"] is True


# ─── MCP version with version_parts ───────────────────────────────


def test_mcp_version_parts_returns_int_triple():
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["version"]["impl"]
    out = impl(cfg, db, {})
    assert out["version"] == __version__
    parts = out["version_parts"]
    assert isinstance(parts, list)
    assert len(parts) == 3
    assert all(isinstance(p, int) for p in parts)
    # Sanity: version_parts agrees with the version string prefix
    expected = [int(p) for p in __version__.split(".")[:3]]
    assert parts == expected
