"""Audit history + diff + prune + alerts + filter CLI flags.

Consolidates the audit-history surface from 5 release-pinned files in
v0.11.8:
  - v0.7.3: history append/load/diff + /api/audit/diff
  - v0.7.4: list_history + /api/audit/history + audit_alert webhook
  - v0.7.5: MCP audit_history + audit_diff + audit --severity filter
  - v0.7.6: audit_history.prune + audit-prune CLI
  - v0.7.7: MCP audit_prune + /api/audit/history.jsonl + audit --diff-only

Does NOT cover: audit-mute (test_v081, v082), audit-categories
(test_v083, v091), or the audit detector unit tests (test_audit.py).
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from homelabsage.audit_alert import _meets_floor, fire_if_new
from homelabsage.audit_history import (
    FILE_NAME,
    append,
    diff_against_latest,
    list_history,
    load_latest,
    prune,
)
from homelabsage.cli import app
from homelabsage.config import AuditAlertsConfig, Config
from homelabsage.db import Database

# ─── shared helpers ───────────────────────────────────────────────


def _report(findings: list[dict] | None = None, *,
            generated_at: str = "2026-06-04T12:00:00Z"):
    fs = findings or []
    return {
        "generated_at": generated_at,
        "healthy": not fs,
        "counts_by_severity": {"medium": len(fs)} if fs else {},
        "counts_by_category": {},
        "findings": fs,
    }


def _finding(category="cve", source_ref="ref1", severity="high"):
    return {
        "category": category, "source_kind": category,
        "source_ref": source_ref, "severity": severity,
        "title": "t", "detail": "d", "cite": "c",
    }


def _config_yaml(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    db_path = tmp_path / "h.db"
    notes_dir = tmp_path / "notes"
    cfg_path.write_text(
        f"""storage:
  database_path: {db_path}
llm:
  endpoint: ""
notes:
  notes_dir: {notes_dir}
