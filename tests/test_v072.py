"""Tests for v0.7.2: list-snoozed + snooze CLI + audit --jsonl."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from homelabsage.cli import app
from homelabsage.config import Config
from homelabsage.db import Database
from homelabsage.models import AnalyzedUpdate, Update

# ─── db.list_snoozed ──────────────────────────────────────────────


def _seed(db, *, source="x", subject="y"):
    item = AnalyzedUpdate(
        update=Update(
            source=source, subject=subject,
            current_version="1", new_version="2",
        ),
    )
    db.upsert(item)
    return item


def test_list_snoozed_filters_past(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    a = _seed(db, subject="future")
    b = _seed(db, subject="past")
    db.set_snooze(a.id, "2199-12-31T00:00:00+00:00")
    db.set_snooze(b.id, "2020-01-01T00:00:00+00:00")
    rows = db.list_snoozed()
    assert [r["subject"] for r in rows] == ["future"]


def test_list_snoozed_sorts_by_until(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    a = _seed(db, subject="later")
    b = _seed(db, subject="sooner")
    db.set_snooze(a.id, "2199-12-31T00:00:00+00:00")
    db.set_snooze(b.id, "2050-01-01T00:00:00+00:00")
    rows = db.list_snoozed()
    assert [r["subject"] for r in rows] == ["sooner", "later"]


# ─── /api/updates/snoozed ────────────────────────────────────────


def test_api_list_snoozed(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    item = _seed(db)
    db.set_snooze(item.id, "2199-12-31T00:00:00+00:00")
    app_inst = create_app(cfg)
    client = TestClient(app_inst)
    r = client.get("/api/updates/snoozed")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["items"][0]["id"] == item.id


# ─── MCP list_snoozed ────────────────────────────────────────────


def test_mcp_list_snoozed(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    item = _seed(db)
    db.set_snooze(item.id, "2199-12-31T00:00:00+00:00")
    impl = TOOLS["list_snoozed"]["impl"]
    out = impl(cfg, db, {})
    assert out["count"] == 1


# ─── CLI: homelabsage snooze ─────────────────────────────────────


def _config_for_cli(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    db_path = tmp_path / "h.db"
    cfg_path.write_text(
        f"""storage:
  database_path: {db_path}
llm:
  endpoint: ""
""",
    )
    return cfg_path, db_path


def test_cli_snooze_until(tmp_path):
    cfg_path, db_path = _config_for_cli(tmp_path)
    db = Database(str(db_path))
    item = _seed(db)
    db.close()
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "snooze", item.id, "--config", str(cfg_path),
            "--until", "2199-12-31T00:00:00+00:00",
        ],
    )
    assert result.exit_code == 0, result.stdout
    db = Database(str(db_path))
    assert db.get_snooze(item.id) == "2199-12-31T00:00:00+00:00"


def test_cli_snooze_for_relative(tmp_path):
    cfg_path, db_path = _config_for_cli(tmp_path)
    db = Database(str(db_path))
    item = _seed(db)
    db.close()
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "snooze", item.id, "--config", str(cfg_path),
            "--for", "7d",
        ],
    )
    assert result.exit_code == 0, result.stdout
    db = Database(str(db_path))
    val = db.get_snooze(item.id)
    assert val is not None
    # Crude future check: starts with "2026" or later. Anything in the
    # past would fail.
    assert val[:4] >= "2026"


def test_cli_snooze_clear(tmp_path):
    cfg_path, db_path = _config_for_cli(tmp_path)
    db = Database(str(db_path))
    item = _seed(db)
    db.set_snooze(item.id, "2199-12-31T00:00:00+00:00")
    db.close()
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "snooze", item.id, "--config", str(cfg_path), "--clear",
        ],
    )
    assert result.exit_code == 0
    db = Database(str(db_path))
    assert db.get_snooze(item.id) is None


def test_cli_snooze_list(tmp_path):
    cfg_path, db_path = _config_for_cli(tmp_path)
    db = Database(str(db_path))
    item = _seed(db, subject="needs-rev")
    db.set_snooze(item.id, "2199-12-31T00:00:00+00:00")
    db.close()
    runner = CliRunner()
    result = runner.invoke(
        app, ["snooze", "--config", str(cfg_path), "--list"],
    )
    assert result.exit_code == 0
    assert "needs-rev" in result.stdout


def test_cli_snooze_rejects_both_until_and_for(tmp_path):
    cfg_path, db_path = _config_for_cli(tmp_path)
    db = Database(str(db_path))
    item = _seed(db)
    db.close()
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "snooze", item.id, "--config", str(cfg_path),
            "--until", "2199-12-31T00:00:00+00:00",
            "--for", "7d",
        ],
    )
    assert result.exit_code != 0


def test_cli_snooze_rejects_bad_relative(tmp_path):
    cfg_path, db_path = _config_for_cli(tmp_path)
    db = Database(str(db_path))
    item = _seed(db)
    db.close()
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "snooze", item.id, "--config", str(cfg_path),
            "--for", "garbage",
        ],
    )
    assert result.exit_code != 0


# ─── CLI: homelabsage audit --jsonl ──────────────────────────────


def test_cli_audit_jsonl_emits_one_object_per_line(tmp_path):
    """Empty DB → 0 findings → no output lines, exit 0."""
    cfg_path, db_path = _config_for_cli(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app, ["audit", "--config", str(cfg_path), "--jsonl"],
    )
    assert result.exit_code == 0
    # 0 findings in this fresh DB → no JSON output; or each line parses
    for line in result.stdout.strip().splitlines():
        if line:
            json.loads(line)
