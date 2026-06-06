"""Tests for v0.7.5: compose-graph CLI + render_mermaid helper.

MCP audit_history + audit_diff + audit --severity tests moved to
test_audit_history.py in v0.11.8.
"""

from __future__ import annotations

from typer.testing import CliRunner

from homelabsage.cli import app
from homelabsage.cli.compose_graph import _node_id, render_mermaid

# ─── render_mermaid helper ─────────────────────────────────────────


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


# ─── compose-graph CLI ─────────────────────────────────────────────


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
