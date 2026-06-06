"""Tests for the v0.4.8 grab-bag — apprise, smtp, resource_limits,
arch_mismatch, scan_diff, stack_health, profile, MCP analyze_url/csi."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.arch_mismatch import ArchFinding, host_platform
from homelabsage.arch_mismatch import evaluate as arch_eval
from homelabsage.config import (
    AppriseOutputConfig,
    Config,
    SMTPOutputConfig,
    load_config,
)
from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
)
from homelabsage.outputs.apprise import AppriseOutput
from homelabsage.outputs.smtp import SMTPOutput
from homelabsage.resource_limits import (
    ResourceLimitFinding,
    has_cpu_limit,
    has_memory_limit,
)
from homelabsage.resource_limits import (
    evaluate as rl_eval,
)
from homelabsage.scan_diff import diff, render_markdown

# ─── apprise ────────────────────────────────────────────────────────


def test_apprise_skips_when_disabled():
    cfg = AppriseOutputConfig(enabled=False)
    out = AppriseOutput(cfg)
    item = AnalyzedUpdate(
        update=Update(source="docker", subject="x",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.HIGH, summary="hi"),
    )
    asyncio.run(out.send(item))  # should not raise


def test_apprise_skips_when_urls_empty():
    cfg = AppriseOutputConfig(enabled=True, urls=[])
    out = AppriseOutput(cfg)
    item = AnalyzedUpdate(
        update=Update(source="x", subject="y", current_version="1",
                      new_version="2"),
        analysis=Analysis(severity=Severity.HIGH, summary="x"),
    )
    asyncio.run(out.send(item))


def test_apprise_respects_min_severity():
    cfg = AppriseOutputConfig(enabled=True, urls=["pover://USER@TOKEN"],
                              min_severity="high")
    out = AppriseOutput(cfg)
    item = AnalyzedUpdate(
        update=Update(source="x", subject="y",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.MEDIUM, summary="below"),
    )
    assert not out._should_send(item)


def test_apprise_build_format():
    cfg = AppriseOutputConfig(enabled=True, urls=["pover://x@y"])
    out = AppriseOutput(cfg)
    item = AnalyzedUpdate(
        update=Update(source="docker", subject="mealie",
                      current_version="1", new_version="2",
                      release_url="https://example/release"),
        analysis=Analysis(
            severity=Severity.HIGH, summary="A",
            breaking_changes=["B1", "B2"],
            recommended_action="Do this",
        ),
    )
    title, body = out._build(item)
    assert title == "mealie  1 → 2"
    assert "B1" in body and "Do this" in body
    assert "example/release" in body


def test_apprise_invokes_library_when_present(monkeypatch):
    cfg = AppriseOutputConfig(enabled=True, urls=["pover://USER@TOKEN"])
    out = AppriseOutput(cfg)
    item = AnalyzedUpdate(
        update=Update(source="x", subject="y", current_version="1",
                      new_version="2"),
        analysis=Analysis(severity=Severity.HIGH, summary="m"),
    )
    fake_apprise = MagicMock()
    instance = MagicMock()
    instance.add.return_value = True
    instance.notify.return_value = True
    fake_apprise.Apprise.return_value = instance
    monkeypatch.setattr(
        "homelabsage.outputs.apprise._import_apprise", lambda: fake_apprise,
    )
    asyncio.run(out.send(item))
    instance.add.assert_called_once_with("pover://USER@TOKEN")
    instance.notify.assert_called_once()


# ─── smtp ───────────────────────────────────────────────────────────


def test_smtp_skips_when_missing_required_fields():
    cfg = SMTPOutputConfig(enabled=True, host="", from_addr="", to_addrs=[])
    out = SMTPOutput(cfg)
    item = AnalyzedUpdate(
        update=Update(source="x", subject="y",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.HIGH, summary="hi"),
    )
    assert not out._should_send(item)


def test_smtp_build_message_includes_severity_and_recipient():
    cfg = SMTPOutputConfig(
        enabled=True, host="smtp.example", from_addr="sender@example",
        to_addrs=["a@example", "b@example"], port=587,
    )
    out = SMTPOutput(cfg)
    item = AnalyzedUpdate(
        update=Update(source="docker", subject="mealie",
                      current_version="1", new_version="2",
                      release_url="https://example"),
        analysis=Analysis(
            severity=Severity.CRITICAL, summary="boom",
            breaking_changes=["B1"],
            recommended_action="back up",
        ),
    )
    msg = out._build_message(item, "a@example")
    assert "[HomelabSage]" in msg["Subject"]
    assert "CRITICAL" in msg["Subject"]
    assert msg["From"] == "sender@example"
    assert msg["To"] == "a@example"
    body = msg.get_content()
    assert "B1" in body
    assert "back up" in body


def test_smtp_send_routes_through_to_thread(monkeypatch):
    cfg = SMTPOutputConfig(
        enabled=True, host="smtp.example", from_addr="s@x",
        to_addrs=["a@x"], port=587,
    )
    out = SMTPOutput(cfg)
    item = AnalyzedUpdate(
        update=Update(source="x", subject="y",
                      current_version="1", new_version="2"),
        analysis=Analysis(severity=Severity.HIGH, summary="m"),
    )
    sent: list = []
    def fake_send_sync(it):
        sent.append(it.id)
    monkeypatch.setattr(out, "_send_sync", fake_send_sync)
    asyncio.run(out.send(item))
    assert sent == [item.id]


# ─── resource_limits ────────────────────────────────────────────────


def test_has_memory_limit_positive():
    assert has_memory_limit({"Memory": 512_000_000})
    assert has_memory_limit({"MemoryReservation": 256_000_000})


def test_has_memory_limit_zero_or_missing():
    assert not has_memory_limit({"Memory": 0})
    assert not has_memory_limit({})


def test_has_cpu_limit_nanocpus():
    assert has_cpu_limit({"NanoCpus": 2_000_000_000})


def test_has_cpu_limit_ignores_default_cpushares():
    """CpuShares=1024 is the kernel default — not a real limit."""
    assert not has_cpu_limit({"CpuShares": 1024})


def test_has_cpu_limit_custom_cpushares():
    assert has_cpu_limit({"CpuShares": 512})


def test_evaluate_no_limits_returns_finding():
    attrs = {"Name": "/blog", "HostConfig": {"Memory": 0, "NanoCpus": 0}}
    f = rl_eval(attrs)
    assert isinstance(f, ResourceLimitFinding)
    assert f.container_name == "blog"
    assert not f.has_mem_limit
    assert not f.has_cpu_limit


def test_evaluate_both_limits_returns_none():
    attrs = {"Name": "/blog", "HostConfig": {"Memory": 1_000_000,
                                               "NanoCpus": 1_000_000_000}}
    assert rl_eval(attrs) is None


def test_evaluate_only_mem_limit_emits_finding():
    attrs = {"Name": "/blog", "HostConfig": {"Memory": 1_000_000, "NanoCpus": 0}}
    f = rl_eval(attrs)
    assert f is not None
    assert f.has_mem_limit
    assert not f.has_cpu_limit


# ─── arch_mismatch ──────────────────────────────────────────────────


def test_host_platform_resolves_known_machine(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    assert host_platform() == "linux/amd64"


def test_arch_evaluate_returns_none_when_no_manifests_array():
    assert arch_eval({"schemaVersion": 2, "config": {}}) is None
    assert arch_eval(None) is None


def test_arch_evaluate_finds_mismatch():
    manifest = {
        "manifests": [
            {"platform": {"os": "linux", "architecture": "arm64"}},
            {"platform": {"os": "linux", "architecture": "arm/v7"}},
        ],
    }
    f = arch_eval(manifest, host="linux/amd64")
    assert isinstance(f, ArchFinding)
    assert f.host_arch == "linux/amd64"
    assert "linux/arm64" in f.available
    assert not f.matches


def test_arch_evaluate_returns_none_when_match():
    manifest = {
        "manifests": [
            {"platform": {"os": "linux", "architecture": "amd64"}},
            {"platform": {"os": "linux", "architecture": "arm64"}},
        ],
    }
    assert arch_eval(manifest, host="linux/amd64") is None


def test_arch_evaluate_variant_match():
    """`linux/arm64/v8` covers a host that just declares `linux/arm64`."""
    manifest = {
        "manifests": [
            {"platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
        ],
    }
    # Host declares no variant — variant-suffixed manifest still satisfies
    assert arch_eval(manifest, host="linux/arm64") is None


# ─── scan_diff ──────────────────────────────────────────────────────


def test_scan_diff_added_and_removed():
    old = "id,subject,status,severity,current_version,new_version\nA,a,new,info,1,2\nB,b,new,info,1,2\n"
    new = "id,subject,status,severity,current_version,new_version\nB,b,new,info,1,2\nC,c,new,info,1,2\n"
    d = diff(old, new)
    assert len(d.added) == 1
    assert d.added[0]["id"] == "C"
    assert len(d.removed) == 1
    assert d.removed[0]["id"] == "A"


def test_scan_diff_status_and_severity_changed():
    old = "id,subject,status,severity\nA,a,new,info\nB,b,analyzed,medium\n"
    new = "id,subject,status,severity\nA,a,applied,info\nB,b,analyzed,high\n"
    d = diff(old, new)
    assert any(r["id"] == "A" for r in d.status_changed)
    assert any(r["id"] == "B" for r in d.severity_changed)


def test_scan_diff_empty_is_quiet():
    csv = "id,subject,status,severity\nA,a,new,info\n"
    d = diff(csv, csv)
    assert d.empty()
    body = render_markdown(d)
    assert "No changes" in body


def test_scan_diff_render_markdown_lists_categories():
    old = "id,subject,status,severity\nA,a,new,info\n"
    new = "id,subject,status,severity\nA,a,applied,high\nC,c,new,info\n"
    d = diff(old, new)
    body = render_markdown(d)
    assert "Added" in body
    assert "Status changed" in body
    assert "Severity changed" in body


# ─── stack-health endpoint ──────────────────────────────────────────


def _bare_app(tmp_path: Path):
    body = (
        "llm:\n  provider: disabled\n  model: stub\n  endpoint: http://stub\n"
        "scheduler:\n  enabled: false\n"
        f"storage:\n  database_path: {tmp_path}/state.sqlite\n"
        "web:\n  enabled: true\n  host: 127.0.0.1\n  port: 0\n"
        "  auth:\n    enabled: false\n"
    )
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(body)
    cfg = load_config(cfg_path)
    return web.create_app(cfg, cfg_path=cfg_path)


def test_stack_health_returns_full_shape(tmp_path: Path):
    app = _bare_app(tmp_path)
    with TestClient(app) as client:
        r = client.get("/api/stack-health")
        assert r.status_code == 200
        body = r.json()
        for key in (
            "version", "audit", "updates", "parity", "pending_dispatches",
            "heartbeat_24h", "backup_health", "post_update_failures",
        ):
            assert key in body
        assert "counts_by_status" in body["updates"]
        assert "active" in body["parity"]


def test_stack_health_bypasses_auth(tmp_path: Path):
    """When auth is enabled, /api/stack-health stays open (like /widget/*)."""
    body = (
        "llm:\n  provider: disabled\n  model: stub\n  endpoint: http://stub\n"
        "scheduler:\n  enabled: false\n"
        f"storage:\n  database_path: {tmp_path}/state.sqlite\n"
        "web:\n  enabled: true\n  host: 127.0.0.1\n  port: 0\n"
        "  auth:\n    enabled: true\n    username: javi\n"
        "    password: secret\n"
    )
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(body)
    cfg = load_config(cfg_path)
    app = web.create_app(cfg, cfg_path=cfg_path)
    with TestClient(app) as client:
        r = client.get("/api/stack-health")
        assert r.status_code == 200


# ─── /profile page ──────────────────────────────────────────────────


def test_profile_page_renders(tmp_path: Path):
    app = _bare_app(tmp_path)
    with TestClient(app) as client:
        r = client.get("/profile")
        assert r.status_code == 200
        assert "Docker plugin" in r.text or "docker plugin" in r.text


def test_profile_api_lists_signals(tmp_path: Path):
    app = _bare_app(tmp_path)
    with TestClient(app) as client:
        r = client.get("/api/profile")
        assert r.status_code == 200
        body = r.json()
        assert "rows" in body
        assert all(isinstance(row, dict) and "label" in row for row in body["rows"])


# ─── MCP analyze_url + csi tools ────────────────────────────────────


def test_mcp_registers_new_tools():
    from homelabsage.mcp import TOOLS
    assert "analyze_url" in TOOLS
    assert "csi" in TOOLS


def test_mcp_analyze_url_requires_param(tmp_path: Path):
    from homelabsage.mcp import dispatch
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    from homelabsage.db import Database
    db = Database(cfg.storage.database_path)
    try:
        resp = dispatch(cfg, db, {
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "analyze_url", "arguments": {}},
        })
        assert resp["error"]["code"] == -32602
        assert "url" in resp["error"]["message"]
    finally:
        db.close()


def test_mcp_analyze_url_returns_unmatched_for_unknown(tmp_path: Path, monkeypatch):
    from homelabsage.mcp import dispatch
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    from homelabsage.db import Database
    db = Database(cfg.storage.database_path)
    try:
        # Patch the article fetcher to return None so the article path
        # bails out without making real HTTP calls.
        async def fake_fetch(url, *, timeout=20.0):
            return None
        monkeypatch.setattr("homelabsage.analyse_url._fetch_article", fake_fetch)
        resp = dispatch(cfg, db, {
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {
                "name": "analyze_url",
                "arguments": {"url": "https://example.com/no-match"},
            },
        })
        data = resp["result"]["structuredContent"]
        assert data["matched"] is False
    finally:
        db.close()
