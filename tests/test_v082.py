"""Tests for v0.8.2: MCP audit_categories.

audit-mute purge-expired CLI + add-from-stdin + MCP
audit_mute_purge_expired + HTTP DELETE /api/audit/mutes/expired moved
to test_audit_mutes.py in v0.11.9.
"""

from __future__ import annotations

from homelabsage.config import Config
from homelabsage.db import Database

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