""",
    )
    return cfg_path, db_path, notes_dir


# ─── audit_history: append / load / list ─────────────────────────


def test_history_append_creates_file(tmp_path):
    notes = tmp_path / "notes"
    out = append(notes, _report([{
        "category": "cve",
        "source_kind": "cve",
        "source_ref": "mealie@1.2.3",
        "severity": "high",
        "title": "x", "detail": "y", "cite": "z",
    }]))
    assert out is not None
    assert (notes / FILE_NAME).exists()


def test_history_append_is_truly_append_only(tmp_path):
    notes = tmp_path / "notes"
    append(notes, _report([], generated_at="2026-06-01T00:00:00Z"))
    append(notes, _report([], generated_at="2026-06-02T00:00:00Z"))
    contents = (notes / FILE_NAME).read_text().splitlines()
    assert len(contents) == 2


def test_history_append_handles_unwritable_dir(monkeypatch, tmp_path):
    notes = tmp_path / "notes"
    # Pre-create as a file so mkdir(parents=True, exist_ok=True) fails
    notes.write_text("")
    out = append(notes, _report([]))
    assert out is None


def test_history_load_latest_returns_last_row(tmp_path):
    notes = tmp_path / "notes"
    append(notes, _report([], generated_at="2026-06-01T00:00:00Z"))
    append(notes, _report([{
        "category": "x", "source_kind": "y", "source_ref": "z",
        "severity": "medium", "title": "a", "detail": "b", "cite": "c",
    }], generated_at="2026-06-02T00:00:00Z"))
    latest = load_latest(notes)
    assert latest is not None
    assert latest.generated_at == "2026-06-02T00:00:00Z"
    assert ("x", "y", "z") in latest.finding_keys


def test_history_load_latest_missing_returns_none(tmp_path):
    assert load_latest(tmp_path / "no_dir") is None


def test_history_load_latest_corrupt_skipped(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / FILE_NAME).write_text("not json\n")
    assert load_latest(notes) is None


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


# ─── audit_history: diff ──────────────────────────────────────────


def test_history_diff_first_run_marks_all_new(tmp_path):
    notes = tmp_path / "notes"
    current = _report([{
        "category": "cve", "source_kind": "cve", "source_ref": "id1",
        "severity": "high", "title": "x", "detail": "y", "cite": "z",
    }])
    diff = diff_against_latest(notes, current)
    assert diff["previous_snapshot"] is None
    assert len(diff["new"]) == 1
    assert diff["resolved"] == []


def test_history_diff_resolves_and_new(tmp_path):
    notes = tmp_path / "notes"
    prev = _report([
        {"category": "a", "source_kind": "a", "source_ref": "old",
         "severity": "medium", "title": "x", "detail": "y", "cite": "z"},
        {"category": "b", "source_kind": "b", "source_ref": "still",
         "severity": "medium", "title": "x", "detail": "y", "cite": "z"},
    ])
    append(notes, prev)
    current = _report([
        {"category": "b", "source_kind": "b", "source_ref": "still",
         "severity": "medium", "title": "x", "detail": "y", "cite": "z"},
        {"category": "c", "source_kind": "c", "source_ref": "new",
         "severity": "medium", "title": "x", "detail": "y", "cite": "z"},
    ])
    diff = diff_against_latest(notes, current)
    assert {f["source_ref"] for f in diff["new"]} == {"new"}
    assert {f["source_ref"] for f in diff["resolved"]} == {"old"}


# ─── audit_history: prune ─────────────────────────────────────────


def test_prune_drops_oldest(tmp_path):
    notes = tmp_path / "notes"
    for i in range(10):
        append(notes, _report(generated_at=f"2026-06-{i+1:02d}T00:00:00Z"))
    dropped = prune(notes, keep_last=3)
    assert dropped == 7
    body = (notes / FILE_NAME).read_text().strip().splitlines()
    assert len(body) == 3


def test_prune_noop_when_within_cap(tmp_path):
    notes = tmp_path / "notes"
    append(notes, _report())
    assert prune(notes, keep_last=10) == 0


def test_prune_missing_file_returns_zero(tmp_path):
    assert prune(tmp_path / "missing", keep_last=10) == 0


def test_prune_keep_zero_truncates(tmp_path):
    notes = tmp_path / "notes"
    for i in range(3):
        append(notes, _report(generated_at=f"2026-06-{i+1:02d}T00:00:00Z"))
    dropped = prune(notes, keep_last=0)
    assert dropped == 3
    assert (notes / FILE_NAME).read_text() == ""


def test_prune_rejects_negative_keep(tmp_path):
    notes = tmp_path / "notes"
    append(notes, _report())
    assert prune(notes, keep_last=-1) == 0


def test_cli_audit_prune(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    notes_dir = tmp_path / "notes"
    db_path = tmp_path / "h.db"
    cfg_path.write_text(
        f"""storage:
  database_path: {db_path}
llm:
  endpoint: ""
notes:
  notes_dir: {notes_dir}
