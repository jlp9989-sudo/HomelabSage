"""Tests for the CVE adapter (trivy/grype)."""

from __future__ import annotations

import json

from homelabsage import cve as cve_mod
from homelabsage.cve import (
    scan_grype,
    scan_image,
    scan_trivy,
)

# ─── helpers ─────────────────────────────────────────────────────────────


def _patch_run_json(monkeypatch, callback):
    """Replace `cve._run_json` with `callback(cmd, timeout) -> payload`."""
    monkeypatch.setattr(cve_mod, "_run_json", lambda cmd, *, timeout: callback(cmd, timeout))


# ─── trivy ───────────────────────────────────────────────────────────────


def test_scan_trivy_returns_none_when_run_returns_none(monkeypatch):
    _patch_run_json(monkeypatch, lambda cmd, t: None)
    assert scan_trivy("nginx:latest") is None


def test_scan_trivy_parses_critical_and_high(monkeypatch):
    payload = {
        "Results": [
            {"Vulnerabilities": [
                {"Severity": "CRITICAL", "VulnerabilityID": "CVE-2026-0001"},
                {"Severity": "CRITICAL", "VulnerabilityID": "CVE-2026-0002"},
                {"Severity": "HIGH", "VulnerabilityID": "CVE-2026-0010"},
                # dedup: same CVE-0001 reported by two packages
                {"Severity": "CRITICAL", "VulnerabilityID": "CVE-2026-0001"},
            ]},
            {"Vulnerabilities": [
                {"Severity": "MEDIUM", "VulnerabilityID": "CVE-2026-0050"},
            ]},
        ],
    }
    _patch_run_json(monkeypatch, lambda cmd, t: payload)
    summary = scan_trivy("nginx:latest")
    assert summary is not None
    assert summary.scanner == "trivy"
    assert summary.counts["critical"] == 3
    assert summary.counts["high"] == 1
    assert summary.counts["medium"] == 1
    # Dedup applied to top_*
    assert summary.top_critical == ["CVE-2026-0001", "CVE-2026-0002"]
    assert summary.top_high == ["CVE-2026-0010"]


def test_scan_trivy_handles_empty_findings(monkeypatch):
    _patch_run_json(monkeypatch, lambda cmd, t: {"Results": []})
    summary = scan_trivy("nginx:clean")
    assert summary is not None and summary.total == 0
    assert summary.top_critical == []


def test_to_context_caps_id_list_at_ten(monkeypatch):
    payload = {"Results": [{"Vulnerabilities": [
        {"Severity": "CRITICAL", "VulnerabilityID": f"CVE-2026-{i:04d}"}
        for i in range(20)
    ]}]}
    _patch_run_json(monkeypatch, lambda cmd, t: payload)
    summary = scan_trivy("nginx:latest")
    assert summary is not None
    ctx = summary.to_context()
    assert len(ctx["top_critical"]) == 10
    assert ctx["counts"]["critical"] == 20


# ─── grype ───────────────────────────────────────────────────────────────


def test_scan_grype_parses_matches(monkeypatch):
    payload = {
        "matches": [
            {"vulnerability": {"severity": "Critical", "id": "GHSA-aaaa"}},
            {"vulnerability": {"severity": "High", "id": "GHSA-bbbb"}},
            {"vulnerability": {"severity": "Negligible", "id": "x"}},
        ]
    }
    _patch_run_json(monkeypatch, lambda cmd, t: payload)
    s = scan_grype("redis:latest")
    assert s is not None
    assert s.scanner == "grype"
    assert s.counts["critical"] == 1
    assert s.counts["high"] == 1
    # Severities outside our gate (negligible) don't bump counts
    assert s.counts["medium"] == 0
    assert s.top_critical == ["GHSA-aaaa"]


# ─── fallback chain ──────────────────────────────────────────────────────


def test_scan_image_tries_trivy_first_then_grype(monkeypatch):
    calls: list[str] = []

    def _stub(cmd, t):
        calls.append(cmd[0])
        if cmd[0] == "trivy":
            return None
        return {"matches": [
            {"vulnerability": {"severity": "high", "id": "X-1"}},
        ]}

    _patch_run_json(monkeypatch, _stub)
    summary = scan_image("nginx")
    assert summary is not None
    assert summary.scanner == "grype"
    assert calls == ["trivy", "grype"]


def test_scan_image_returns_none_when_neither_tool_available(monkeypatch):
    _patch_run_json(monkeypatch, lambda cmd, t: None)
    assert scan_image("nginx") is None


# ─── _run_json defensive behaviour ──────────────────────────────────────


def test_run_json_returns_none_when_binary_absent(monkeypatch):
    monkeypatch.setattr(cve_mod.shutil, "which", lambda name: None)
    assert cve_mod._run_json(["nonsense-tool", "x"], timeout=1.0) is None


def test_run_json_returns_none_on_malformed_json(monkeypatch):
    monkeypatch.setattr(cve_mod.shutil, "which", lambda name: "/usr/bin/echo")

    class _R:
        returncode = 0
        stdout = "not JSON at all"
        stderr = ""

    monkeypatch.setattr(cve_mod.subprocess, "run", lambda *a, **kw: _R())
    assert cve_mod._run_json(["echo", "x"], timeout=1.0) is None


def test_run_json_accepts_trivy_exit_1_as_findings_present(monkeypatch):
    """Trivy uses exit 1 to mean 'scan OK, findings exist' — that's not failure."""
    monkeypatch.setattr(cve_mod.shutil, "which", lambda name: "/usr/bin/trivy")

    class _R:
        returncode = 1
        stdout = json.dumps({"Results": [], "Findings": True})
        stderr = ""

    monkeypatch.setattr(cve_mod.subprocess, "run", lambda *a, **kw: _R())
    out = cve_mod._run_json(["trivy", "image", "x"], timeout=10.0)
    assert isinstance(out, dict)
