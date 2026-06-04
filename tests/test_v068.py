"""Tests for v0.6.8: snooze, container disappearance, DNS check, MCP additions."""

from __future__ import annotations

import socket

import pytest

from homelabsage.config import Config
from homelabsage.disappearance import (
    DisappearanceFinding,
    diff_snapshots,
    load_snapshot,
    save_snapshot,
)
from homelabsage.dns_check import DNSFinding, check_hostnames
from homelabsage.dns_check import probe as probe_dns

# ─── snooze ────────────────────────────────────────────────────────


def test_snooze_set_and_get(tmp_path):
    from homelabsage.db import Database
    from homelabsage.models import AnalyzedUpdate, Update
    db = Database(str(tmp_path / "t.db"))
    item = AnalyzedUpdate(
        update=Update(
            source="x", subject="y",
            current_version="1", new_version="2",
        ),
    )
    db.upsert(item)
    assert db.get_snooze(item.id) is None
    assert db.set_snooze(item.id, "2026-12-31T00:00:00+00:00") is True
    assert db.get_snooze(item.id) == "2026-12-31T00:00:00+00:00"


def test_snooze_clear(tmp_path):
    from homelabsage.db import Database
    from homelabsage.models import AnalyzedUpdate, Update
    db = Database(str(tmp_path / "t.db"))
    item = AnalyzedUpdate(
        update=Update(
            source="x", subject="y",
            current_version="1", new_version="2",
        ),
    )
    db.upsert(item)
    db.set_snooze(item.id, "2026-12-31T00:00:00+00:00")
    assert db.set_snooze(item.id, None) is True
    assert db.get_snooze(item.id) is None


def test_snooze_unknown_update_returns_false(tmp_path):
    from homelabsage.db import Database
    db = Database(str(tmp_path / "t.db"))
    assert db.set_snooze("ghost-id", "2026-12-31T00:00:00+00:00") is False
    assert db.get_snooze("ghost-id") is None


def test_snooze_api_validates_iso8601(tmp_path):
    from fastapi.testclient import TestClient

    from homelabsage.db import Database
    from homelabsage.models import AnalyzedUpdate, Update
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    db = Database(cfg.storage.database_path)
    item = AnalyzedUpdate(
        update=Update(
            source="x", subject="y",
            current_version="1", new_version="2",
        ),
    )
    db.upsert(item)
    app = create_app(cfg)
    client = TestClient(app)
    r = client.post(
        f"/api/updates/{item.id}/snooze",
        json={"snooze_until": "not-a-date"},
    )
    assert r.status_code == 400
    r = client.post(
        f"/api/updates/{item.id}/snooze",
        json={"snooze_until": "2026-12-31T00:00:00+00:00"},
    )
    assert r.status_code == 200
    assert r.json()["snooze_until"] == "2026-12-31T00:00:00+00:00"


# ─── disappearance ────────────────────────────────────────────────


def test_disappearance_diff_returns_missing():
    prev = {"a": "2026-06-01T00:00:00Z", "b": "2026-06-01T00:00:00Z"}
    out = diff_snapshots(prev, {"a"})
    assert len(out) == 1
    assert out[0].name == "b"
    assert out[0].last_seen_at == "2026-06-01T00:00:00Z"


def test_disappearance_diff_no_changes():
    prev = {"a": "2026-06-01T00:00:00Z"}
    assert diff_snapshots(prev, {"a"}) == []


def test_disappearance_diff_extra_in_current_ignored():
    prev = {"a": "t"}
    assert diff_snapshots(prev, {"a", "b"}) == []


def test_disappearance_save_and_load_roundtrip(tmp_path):
    snap_path = tmp_path / "snap.json"
    save_snapshot(snap_path, {"a", "b"}, "2026-06-04T00:00:00Z")
    loaded = load_snapshot(snap_path)
    assert loaded == {
        "a": "2026-06-04T00:00:00Z",
        "b": "2026-06-04T00:00:00Z",
    }


def test_disappearance_load_corrupt_returns_empty(tmp_path):
    p = tmp_path / "snap.json"
    p.write_text("not json")
    assert load_snapshot(p) == {}


def test_disappearance_load_missing_returns_empty(tmp_path):
    assert load_snapshot(tmp_path / "nope.json") == {}


def test_disappearance_to_context_shape():
    d = DisappearanceFinding(name="x", last_seen_at="t")
    assert d.to_context() == {"name": "x", "last_seen_at": "t"}


