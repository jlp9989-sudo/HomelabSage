"""Tests for v0.7.4: /api/audit/history + audit-alert webhook + doctor --watch."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from homelabsage.audit_alert import _meets_floor, fire_if_new
from homelabsage.audit_history import FILE_NAME, append, list_history
from homelabsage.config import AuditAlertsConfig, Config

# ─── list_history ──────────────────────────────────────────────────


def _report(findings: list[dict], *, generated_at: str = "2026-06-04T12:00:00Z"):
    return {
        "generated_at": generated_at,
        "healthy": not findings,
        "counts_by_severity": {"medium": len(findings)},
        "counts_by_category": {"cve": 1} if findings else {},
        "findings": findings,
    }


def test_list_history_empty(tmp_path):
    assert list_history(tmp_path / "notes") == []


def test_list_history_newest_first(tmp_path):
    notes = tmp_path / "notes"
    append(notes, _report([], generated_at="2026-06-01T00:00:00Z"))
    append(notes, _report([], generated_at="2026-06-02T00:00:00Z"))
    append(notes, _report([], generated_at="2026-06-03T00:00:00Z"))
    out = list_history(notes)
    assert [r["generated_at"] for r in out] == [
        "2026-06-03T00:00:00Z",
        "2026-06-02T00:00:00Z",
        "2026-06-01T00:00:00Z",
    ]


def test_list_history_pagination(tmp_path):
    notes = tmp_path / "notes"
    for i in range(5):
        append(notes, _report([], generated_at=f"2026-06-0{i+1}T00:00:00Z"))
    page1 = list_history(notes, limit=2, offset=0)
    page2 = list_history(notes, limit=2, offset=2)
    assert [r["generated_at"] for r in page1] == [
        "2026-06-05T00:00:00Z", "2026-06-04T00:00:00Z",
    ]
    assert [r["generated_at"] for r in page2] == [
        "2026-06-03T00:00:00Z", "2026-06-02T00:00:00Z",
    ]


def test_list_history_returns_compact_summary(tmp_path):
    notes = tmp_path / "notes"
    append(notes, _report([{
        "category": "cve", "source_kind": "cve", "source_ref": "x",
        "severity": "high", "title": "t", "detail": "d", "cite": "c",
    }]))
    out = list_history(notes)
    row = out[0]
    assert "findings" not in row    # full list never returned
    assert row["finding_count"] == 1
    assert row["counts_by_severity"] == {"medium": 1}


def test_list_history_skips_corrupt_lines(tmp_path):
    notes = tmp_path / "notes"
    append(notes, _report([], generated_at="2026-06-01T00:00:00Z"))
    # Manually corrupt the file with a junk line in the middle
    p = notes / FILE_NAME
    body = p.read_text()
    p.write_text(body + "not-json\n" + body)
    out = list_history(notes)
    # 2 valid rows, junk skipped
    assert len(out) == 2


# ─── /api/audit/history ───────────────────────────────────────────


def test_api_audit_history_empty(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    cfg.notes.notes_dir = str(tmp_path / "notes")
    client = TestClient(create_app(cfg))
    r = client.get("/api/audit/history")
    assert r.status_code == 200
    assert r.json() == {"count": 0, "items": []}


def test_api_audit_history_clamps_limit(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    cfg.notes.notes_dir = str(tmp_path / "notes")
    client = TestClient(create_app(cfg))
    r = client.get("/api/audit/history?limit=999999")
    assert r.status_code == 200


# ─── audit_alert ──────────────────────────────────────────────────


def test_audit_alert_meets_floor_buckets():
    findings = [{"severity": "info"}, {"severity": "medium"}]
    assert _meets_floor(findings, "info") is True
    assert _meets_floor(findings, "medium") is True
    assert _meets_floor(findings, "high") is False


def test_audit_alert_fires_when_new_severe_finding(monkeypatch):
    calls: list[dict] = []

    class _R:
        status_code = 200
        def raise_for_status(self): pass

    class _Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None, **kw):
            calls.append({"url": url, "payload": json})
            return _R()

    monkeypatch.setattr(httpx, "Client", _Client)
    n = fire_if_new(
        webhook_urls=["https://example/webhook"],
        min_severity="high",
        timeout=1.0,
        diff={
            "new": [{
                "category": "cve", "severity": "critical",
                "title": "x", "source_kind": "cve",
                "source_ref": "y", "cite": "z",
            }],
            "resolved": [],
        },
        counts_by_severity={"critical": 1},
        generated_at="2026-06-04T12:00:00Z",
    )
    assert n == 1
    assert calls[0]["payload"]["type"] == "audit_diff"
    assert calls[0]["payload"]["new"][0]["severity"] == "critical"


def test_audit_alert_skips_below_floor(monkeypatch):
    """Medium new findings don't fire when floor=high."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: None)
    n = fire_if_new(
        webhook_urls=["https://example/webhook"],
        min_severity="high",
        timeout=1.0,
        diff={
            "new": [{"severity": "medium"}],
            "resolved": [],
        },
        counts_by_severity={},
        generated_at="t",
    )
    assert n == 0


def test_audit_alert_skips_when_no_new_findings(monkeypatch):
    """Only resolved? No fire — resolution isn't an alert."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: None)
    n = fire_if_new(
        webhook_urls=["https://example/webhook"],
        min_severity="info",
        timeout=1.0,
        diff={"new": [], "resolved": [{"category": "x"}]},
        counts_by_severity={},
        generated_at="t",
    )
    assert n == 0


def test_audit_alert_handles_per_url_failure(monkeypatch):
    """One bad URL must not block the rest."""
    class _GoodResp:
        status_code = 200
        def raise_for_status(self): pass

    class _Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None, **kw):
            if "bad" in url:
                raise httpx.ConnectError("nope")
            return _GoodResp()

    monkeypatch.setattr(httpx, "Client", _Client)
    n = fire_if_new(
        webhook_urls=["https://bad", "https://good"],
        min_severity="info",
        timeout=1.0,
        diff={"new": [{"severity": "medium"}], "resolved": []},
        counts_by_severity={"medium": 1},
        generated_at="t",
    )
    assert n == 1


def test_audit_alerts_config_defaults():
    c = AuditAlertsConfig()
    assert c.enabled is False
    assert c.webhook_urls == []
    assert c.min_severity == "medium"


# doctor --watch test moved to test_doctor.py in v0.11.7.
