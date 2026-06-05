"""Tests for v0.9.2: where-is CLI/MCP + MCP update_diff."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from homelabsage.cli import app
from homelabsage.compose import build_graph
from homelabsage.config import Config
from homelabsage.db import Database
from homelabsage.where_is import find


def _make_stack(tmp_path: Path) -> Path:
    stack = tmp_path / "stacks"
    a = stack / "mealie"
    a.mkdir(parents=True)
    (a / "docker-compose.yml").write_text(
        "services:\n"
        "  mealie:\n"
        "    image: ghcr.io/mealie-recipes/mealie:v2.5.0\n"
        "    container_name: mealie-app\n"
        "    depends_on: [db]\n"
        "  db:\n"
        "    image: postgres:16\n",
    )
    b = stack / "kavita"
    b.mkdir()
    (b / "docker-compose.yml").write_text(
        "services:\n"
        "  kavita:\n"
        "    image: jvmilazz0/kavita:0.8\n",
    )
    return stack


# ─── where_is.find ─────────────────────────────────────────────────


def test_find_exact_service_name(tmp_path):
    graph = build_graph([str(_make_stack(tmp_path))])
    hits = find(graph, "mealie")
    assert any(h.name == "mealie" for h in hits)
    h = next(h for h in hits if h.name == "mealie")
    assert h.container_name == "mealie-app"
    assert h.line > 0
    assert "mealie:" in h.context


def test_find_exact_container_name(tmp_path):
    graph = build_graph([str(_make_stack(tmp_path))])
    hits = find(graph, "mealie-app")
    assert any(h.container_name == "mealie-app" for h in hits)


def test_find_substring_case_insensitive(tmp_path):
    graph = build_graph([str(_make_stack(tmp_path))])
    hits = find(graph, "KAV")
    assert any(h.name == "kavita" for h in hits)


def test_find_empty_returns_empty(tmp_path):
    graph = build_graph([str(_make_stack(tmp_path))])
    assert find(graph, "") == []
    assert find(graph, "   ") == []


def test_find_no_match_returns_empty(tmp_path):
    graph = build_graph([str(_make_stack(tmp_path))])
    assert find(graph, "nonexistent-service") == []


# ─── homelabsage where-is CLI ──────────────────────────────────────


def _config(tmp_path, scan_dir):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"""storage:
  database_path: {tmp_path / "h.db"}
llm:
  endpoint: ""
sources:
  docker:
    compose_scan_paths:
      - {scan_dir}
""",
    )
    return cfg_path


def test_cli_where_is_finds_service(tmp_path):
    stack = _make_stack(tmp_path)
    cfg_path = _config(tmp_path, stack)
    runner = CliRunner()
    r = runner.invoke(app, ["where-is", "mealie", "--config", str(cfg_path)])
    assert r.exit_code == 0
    assert "mealie" in r.stdout
    assert "match" in r.stdout
    # Container_name annotation survives wrapping cleanly
    assert "mealie-app" in r.stdout


def test_cli_where_is_no_match(tmp_path):
    stack = _make_stack(tmp_path)
    cfg_path = _config(tmp_path, stack)
    runner = CliRunner()
    r = runner.invoke(
        app, ["where-is", "ghost-service", "--config", str(cfg_path)],
    )
    assert r.exit_code == 0
    assert "No match" in r.stdout


def test_cli_where_is_json(tmp_path):
    stack = _make_stack(tmp_path)
    cfg_path = _config(tmp_path, stack)
    runner = CliRunner()
    r = runner.invoke(
        app, ["where-is", "mealie", "--config", str(cfg_path), "--json"],
    )
    assert r.exit_code == 0
    rows = [json.loads(ln) for ln in r.stdout.strip().splitlines() if ln]
    assert any(row["name"] == "mealie" for row in rows)


def test_cli_where_is_empty_scan_paths(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"""storage:
  database_path: {tmp_path / "h.db"}
llm:
  endpoint: ""
""",
    )
    runner = CliRunner()
    r = runner.invoke(app, ["where-is", "anything", "--config", str(cfg_path)])
    assert r.exit_code == 0
    assert "compose_scan_paths is empty" in r.stdout


# ─── MCP where_is ─────────────────────────────────────────────────


def test_mcp_where_is_finds_service(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    cfg.sources.docker.compose_scan_paths = [str(_make_stack(tmp_path))]
    db = Database(":memory:")
    impl = TOOLS["where_is"]["impl"]
    out = impl(cfg, db, {"name": "mealie"})
    assert out["count"] >= 1
    assert any(it["name"] == "mealie" for it in out["items"])


def test_mcp_where_is_empty_name():
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["where_is"]["impl"]
    out = impl(cfg, db, {"name": ""})
    assert out["count"] == 0
    assert "name is required" in out["reason"]


def test_mcp_where_is_no_scan_paths():
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["where_is"]["impl"]
    out = impl(cfg, db, {"name": "mealie"})
    assert out["count"] == 0
    assert "no compose scan paths" in out["reason"]


# ─── MCP update_diff ──────────────────────────────────────────────


def _seed_analyzed(db, *, subject, severity, breaks):
    from homelabsage.models import (
        Analysis,
        AnalyzedUpdate,
        Severity,
        Update,
    )
    sev = {
        "info": Severity.INFO, "medium": Severity.MEDIUM,
        "high": Severity.HIGH, "critical": Severity.CRITICAL,
    }[severity]
    item = AnalyzedUpdate(
        update=Update(
            source="docker", subject=subject,
            current_version="1", new_version="2",
        ),
        analysis=Analysis(
            severity=sev, summary=f"summary for {subject}",
            breaking_changes=list(breaks),
        ),
    )
    db.upsert(item)
    return item


def test_mcp_update_diff_detects_severity_change(tmp_path):
    from homelabsage.mcp import TOOLS
    db = Database(str(tmp_path / "t.db"))
    a = _seed_analyzed(db, subject="a", severity="medium", breaks=["x"])
    b = _seed_analyzed(db, subject="b", severity="critical", breaks=["x", "y"])
    cfg = Config()
    impl = TOOLS["update_diff"]["impl"]
    out = impl(cfg, db, {"a": a.id, "b": b.id})
    assert out["ok"] is True
    assert out["diff"]["severity_changed"] is True
    assert out["diff"]["breaking_changes_added"] == ["y"]
    assert out["diff"]["breaking_changes_removed"] == []


def test_mcp_update_diff_missing_id_returns_error(tmp_path):
    from homelabsage.mcp import TOOLS
    db = Database(str(tmp_path / "t.db"))
    cfg = Config()
    impl = TOOLS["update_diff"]["impl"]
    out = impl(cfg, db, {"a": "ghost", "b": "ghost"})
    assert out["ok"] is False


def test_mcp_update_diff_requires_both_params(tmp_path):
    from homelabsage.mcp import TOOLS
    db = Database(str(tmp_path / "t.db"))
    cfg = Config()
    impl = TOOLS["update_diff"]["impl"]
    out = impl(cfg, db, {"a": "x"})
    assert out["ok"] is False
    assert "b is required" in out["error"]