# ─── DNS check ────────────────────────────────────────────────────


def test_dns_probe_resolves_localhost():
    """localhost is universally resolvable; success returns None."""
    assert probe_dns("localhost") is None


def test_dns_probe_nxdomain_returns_finding(monkeypatch):
    def boom(*a, **kw):
        raise socket.gaierror("nodename nor servname provided")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    out = probe_dns("nonexistent.invalid.")
    assert out is not None
    assert out.severity == "medium"
    assert "gaierror" in out.error


def test_dns_probe_empty_hostname_returns_none():
    assert probe_dns("") is None
    assert probe_dns("   ") is None


def test_dns_check_hostnames_filters_failures(monkeypatch):
    calls = []
    def fake(host, port):
        calls.append(host)
        if host == "bad.invalid":
            raise socket.gaierror("nope")
        return [("AF_INET", None, None, "", ("1.2.3.4", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    out = check_hostnames(["good.example", "bad.invalid"])
    assert len(out) == 1
    assert out[0].hostname == "bad.invalid"


def test_dns_finding_to_context_shape():
    f = DNSFinding(hostname="x.example", error="boom", severity="medium")
    assert f.to_context() == {
        "hostname": "x.example", "error": "boom", "severity": "medium",
    }


# ─── MCP tools ────────────────────────────────────────────────────


def test_mcp_snooze_update_validates_iso(tmp_path):
    from homelabsage.db import Database
    from homelabsage.mcp import TOOLS
    from homelabsage.models import AnalyzedUpdate, Update
    db = Database(str(tmp_path / "t.db"))
    item = AnalyzedUpdate(
        update=Update(
            source="x", subject="y",
            current_version="1", new_version="2",
        ),
    )
    db.upsert(item)
    cfg = Config()
    impl = TOOLS["snooze_update"]["impl"]
    bad = impl(cfg, db, {"update_id": item.id, "snooze_until": "garbage"})
    assert bad["ok"] is False
    ok = impl(cfg, db, {
        "update_id": item.id,
        "snooze_until": "2026-12-31T00:00:00+00:00",
    })
    assert ok["ok"] is True
    assert ok["snooze_until"] == "2026-12-31T00:00:00+00:00"


def test_mcp_snooze_update_clears_with_null(tmp_path):
    from homelabsage.db import Database
    from homelabsage.mcp import TOOLS
    from homelabsage.models import AnalyzedUpdate, Update
    db = Database(str(tmp_path / "t.db"))
    item = AnalyzedUpdate(
        update=Update(
            source="x", subject="y",
            current_version="1", new_version="2",
        ),
    )
    db.upsert(item)
    db.set_snooze(item.id, "2026-12-31T00:00:00+00:00")
    cfg = Config()
    impl = TOOLS["snooze_update"]["impl"]
    out = impl(cfg, db, {"update_id": item.id, "snooze_until": None})
    assert out["ok"] is True
    assert out["snooze_until"] is None


def test_mcp_snooze_update_unknown_id(tmp_path):
    from homelabsage.db import Database
    from homelabsage.mcp import TOOLS
    db = Database(str(tmp_path / "t.db"))
    cfg = Config()
    impl = TOOLS["snooze_update"]["impl"]
    out = impl(cfg, db, {"update_id": "ghost",
                         "snooze_until": "2026-12-31T00:00:00+00:00"})
    assert out["ok"] is False


def test_mcp_recurring_failures_empty(tmp_path):
    from homelabsage.db import Database
    from homelabsage.mcp import TOOLS
    db = Database(str(tmp_path / "t.db"))
    cfg = Config()
    impl = TOOLS["recurring_failures"]["impl"]
    out = impl(cfg, db, {})
    assert out["count"] == 0


def test_mcp_dns_check_no_hostnames():
    from homelabsage.db import Database
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["dns_check"]["impl"]
    out = impl(cfg, db, {})
    assert out["count"] == 0
    assert "no hostnames" in out["reason"]


def test_mcp_dns_check_with_explicit_hostnames(monkeypatch):
    from homelabsage.db import Database
    from homelabsage.mcp import TOOLS
    def boom(*a, **kw):
        raise socket.gaierror("nope")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["dns_check"]["impl"]
    out = impl(cfg, db, {"hostnames": ["bad.invalid"]})
    assert out["count"] == 1


# Keep `pytest` referenced so the linter is happy in case nothing imports it.
_ = pytest
