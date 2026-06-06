"""Tests for v0.7.2: audit --jsonl CLI.

list-snoozed + snooze CLI + MCP list_snoozed tests moved to
test_snooze.py in v0.11.6.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from homelabsage.cli import app


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