""",
    )
    for i in range(5):
        append(notes_dir, _report(generated_at=f"2026-06-{i+1:02d}T00:00:00Z"))
    runner = CliRunner()
    result = runner.invoke(
        app, ["audit-prune", "--config", str(cfg_path), "--keep", "2"],
    )
    assert result.exit_code == 0
    assert "Pruned 3" in result.stdout
    remaining = (notes_dir / FILE_NAME).read_text().strip().splitlines()
    assert len(remaining) == 2


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


# ─── HTTP API: /api/audit/diff, /history, /history.jsonl ──────────


def test_api_audit_diff_first_call_no_previous(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    cfg.notes.notes_dir = str(tmp_path / "notes")
    app_inst = create_app(cfg)
    client = TestClient(app_inst)
    r = client.get("/api/audit/diff")
    assert r.status_code == 200
    body = r.json()
    assert body["previous_snapshot"] is None
    assert body["resolved"] == []


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


def test_api_audit_history_jsonl_empty(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    cfg.notes.notes_dir = str(tmp_path / "notes")
    client = TestClient(create_app(cfg))
    r = client.get("/api/audit/history.jsonl")
    assert r.status_code == 200
    assert r.text == ""


def test_api_audit_history_jsonl_serves_file(tmp_path):
    from homelabsage.web import create_app
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    cfg.notes.notes_dir = str(tmp_path / "notes")
    append(cfg.notes.notes_dir, _report([_finding()]))
    append(cfg.notes.notes_dir, _report([_finding(source_ref="ref2")]))
    client = TestClient(create_app(cfg))
    r = client.get("/api/audit/history.jsonl")
    assert r.status_code == 200
    lines = [ln for ln in r.text.splitlines() if ln]
    assert len(lines) == 2
    # Content-Disposition + correct mime
    cd = r.headers.get("content-disposition", "")
    assert "audit_history.jsonl" in cd
    assert r.headers.get("content-type", "").startswith("application/x-ndjson")


# ─── MCP: audit_history / audit_diff / audit_prune ────────────────


def test_mcp_audit_history_returns_compact(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    cfg.notes.notes_dir = str(tmp_path / "notes")
    append(cfg.notes.notes_dir, _report([{
        "category": "cve", "source_kind": "cve", "source_ref": "x",
        "severity": "high", "title": "t", "detail": "d", "cite": "c",
    }]))
    db = Database(":memory:")
    impl = TOOLS["audit_history"]["impl"]
    out = impl(cfg, db, {})
    assert out["count"] == 1
    assert out["items"][0]["finding_count"] == 1
    assert "findings" not in out["items"][0]   # never returns full list


def test_mcp_audit_history_no_notes_dir():
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["audit_history"]["impl"]
    out = impl(cfg, db, {})
    assert out["count"] == 0
    assert "no notes_dir" in out["reason"]


def test_mcp_audit_diff_first_run_all_new(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    cfg.notes.notes_dir = str(tmp_path / "notes")
    db = Database(cfg.storage.database_path)
    impl = TOOLS["audit_diff"]["impl"]
    out = impl(cfg, db, {})
    assert out["previous_snapshot"] is None
    assert isinstance(out["new"], list)
    assert out["resolved"] == []


def test_mcp_audit_diff_no_notes_dir():
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["audit_diff"]["impl"]
    out = impl(cfg, db, {})
    assert out["previous_snapshot"] is None
    assert "no notes_dir" in out["reason"]


def test_mcp_audit_prune_drops_rows(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    cfg.notes.notes_dir = str(tmp_path / "notes")
    db = Database(":memory:")
    for i in range(5):
        append(cfg.notes.notes_dir,
               _report(generated_at=f"2026-06-{i+1:02d}T00:00:00Z"))
    impl = TOOLS["audit_prune"]["impl"]
    out = impl(cfg, db, {"keep_last": 2})
    assert out["ok"] is True
    assert out["dropped"] == 3
    assert out["keep_last"] == 2


def test_mcp_audit_prune_no_notes_dir():
    from homelabsage.mcp import TOOLS
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["audit_prune"]["impl"]
    out = impl(cfg, db, {"keep_last": 10})
    assert out["ok"] is False
    assert "no notes_dir" in out["reason"]


def test_mcp_audit_prune_defaults_to_100(tmp_path):
    from homelabsage.mcp import TOOLS
    cfg = Config()
    cfg.notes.notes_dir = str(tmp_path / "notes")
    db = Database(":memory:")
    impl = TOOLS["audit_prune"]["impl"]
    # Empty params → defaults
    out = impl(cfg, db, {})
    assert out["ok"] is True
    assert out["keep_last"] == 100


# ─── CLI: audit --severity ────────────────────────────────────────


def test_audit_severity_rejects_bad_value(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    db_path = tmp_path / "h.db"
    cfg_path.write_text(
        f"""storage:
  database_path: {db_path}
llm:
  endpoint: ""
