"""Tests for v0.8.1: audit_mutes (db + filter + CLI + HTTP + MCP)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from homelabsage.cli import app
from homelabsage.config import Config
from homelabsage.db import Database

# ─── db.add_audit_mute / list / remove ─────────────────────────────


def test_add_and_list_audit_mute(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.add_audit_mute(
        category="cve", source_kind="cve",
        source_ref="mealie@1.2.3", reason="known upstream",
    )
    rows = db.list_audit_mutes()
    assert len(rows) == 1
    assert rows[0]["category"] == "cve"
    assert rows[0]["expires_at"] is None
    assert rows[0]["reason"] == "known upstream"


def test_add_audit_mute_is_idempotent(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    for _ in range(3):
        db.add_audit_mute(
            category="x", source_kind="x", source_ref="ref1",
        )
    assert len(db.list_audit_mutes()) == 1


def test_remove_audit_mute(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.add_audit_mute(
        category="x", source_kind="x", source_ref="ref1",
    )
    assert db.remove_audit_mute(
        category="x", source_kind="x", source_ref="ref1",
    ) is True
    assert db.remove_audit_mute(
        category="x", source_kind="x", source_ref="ref1",
    ) is False
    assert db.list_audit_mutes() == []


def test_list_audit_mutes_drops_expired_by_default(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.add_audit_mute(
        category="a", source_kind="a", source_ref="r",
        expires_at="2020-01-01T00:00:00+00:00",   # already past
    )
    db.add_audit_mute(
        category="b", source_kind="b", source_ref="r",
        expires_at="2199-01-01T00:00:00+00:00",   # future
    )
    active = db.list_audit_mutes()
    assert {r["category"] for r in active} == {"b"}
    all_rows = db.list_audit_mutes(include_expired=True)
    assert {r["category"] for r in all_rows} == {"a", "b"}


def test_active_audit_mute_keys_returns_tuples(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.add_audit_mute(category="x", source_kind="y", source_ref="z")
    keys = db.active_audit_mute_keys()
    assert keys == {("x", "y", "z")}


def test_purge_expired_audit_mutes(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.add_audit_mute(
        category="a", source_kind="a", source_ref="r",
        expires_at="2020-01-01T00:00:00+00:00",
    )
    db.add_audit_mute(
        category="b", source_kind="b", source_ref="r",
        expires_at="2199-01-01T00:00:00+00:00",
    )
    db.add_audit_mute(category="c", source_kind="c", source_ref="r")   # perm
    assert db.purge_expired_audit_mutes() == 1
    # Future + permanent stay
    assert {r["category"] for r in db.list_audit_mutes(include_expired=True)} == {"b", "c"}


# ─── audit.build_report filters mutes ──────────────────────────────


def test_build_report_filters_muted_fingerprints(tmp_path):
    """A muted (category, source_kind, source_ref) doesn't surface."""
    from homelabsage.audit import build_report
    from homelabsage.models import (
        Analysis,
        AnalyzedUpdate,
        Severity,
        Update,
        UpdateStatus,
    )
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    # Seed an update with abandoned repo_health → produces an
    # `_abandoned_findings` row.
    item = AnalyzedUpdate(
        update=Update(
            source="docker", subject="oldthing",
            current_version="1", new_version="2",
            context={"repo_health": {"status": "abandoned", "reason": "no push"}},
        ),
        analysis=Analysis(severity=Severity.HIGH, summary="m"),
        status=UpdateStatus.NEW,
    )
    db.upsert(item)
    # Pre-mute: the abandoned finding appears
    rep_before = build_report(cfg, db)
    assert any(f.category == "abandoned" for f in rep_before.findings)
    # Mute it by the exact fingerprint the auditor uses
    abandoned = next(f for f in rep_before.findings if f.category == "abandoned")
    db.add_audit_mute(
        category=abandoned.category,
        source_kind=abandoned.source_kind,
        source_ref=abandoned.source_ref,
    )
    rep_after = build_report(cfg, db)
    assert not any(f.category == "abandoned" for f in rep_after.findings)


# ─── CLI: homelabsage audit-mute add/list/remove ──────────────────


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


