"""Tests for v0.8.5: homelabsage activity CLI."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from homelabsage.cli import app
from homelabsage.db import Database


def _config(tmp_path):
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


def _seed(db, subject):
    from homelabsage.models import AnalyzedUpdate, Update
    item = AnalyzedUpdate(
        update=Update(
            source="docker", subject=subject,
            current_version="1", new_version="2",
        ),
    )
    db.upsert(item)


def test_activity_cli_empty(tmp_path):
    cfg_path, _ = _config(tmp_path)
    runner = CliRunner()
    r = runner.invoke(app, ["activity", "--config", str(cfg_path)])
    assert r.exit_code == 0
    assert "No updates yet" in r.stdout


def test_activity_cli_shows_items(tmp_path):
    cfg_path, db_path = _config(tmp_path)
    db = Database(str(db_path))
    _seed(db, "mealie")
    _seed(db, "kavita")
    db.close()
    runner = CliRunner()
    r = runner.invoke(app, ["activity", "--config", str(cfg_path)])
    assert r.exit_code == 0
    assert "mealie" in r.stdout
    assert "kavita" in r.stdout


def test_activity_cli_json(tmp_path):
    cfg_path, db_path = _config(tmp_path)
    db = Database(str(db_path))
    _seed(db, "mealie")
    db.close()
    runner = CliRunner()
    r = runner.invoke(
        app, ["activity", "--config", str(cfg_path), "--json"],
    )
    assert r.exit_code == 0
    rows = [json.loads(ln) for ln in r.stdout.strip().splitlines() if ln]
    assert len(rows) == 1
    assert rows[0]["subject"] == "mealie"
    assert rows[0]["status"] == "new"


def test_activity_cli_limit_clamped(tmp_path):
    cfg_path, db_path = _config(tmp_path)
    db = Database(str(db_path))
    for i in range(3):
        _seed(db, f"app-{i}")
    db.close()
    runner = CliRunner()
    r = runner.invoke(
        app, ["activity", "--config", str(cfg_path), "--limit", "999999"],
    )
    assert r.exit_code == 0
