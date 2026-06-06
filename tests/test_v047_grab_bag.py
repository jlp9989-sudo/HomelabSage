"""Tests for the v0.4.7 grab-bag: sidecars, watched_enrich, heartbeats."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

from homelabsage.db import Database
from homelabsage.sidecars import (
    SidecarLink,
    _network_primary,
    _pid_primary,
    discover_runtime,
    sidecars_for,
)
from homelabsage.watched_enrich import _decode_readme_body, enrich

# ─── sidecars ──────────────────────────────────────────────────────


def test_network_primary_from_attrs():
    attrs = {"HostConfig": {"NetworkMode": "container:gluetun"}}
    assert _network_primary(attrs) == "gluetun"


def test_network_primary_strips_leading_slash():
    attrs = {"HostConfig": {"NetworkMode": "container:/gluetun"}}
    assert _network_primary(attrs) == "gluetun"


def test_network_primary_returns_none_for_bridge():
    assert _network_primary({"HostConfig": {"NetworkMode": "bridge"}}) is None


def test_pid_primary_extracts():
    attrs = {"HostConfig": {"PidMode": "container:db"}}
    assert _pid_primary(attrs) == "db"


def _fake_container(name: str, *, network_primary: str | None = None,
                    pid_primary: str | None = None, id_: str = "abcdef123456ff"):
    c = MagicMock()
    c.name = name
    c.id = id_
    nm = f"container:{network_primary}" if network_primary else "bridge"
    pm = f"container:{pid_primary}" if pid_primary else ""
    c.attrs = {"HostConfig": {"NetworkMode": nm, "PidMode": pm}}
    return c


def test_discover_runtime_emits_network_links():
    qbit = _fake_container("qbittorrent", network_primary="gluetun")
    gluetun = _fake_container("gluetun")
    links = discover_runtime([qbit, gluetun])
    assert len(links) == 1
    assert links[0] == SidecarLink(
        sidecar="qbittorrent", primary="gluetun", relation="network",
    )


def test_discover_runtime_skips_self_reference():
    """container:<own-name> should not produce a link."""
    self_ref = _fake_container("x", network_primary="x")
    assert discover_runtime([self_ref]) == []


def test_discover_runtime_emits_both_network_and_pid():
    qbit = _fake_container("qbit", network_primary="gluetun", pid_primary="db")
    discover = discover_runtime([qbit, _fake_container("gluetun"), _fake_container("db")])
    rels = sorted(link.relation for link in discover)
    assert rels == ["network", "pid"]


def test_discover_runtime_dedups_when_same_primary():
    """net + pid pointing at the same primary → only one link."""
    same = _fake_container("x", network_primary="parent", pid_primary="parent")
    links = discover_runtime([same, _fake_container("parent")])
    assert len(links) == 1
    assert links[0].relation == "network"


def test_sidecars_for_filters_to_primary():
    links = [
        SidecarLink("a", "primary", "network"),
        SidecarLink("b", "other", "network"),
        SidecarLink("c", "primary", "pid"),
    ]
    matches = sidecars_for("primary", links)
    assert len(matches) == 2
    assert {link.sidecar for link in matches} == {"a", "c"}


def test_sidecar_link_to_context():
    link = SidecarLink("vpn", "qbit", "network")
    assert link.to_context() == {
        "sidecar": "vpn", "primary": "qbit", "relation": "network",
    }


# ─── watched_enrich ────────────────────────────────────────────────


def test_decode_readme_base64():
    """GitHub-style base64 README payload."""
    import base64
    body = "# my project\nshort description"
    enc = base64.b64encode(body.encode("utf-8")).decode("ascii")
    payload = {"content": enc, "encoding": "base64"}
    assert _decode_readme_body(payload) == body


def test_decode_readme_plain():
    payload = {"content": "plain text body", "encoding": "utf-8"}
    assert _decode_readme_body(payload) == "plain text body"


def test_decode_readme_garbage_returns_empty():
    assert _decode_readme_body({"content": "@@@ not base64 @@@",
                                 "encoding": "base64"}) == ""


def test_decode_readme_missing_content():
    assert _decode_readme_body({}) == ""


def test_enrich_returns_defaults_on_404(monkeypatch):
    from homelabsage import watched_enrich
    async def fake_fetch(url, *, headers, timeout=15.0):
        return None
    monkeypatch.setattr(watched_enrich, "_fetch_json", fake_fetch)
    res = asyncio.run(enrich("owner/repo"))
    assert res.topics == []
    assert res.readme_excerpt == ""
    assert res.homepage is None


def test_enrich_pulls_topics_and_readme(monkeypatch):
    """Both fetches succeed → enrichment is populated."""
    import base64

    from homelabsage import watched_enrich

    async def fake_fetch(url, *, headers, timeout=15.0):
        if url.endswith("/readme"):
            return {
                "content": base64.b64encode(b"# Cool\nIt does stuff.").decode(),
                "encoding": "base64",
            }
        return {
            "topics": ["selfhosted", "rust", "cli"],
            "homepage": "https://example.com",
        }

    monkeypatch.setattr(watched_enrich, "_fetch_json", fake_fetch)
    res = asyncio.run(enrich("owner/repo"))
    assert res.topics == ["selfhosted", "rust", "cli"]
    assert "Cool" in res.readme_excerpt
    assert res.homepage == "https://example.com"


def test_enrich_readme_capped_at_budget(monkeypatch):
    import base64

    from homelabsage import watched_enrich

    big = "x" * 8000
    async def fake_fetch(url, *, headers, timeout=15.0):
        if url.endswith("/readme"):
            return {"content": base64.b64encode(big.encode()).decode(),
                    "encoding": "base64"}
        return {"topics": []}

    monkeypatch.setattr(watched_enrich, "_fetch_json", fake_fetch)
    res = asyncio.run(enrich("owner/repo", budget=500))
    assert len(res.readme_excerpt) == 500


# ─── heartbeats ────────────────────────────────────────────────────


def test_record_heartbeat_persists_success(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        db.record_heartbeat(
            url="http://kuma/api/push/x", ok=True,
            status_code=200, duration_ms=42,
        )
        rows = db.list_recent_heartbeats()
        assert len(rows) == 1
        assert rows[0]["ok"] == 1
        assert rows[0]["status_code"] == 200
    finally:
        db.close()


def test_record_heartbeat_persists_failure_with_error(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        db.record_heartbeat(
            url="http://kuma/", ok=False, error="connection refused",
        )
        rows = db.list_recent_heartbeats()
        assert rows[0]["ok"] == 0
        assert "connection refused" in rows[0]["error"]
    finally:
        db.close()


def test_heartbeat_summary_counts(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        db.record_heartbeat(url="x", ok=True)
        db.record_heartbeat(url="x", ok=True)
        db.record_heartbeat(url="x", ok=False, error="boom")
        summary = db.heartbeat_summary()
        assert summary["succeeded"] == 2
        assert summary["failed"] == 1
        assert summary["last_ok_at"] is not None
        assert summary["last_failure_at"] is not None
    finally:
        db.close()


def test_heartbeat_summary_empty_window(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        summary = db.heartbeat_summary()
        assert summary["succeeded"] == 0
        assert summary["failed"] == 0
        assert summary["last_ok_at"] is None
    finally:
        db.close()


def test_heartbeat_error_truncated_to_500_chars(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        db.record_heartbeat(url="x", ok=False, error="x" * 5000)
        rows = db.list_recent_heartbeats()
        assert len(rows[0]["error"]) == 500
    finally:
        db.close()