def test_cli_audit_mute_add_and_list(tmp_path):
    cfg_path, db_path = _config_for_cli(tmp_path)
    runner = CliRunner()
    r = runner.invoke(
        app, [
            "audit-mute", "add", "cve", "cve", "mealie@1.2.3",
            "--config", str(cfg_path),
            "--for", "7d", "--reason", "false positive",
        ],
    )
    assert r.exit_code == 0, r.stdout
    assert "Muted" in r.stdout
    r2 = runner.invoke(
        app, ["audit-mute", "list", "--config", str(cfg_path)],
    )
    assert r2.exit_code == 0
    assert "mealie@1.2.3" in r2.stdout
    assert "false positive" in r2.stdout


def test_cli_audit_mute_remove(tmp_path):
    cfg_path, db_path = _config_for_cli(tmp_path)
    db = Database(str(db_path))
    db.add_audit_mute(category="x", source_kind="y", source_ref="z")
    db.close()
    runner = CliRunner()
    r = runner.invoke(
        app, [
            "audit-mute", "remove", "x", "y", "z",
            "--config", str(cfg_path),
        ],
    )
    assert r.exit_code == 0
    assert "Removed" in r.stdout


def test_cli_audit_mute_add_rejects_bad_relative(tmp_path):
    cfg_path, _ = _config_for_cli(tmp_path)
    runner = CliRunner()
    r = runner.invoke(
        app, [
            "audit-mute", "add", "a", "b", "c",
            "--config", str(cfg_path),
            "--for", "garbage",
        ],
    )
    assert r.exit_code != 0


# ─── HTTP /api/audit/mutes ─────────────────────────────────────────


def test_api_audit_mutes_post_get_delete(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    client = TestClient(create_app(cfg))

    # POST
    r = client.post(
        "/api/audit/mutes",
        json={
            "category": "cve", "source_kind": "cve",
            "source_ref": "mealie@1.2.3", "reason": "ignore for now",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"]

    # GET
    r = client.get("/api/audit/mutes")
    assert r.status_code == 200
    assert r.json()["count"] == 1

    # DELETE
    r = client.request(
        "DELETE", "/api/audit/mutes",
        json={"category": "cve", "source_kind": "cve",
              "source_ref": "mealie@1.2.3"},
    )
    assert r.status_code == 200
    # 404 when no match
    r = client.request(
        "DELETE", "/api/audit/mutes",
        json={"category": "cve", "source_kind": "cve",
              "source_ref": "mealie@1.2.3"},
    )
    assert r.status_code == 404


def test_api_audit_mutes_post_validates_iso(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    client = TestClient(create_app(cfg))
    r = client.post(
        "/api/audit/mutes",
        json={
            "category": "x", "source_kind": "y", "source_ref": "z",
            "expires_at": "not-a-date",
        },
    )
    assert r.status_code == 400


def test_api_audit_mutes_post_requires_fields(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    client = TestClient(create_app(cfg))
    r = client.post("/api/audit/mutes", json={"category": "x"})
    assert r.status_code == 400


# ─── MCP audit_mute_add/list/remove ────────────────────────────────


def test_mcp_audit_mute_add_list_remove(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    add = TOOLS["audit_mute_add"]["impl"]
    lst = TOOLS["audit_mute_list"]["impl"]
    rm = TOOLS["audit_mute_remove"]["impl"]
    out = add(cfg, db, {
        "category": "cve", "source_kind": "cve",
        "source_ref": "x@1", "reason": "noisy",
    })
    assert out["ok"] is True
    rows = lst(cfg, db, {})
    assert rows["count"] == 1
    out = rm(cfg, db, {
        "category": "cve", "source_kind": "cve", "source_ref": "x@1",
    })
    assert out["ok"] is True


def test_mcp_audit_mute_add_rejects_bad_iso(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    add = TOOLS["audit_mute_add"]["impl"]
    out = add(cfg, db, {
        "category": "x", "source_kind": "y", "source_ref": "z",
        "expires_at": "not-iso",
    })
    assert out["ok"] is False
    assert "ISO 8601" in out["error"]


def test_mcp_audit_mute_add_requires_fields(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    add = TOOLS["audit_mute_add"]["impl"]
    out = add(cfg, db, {"category": "x"})
    assert out["ok"] is False
