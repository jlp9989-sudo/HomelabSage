"""Tests for v0.6.8: container disappearance, DNS check, MCP additions.

Snooze tests moved to test_snooze.py in v0.11.6.
"""

from __future__ import annotations

import socket

import pytest

from homelabsage.config import Config
from homelabsage.dns_check import DNSFinding, check_hostnames
from homelabsage.dns_check import probe as probe_dns

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
