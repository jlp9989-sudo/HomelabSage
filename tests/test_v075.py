"""Tests for v0.7.5: MCP audit_history+diff, compose-graph CLI, audit --severity."""

from __future__ import annotations

from typer.testing import CliRunner

from homelabsage.audit_history import append
from homelabsage.cli import app
from homelabsage.cli.compose_graph import _node_id, render_mermaid
from homelabsage.config import Config
from homelabsage.db import Database

# ─── MCP audit_history / audit_diff ────────────────────────────────


def _report(findings: list[dict], *, generated_at: str = "2026-06-04T12:00:00Z"):
    return {
        "generated_at": generated_at,
        "healthy": not findings,
        "counts_by_severity": {"medium": len(findings)},
        "counts_by_category": {},
        "findings": findings,
    }


def test_mcp_audit_history_returns_compact(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    cfg.notes.notes_dir = str(tmp_path / "notes")
    append(cfg.notes.notes_dir, _report([{
        "category": "cve", "source_kind": "cve", "source_ref": "x",
        "severity": "high", "title": "t", "detail": "d", "cite": "c",
    }]))
    db = Database(":memory:")
    impl = TOOLS["audit_history"]["impl"]
    out = impl(cfg, db, {})
    assert out["count"] == 1
    assert out["items"][0]["finding_count"] == 1
    assert "findings" not in out["items"][0]   # never returns full list


def test_mcp_audit_history_no_notes_dir():
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["audit_history"]["impl"]
    out = impl(cfg, db, {})
    assert out["count"] == 0
    assert "no notes_dir" in out["reason"]


def test_mcp_audit_diff_first_run_all_new(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    cfg.notes.notes_dir = str(tmp_path / "notes")
    db = Database(cfg.storage.database_path)
    impl = TOOLS["audit_diff"]["impl"]
    out = impl(cfg, db, {})
    assert out["previous_snapshot"] is None
    assert isinstance(out["new"], list)
    assert out["resolved"] == []


def test_mcp_audit_diff_no_notes_dir():
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["audit_diff"]["impl"]
    out = impl(cfg, db, {})
    assert out["previous_snapshot"] is None
    assert "no notes_dir" in out["reason"]


# ─── compose-graph CLI ─────────────────────────────────────────────


def test_node_id_sanitises_special_chars():
    assert _node_id("foo-bar.svc") == "foo_bar_svc"
    assert _node_id("ok_name") == "ok_name"


def test_node_id_prefixes_leading_digit():
    """Mermaid rejects ids starting with a digit."""
    assert _node_id("123svc").startswith("n_")


def test_node_id_handles_empty():
    assert _node_id("") == "n_unknown"


def test_render_mermaid_empty():
    body = render_mermaid({}, {})
    assert body.strip() == "flowchart LR"


def test_render_mermaid_with_services():
    from homelabsage.compose import ComposeService
    svcs = {
        "web": ComposeService(
            name="web", container_name=None, image="nginx",
            depends_on=["db"], project="stack1",
        ),
        "db": ComposeService(
            name="db", container_name="db1", image="postgres",
            project="stack1",
        ),
    }
    body = render_mermaid(svcs, {"db": {"web"}})
    assert "flowchart LR" in body
    assert "web --> db" in body
    assert "subgraph" in body
    assert "stack1" in body


def test_compose_graph_cli_with_empty_paths(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    db_path = tmp_path / "h.db"
    cfg_path.write_text(
        f"""storage:
  database_path: {db_path}
llm:
  endpoint: ""
sources:
  docker:
    compose_scan_paths: []
""",
    )
    runner = CliRunner()
    result = runner.invoke(
        app, ["compose-graph", "--config", str(cfg_path)],
    )
    assert result.exit_code == 0
    assert "nothing to render" in result.stdout


def test_compose_graph_cli_writes_to_file(tmp_path):
    """End-to-end: parse a real compose file, write a fenced .md."""
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
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"""storage:
  database_path: {tmp_path / 'h.db'}
llm:
  endpoint: ""
sources:
  docker:
    compose_scan_paths:
      - {compose_dir}
""",
    )
    out_path = tmp_path / "graph.md"
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "compose-graph", "--config", str(cfg_path),
            "--out", str(out_path),
        ],
    )
    assert result.exit_code == 0, result.stdout
    body = out_path.read_text()
    assert body.startswith("```mermaid")
    assert "web --> db" in body


# ─── audit --severity filter ───────────────────────────────────────


def test_audit_severity_rejects_bad_value(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    db_path = tmp_path / "h.db"
    cfg_path.write_text(
        f"""storage:
  database_path: {db_path}
llm:
  endpoint: ""
""",
    )
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "audit", "--config", str(cfg_path),
            "--severity", "garbage",
        ],
    )
    assert result.exit_code != 0


def test_audit_severity_accepts_known_values(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    db_path = tmp_path / "h.db"
    cfg_path.write_text(
        f"""storage:
  database_path: {db_path}
llm:
  endpoint: ""
""",
    )
    runner = CliRunner()
    for sev in ("info", "medium", "high", "critical"):
        result = runner.invoke(
            app, [
                "audit", "--config", str(cfg_path),
                "--severity", sev, "--jsonl",
            ],
        )
        assert result.exit_code == 0, f"failed for {sev}: {result.stdout}"


def test_audit_filter_keeps_high_only():
    """The _filter_findings helper drops below-floor rows + recomputes counts."""
    from dataclasses import dataclass

    from homelabsage.cli.audit import _filter_findings

    @dataclass
    class _F:
        severity: str
        category: str
        title: str = "t"
        detail: str = "d"
        source_kind: str = "sk"
        source_ref: str = "sr"
        cite: str = "c"

    @dataclass
    class _R:
        findings: list
        counts_by_severity: dict
        counts_by_category: dict
        healthy: bool

    rep = _R(
        findings=[
            _F("info", "a"), _F("medium", "b"),
            _F("high", "c"), _F("critical", "d"),
        ],
        counts_by_severity={"info": 1, "medium": 1, "high": 1, "critical": 1},
        counts_by_category={"a": 1, "b": 1, "c": 1, "d": 1},
        healthy=False,
    )
    filtered = _filter_findings(rep, "high")
    sevs = [f.severity for f in filtered.findings]
    assert sevs == ["high", "critical"]
    assert filtered.counts_by_severity == {"high": 1, "critical": 1}
    assert filtered.healthy is False


def test_audit_filter_empty_floor_is_noop():
    """No severity flag → return report unchanged."""
    from dataclasses import dataclass

    from homelabsage.cli.audit import _filter_findings

    @dataclass
    class _R:
        findings: list
        counts_by_severity: dict
        counts_by_category: dict
        healthy: bool

    original = _R([1, 2, 3], {"info": 3}, {"x": 3}, False)
    out = _filter_findings(original, "")
    assert out is original
