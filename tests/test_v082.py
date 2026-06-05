"""Tests for v0.8.2: purge-expired verb + add-from-stdin + audit_categories."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from homelabsage.cli import app
from homelabsage.config import Config
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


# ─── audit-mute purge-expired CLI ────────────────────────────────


def test_cli_audit_mute_purge_expired(tmp_path):
    cfg_path, db_path = _config(tmp_path)
    db = Database(str(db_path))
    db.add_audit_mute(
        category="a", source_kind="a", source_ref="r",
        expires_at="2020-01-01T00:00:00+00:00",
    )
    db.add_audit_mute(
        category="b", source_kind="b", source_ref="r",
        expires_at="2199-01-01T00:00:00+00:00",
    )
    db.close()
    runner = CliRunner()
    r = runner.invoke(
        app, ["audit-mute", "purge-expired", "--config", str(cfg_path)],
    )
    assert r.exit_code == 0
    assert "Purged 1" in r.stdout
    db = Database(str(db_path))
    rows = db.list_audit_mutes(include_expired=True)
    assert {row["category"] for row in rows} == {"b"}


def test_cli_audit_mute_purge_expired_noop(tmp_path):
    cfg_path, _db_path = _config(tmp_path)
    runner = CliRunner()
    r = runner.invoke(
        app, ["audit-mute", "purge-expired", "--config", str(cfg_path)],
    )
    assert r.exit_code == 0
    assert "Nothing to purge" in r.stdout


# ─── audit-mute add-from-stdin CLI ────────────────────────────────


def test_cli_audit_mute_add_from_stdin_bulk(tmp_path):
    cfg_path, db_path = _config(tmp_path)
    runner = CliRunner()
    stdin = "\n".join([
        json.dumps({"category": "cve", "source_kind": "cve",
                    "source_ref": "x@1", "severity": "high"}),
        json.dumps({"category": "cve", "source_kind": "cve",
                    "source_ref": "y@2", "severity": "medium"}),
        json.dumps({"category": "compose_override",
                    "source_kind": "compose_override",
                    "source_ref": "/stacks/a"}),
        "",  # blank line ignored
    ]) + "\n"
    r = runner.invoke(
        app, [
            "audit-mute", "add-from-stdin",
            "--config", str(cfg_path),
            "--for", "30d", "--reason", "bulk",
        ],
        input=stdin,
    )
    assert r.exit_code == 0, r.stdout
    assert "Muted 3 fingerprint" in r.stdout
    db = Database(str(db_path))
    assert len(db.list_audit_mutes()) == 3


def test_cli_audit_mute_add_from_stdin_skips_malformed(tmp_path):
    cfg_path, db_path = _config(tmp_path)
    runner = CliRunner()
    stdin = "\n".join([
        "not json at all",
        json.dumps({"category": "x"}),                       # missing fields
        json.dumps(["not", "a", "dict"]),                    # wrong shape
        json.dumps({"category": "ok", "source_kind": "k",
                    "source_ref": "r"}),                     # valid
    ]) + "\n"
    r = runner.invoke(
        app, [
            "audit-mute", "add-from-stdin",
            "--config", str(cfg_path),
        ],
        input=stdin,
    )
    assert r.exit_code == 0
    assert "Muted 1" in r.stdout
    assert "Skipped 3" in r.stdout


def test_cli_audit_mute_add_from_stdin_rejects_bad_for(tmp_path):
    cfg_path, _ = _config(tmp_path)
    runner = CliRunner()
    r = runner.invoke(
        app, [
            "audit-mute", "add-from-stdin",
            "--config", str(cfg_path),
            "--for", "garbage",
        ],
        input="",
    )
    assert r.exit_code != 0


# ─── MCP audit_mute_purge_expired ─────────────────────────────────


def test_mcp_audit_mute_purge_expired(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    db.add_audit_mute(
        category="a", source_kind="a", source_ref="r",
        expires_at="2020-01-01T00:00:00+00:00",
    )
    impl = TOOLS["audit_mute_purge_expired"]["impl"]
    out = impl(cfg, db, {})
    assert out["ok"] is True
    assert out["purged"] == 1


def test_mcp_audit_mute_purge_expired_when_empty(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    impl = TOOLS["audit_mute_purge_expired"]["impl"]
    out = impl(cfg, db, {})
    assert out["ok"] is True
    assert out["purged"] == 0


# ─── MCP audit_categories ────────────────────────────────────────


def test_mcp_audit_categories_empty(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    impl = TOOLS["audit_categories"]["impl"]
    out = impl(cfg, db, {})
    assert out["total"] == 0
    assert out["healthy"] is True
    assert "counts_by_category" in out
    assert "counts_by_severity" in out


def test_mcp_audit_categories_excludes_full_findings(tmp_path):
    """Defensive: the compact payload must not include the findings list."""
    from homelabsage.mcp import TOOLS
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    impl = TOOLS["audit_categories"]["impl"]
    out = impl(cfg, db, {})
    assert "findings" not in out


# ─── HTTP DELETE /api/audit/mutes/expired ────────────────────────


def test_api_audit_mutes_purge_expired(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    db.add_audit_mute(
        category="a", source_kind="a", source_ref="r",
        expires_at="2020-01-01T00:00:00+00:00",
    )
    db.add_audit_mute(
        category="b", source_kind="b", source_ref="r",
        expires_at="2199-01-01T00:00:00+00:00",
    )
    client = TestClient(create_app(cfg))
    r = client.delete("/api/audit/mutes/expired")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["purged"] == 1