""",
    )
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "audit", "--config", str(cfg_path),
            "--severity", "garbage",
        ],
    )
    assert result.exit_code != 0


def test_audit_severity_accepts_known_values(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    db_path = tmp_path / "h.db"
    cfg_path.write_text(
        f"""storage:
  database_path: {db_path}
llm:
  endpoint: ""
""",
    )
    runner = CliRunner()
    for sev in ("info", "medium", "high", "critical"):
        result = runner.invoke(
            app, [
                "audit", "--config", str(cfg_path),
                "--severity", sev, "--jsonl",
            ],
        )
        assert result.exit_code == 0, f"failed for {sev}: {result.stdout}"


def test_audit_filter_keeps_high_only():
    """The _filter_findings helper drops below-floor rows + recomputes counts."""
    from dataclasses import dataclass

    from homelabsage.cli.audit import _filter_findings

    @dataclass
    class _F:
        severity: str
        category: str
        title: str = "t"
        detail: str = "d"
        source_kind: str = "sk"
        source_ref: str = "sr"
        cite: str = "c"

    @dataclass
    class _R:
        findings: list
        counts_by_severity: dict
        counts_by_category: dict
        healthy: bool

    rep = _R(
        findings=[
            _F("info", "a"), _F("medium", "b"),
            _F("high", "c"), _F("critical", "d"),
        ],
        counts_by_severity={"info": 1, "medium": 1, "high": 1, "critical": 1},
        counts_by_category={"a": 1, "b": 1, "c": 1, "d": 1},
        healthy=False,
    )
    filtered = _filter_findings(rep, "high")
    sevs = [f.severity for f in filtered.findings]
    assert sevs == ["high", "critical"]
    assert filtered.counts_by_severity == {"high": 1, "critical": 1}
    assert filtered.healthy is False


def test_audit_filter_empty_floor_is_noop():
    """No severity flag → return report unchanged."""
    from dataclasses import dataclass

    from homelabsage.cli.audit import _filter_findings

    @dataclass
    class _R:
        findings: list
        counts_by_severity: dict
        counts_by_category: dict
        healthy: bool

    original = _R([1, 2, 3], {"info": 3}, {"x": 3}, False)
    out = _filter_findings(original, "")
    assert out is original


# ─── CLI: audit --diff-only ───────────────────────────────────────


def test_audit_diff_only_no_previous_returns_all(tmp_path):
    """No previous snapshot → diff-only behaves like normal (empty here)."""
    cfg_path, _db_path, _notes_dir = _config_yaml(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app, ["audit", "--config", str(cfg_path), "--diff-only", "--jsonl"],
    )
    assert result.exit_code == 0
    # Empty DB + no previous snapshot → 0 findings
    assert result.stdout.strip() == ""


def test_audit_diff_only_jsonl_does_not_append_history(tmp_path):
    """--diff-only must not call run_audit (no append, no notes write)."""
    cfg_path, _db_path, notes_dir = _config_yaml(tmp_path)
    runner = CliRunner()
    runner.invoke(
        app, ["audit", "--config", str(cfg_path), "--diff-only", "--jsonl"],
    )
    # audit_history.jsonl should NOT have been created by --diff-only
    assert not (notes_dir / FILE_NAME).exists()


def test_audit_diff_only_filters_against_previous(tmp_path):
    """Seed a previous snapshot, verify diff-only hides matching keys."""
    cfg_path, _db_path, notes_dir = _config_yaml(tmp_path)
    # Seed a previous snapshot containing a fingerprint we expect to match
    # one that build_report would NOT produce on this empty DB.
    notes_dir.mkdir(parents=True, exist_ok=True)
    append(notes_dir, _report([_finding(category="x", source_ref="y")]))
    runner = CliRunner()
    result = runner.invoke(
        app, ["audit", "--config", str(cfg_path), "--diff-only", "--jsonl"],
    )
    assert result.exit_code == 0
    # No new findings vs the seed; output is empty
    assert result.stdout.strip() == ""
    # And the diff-only flag still didn't append a new history row
    contents = (notes_dir / FILE_NAME).read_text().splitlines()
    assert len(contents) == 1
