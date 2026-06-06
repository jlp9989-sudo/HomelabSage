"""Tests for v0.9.4: oom_killed + network_mode_host detectors +
MCP get_explainer + list_heartbeats."""

from __future__ import annotations

from homelabsage.audit import build_report
from homelabsage.config import Config
from homelabsage.db import Database
from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
)
from homelabsage.network_mode_host import (
    HostNetFinding,
)
from homelabsage.network_mode_host import (
    evaluate as eval_hostnet,
)
from homelabsage.oom_killed import OOMFinding
from homelabsage.oom_killed import evaluate as eval_oom

# ─── oom_killed unit ──────────────────────────────────────────────


def test_oom_killed_flag_returns_high():
    state = {
        "OOMKilled": True, "ExitCode": 137,
        "FinishedAt": "2026-06-04T10:00:00Z",
    }
    out = eval_oom(state)
    assert out is not None
    assert out.severity == "high"
    assert out.exit_code == 137
    assert out.finished_at == "2026-06-04T10:00:00Z"


def test_oom_killed_flag_false_returns_none():
    assert eval_oom({"OOMKilled": False}) is None


def test_oom_killed_missing_state_returns_none():
    assert eval_oom({}) is None
    assert eval_oom(None) is None


def test_oom_killed_strips_zero_date():
    """Docker emits 0001-01-01 for never-finished containers."""
    state = {"OOMKilled": True, "ExitCode": 137,
             "FinishedAt": "0001-01-01T00:00:00Z"}
    out = eval_oom(state)
    assert out is not None
    assert out.finished_at is None


def test_oom_to_context_shape():
    f = OOMFinding(oom_killed=True, exit_code=137,
                   finished_at="t", severity="high")
    ctx = f.to_context()
    assert ctx["oom_killed"] is True
    assert ctx["severity"] == "high"


# ─── network_mode_host unit ──────────────────────────────────────


def test_network_mode_host_flags_host():
    out = eval_hostnet({"NetworkMode": "host"})
    assert out is not None
    assert out.severity == "info"


def test_network_mode_host_case_insensitive():
    out = eval_hostnet({"NetworkMode": "Host"})
    assert out is not None


def test_network_mode_bridge_returns_none():
    assert eval_hostnet({"NetworkMode": "bridge"}) is None
    assert eval_hostnet({}) is None


def test_hostnet_to_context_shape():
    f = HostNetFinding(network_mode="host", severity="info")
    assert f.to_context() == {"network_mode": "host", "severity": "info"}


# ─── audit wiring ────────────────────────────────────────────────


def _seed_with_context(db, *, subject, context):
    item = AnalyzedUpdate(
        update=Update(
            source="docker", subject=subject,
            current_version="1", new_version="2",
            context=context,
        ),
        analysis=Analysis(severity=Severity.HIGH, summary="m"),
    )
    db.upsert(item)
    return item


def test_audit_surfaces_oom_killed(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    _seed_with_context(db, subject="hungry", context={
        "oom_killed": {
            "oom_killed": True, "exit_code": 137,
            "finished_at": "2026-06-04T10:00:00Z", "severity": "high",
        },
    })
    report = build_report(cfg, db)
    oom = [f for f in report.findings if f.category == "oom_killed"]
    assert len(oom) == 1
    assert oom[0].severity == "high"


def test_audit_surfaces_network_mode_host(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    _seed_with_context(db, subject="tailscale", context={
        "network_mode_host": {"network_mode": "host", "severity": "info"},
    })
    report = build_report(cfg, db)
    hn = [f for f in report.findings if f.category == "network_mode_host"]
    assert len(hn) == 1
    assert hn[0].severity == "info"


# ─── MCP get_explainer ────────────────────────────────────────────


def test_mcp_get_explainer_missing_returns_error(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    impl = TOOLS["get_explainer"]["impl"]
    out = impl(cfg, db, {"update_id": "ghost"})
    assert out["ok"] is False
    assert "no explainer" in out["error"]


def test_mcp_get_explainer_requires_update_id():
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["get_explainer"]["impl"]
    out = impl(cfg, db, {})
    assert out["ok"] is False
    assert "update_id is required" in out["error"]


def test_mcp_get_explainer_returns_cached(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    # Seed an update + explainer
    item = AnalyzedUpdate(
        update=Update(
            source="x", subject="y",
            current_version="1", new_version="2",
        ),
    )
    db.upsert(item)
    db.upsert_explainer(
        update_id=item.id,
        prompt="why is this risky?",
        raw_response="because X",
        model="test-model",
        provider="openai",
        notes_used="(no notes)",
    )
    impl = TOOLS["get_explainer"]["impl"]
    out = impl(cfg, db, {"update_id": item.id})
    assert out["ok"] is True
    assert out["prompt"] == "why is this risky?"
    assert out["model"] == "test-model"


# ─── MCP list_heartbeats ─────────────────────────────────────────


def test_mcp_list_heartbeats_empty(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    impl = TOOLS["list_heartbeats"]["impl"]
    out = impl(cfg, db, {})
    assert "summary" in out
    assert out["recent"] == []


def test_mcp_list_heartbeats_returns_recorded(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(str(tmp_path / "t.db"))
    db.record_heartbeat(
        url="https://example/ping", ok=True, status_code=200,
        error=None, duration_ms=42,
    )
    impl = TOOLS["list_heartbeats"]["impl"]
    out = impl(cfg, db, {"limit": 10})
    assert len(out["recent"]) == 1
    assert out["recent"][0]["status_code"] == 200
