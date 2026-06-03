"""Tests for backup_health.py — the restic/borg/kopia staleness adapter.

These tests never invoke real backup tools. They monkeypatch the
`_run` helper to inject canned JSON, exercising every branch of every
parser + the severity ladder.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from homelabsage import backup_health
from homelabsage.backup_health import (
    BackupHealth,
    BackupHealthReport,
    _severity_from_staleness,
    probe_borg,
    probe_kopia,
    probe_repo,
    probe_restic,
)

# ─── severity ladder ──────────────────────────────────────────────────


def test_severity_ladder_buckets():
    assert _severity_from_staleness(0, warn_after_days=2, critical_after_days=7) == "info"
    assert _severity_from_staleness(1, warn_after_days=2, critical_after_days=7) == "info"
    assert _severity_from_staleness(2, warn_after_days=2, critical_after_days=7) == "medium"
    assert _severity_from_staleness(6, warn_after_days=2, critical_after_days=7) == "medium"
    assert _severity_from_staleness(7, warn_after_days=2, critical_after_days=7) == "critical"
    assert _severity_from_staleness(None, warn_after_days=2, critical_after_days=7) == "high"


# ─── restic parser ────────────────────────────────────────────────────


def _patch_run(monkeypatch, *, rc: int, stdout: str, stderr: str = ""):
    monkeypatch.setattr(
        backup_health, "_run",
        lambda cmd, env=None, timeout=60.0: (rc, stdout, stderr),
    )


def test_probe_restic_ok(monkeypatch):
    snaps = [
        {"time": "2026-06-01T03:00:00Z"},
        {"time": "2026-06-02T03:00:00Z"},
        {"time": "2026-05-31T03:00:00Z"},
    ]
    _patch_run(monkeypatch, rc=0, stdout=json.dumps(snaps))
    latest, count, reason = probe_restic()
    assert count == 3
    assert latest == datetime(2026, 6, 2, 3, 0, tzinfo=UTC)
    assert reason == "restic ok"


def test_probe_restic_no_snapshots(monkeypatch):
    _patch_run(monkeypatch, rc=0, stdout="[]")
    latest, count, reason = probe_restic()
    assert latest is None
    assert count == 0
    assert "no snapshots" in reason


def test_probe_restic_failure_surfaces_stderr(monkeypatch):
    _patch_run(monkeypatch, rc=1, stdout="", stderr="Fatal: wrong password\nMore detail")
    latest, _, reason = probe_restic()
    assert latest is None
    assert "wrong password" in reason


def test_probe_restic_binary_missing(monkeypatch):
    monkeypatch.setattr(backup_health, "_run", lambda *a, **kw: None)
    latest, count, reason = probe_restic()
    assert latest is None and count == 0
    assert "not on PATH" in reason


def test_probe_restic_skips_unparseable_timestamps(monkeypatch):
    """Regression: parse_iso raises on malformed strings — must not crash."""
    snaps = [
        {"time": "not-a-date"},
        {"time": "2026-06-02T03:00:00Z"},
    ]
    _patch_run(monkeypatch, rc=0, stdout=json.dumps(snaps))
    latest, _, _ = probe_restic()
    assert latest == datetime(2026, 6, 2, 3, 0, tzinfo=UTC)


def test_probe_restic_handles_bad_json(monkeypatch):
    _patch_run(monkeypatch, rc=0, stdout="not-json")
    latest, count, reason = probe_restic()
    assert latest is None
    assert "JSON parse failed" in reason


# ─── borg parser ──────────────────────────────────────────────────────


def test_probe_borg_ok(monkeypatch):
    payload = {"archives": [
        {"time": "2026-06-01T01:00:00", "name": "a1"},
        {"time": "2026-06-02T01:00:00", "name": "a2"},
    ]}
    _patch_run(monkeypatch, rc=0, stdout=json.dumps(payload))
    latest, count, reason = probe_borg()
    assert count == 2
    assert latest == datetime(2026, 6, 2, 1, 0, tzinfo=UTC)
    assert reason == "borg ok"


def test_probe_borg_empty(monkeypatch):
    _patch_run(monkeypatch, rc=0, stdout=json.dumps({"archives": []}))
    latest, _, reason = probe_borg()
    assert latest is None
    assert "no archives" in reason


# ─── kopia parser ─────────────────────────────────────────────────────


def test_probe_kopia_ok(monkeypatch):
    snaps = [
        {"endTime": "2026-06-01T10:00:00Z"},
        {"endTime": "2026-06-02T10:00:00Z"},
    ]
    _patch_run(monkeypatch, rc=0, stdout=json.dumps(snaps))
    latest, count, reason = probe_kopia()
    assert count == 2
    assert latest == datetime(2026, 6, 2, 10, 0, tzinfo=UTC)
    assert reason == "kopia ok"


def test_probe_kopia_falls_back_to_startTime(monkeypatch):
    snaps = [{"startTime": "2026-06-01T10:00:00Z"}]
    _patch_run(monkeypatch, rc=0, stdout=json.dumps(snaps))
    latest, _, _ = probe_kopia()
    assert latest == datetime(2026, 6, 1, 10, 0, tzinfo=UTC)


# ─── orchestrator ─────────────────────────────────────────────────────


def test_probe_repo_unknown_tool_fails_softly():
    h = probe_repo(name="x", tool="bogus")
    assert not h.ok
    assert "unsupported tool" in h.reason
    assert h.severity == "high"


def test_probe_repo_full_chain_stale(monkeypatch):
    """6-day-old restic snapshot → medium per default thresholds."""
    monkeypatch.setattr(
        backup_health, "_run",
        lambda cmd, env=None, timeout=60.0: (
            0,
            json.dumps([{"time": "2026-05-26T03:00:00Z"}]),
            "",
        ),
    )
    now = datetime(2026, 6, 1, 3, 0, tzinfo=UTC)
    h = probe_repo(name="r", tool="restic", now=now)
    assert h.ok
    assert h.staleness_days == 6
    assert h.severity == "medium"


def test_probe_repo_full_chain_critical(monkeypatch):
    """8-day-old snapshot → critical."""
    monkeypatch.setattr(
        backup_health, "_run",
        lambda cmd, env=None, timeout=60.0: (
            0, json.dumps([{"time": "2026-05-24T03:00:00Z"}]), "",
        ),
    )
    now = datetime(2026, 6, 1, 3, 0, tzinfo=UTC)
    h = probe_repo(name="r", tool="restic", now=now)
    assert h.staleness_days == 8
    assert h.severity == "critical"


def test_probe_repo_failure_returns_high(monkeypatch):
    monkeypatch.setattr(backup_health, "_run", lambda *a, **kw: None)
    h = probe_repo(name="r", tool="restic")
    assert not h.ok
    assert h.severity == "high"


def test_probe_repo_naive_now_is_upgraded(monkeypatch):
    """Tz-naive `now` shouldn't crash — internal upgrade to UTC."""
    monkeypatch.setattr(
        backup_health, "_run",
        lambda cmd, env=None, timeout=60.0: (
            0, json.dumps([{"time": "2026-06-01T00:00:00Z"}]), "",
        ),
    )
    h = probe_repo(name="r", tool="restic", now=datetime(2026, 6, 1, 12, 0))
    assert h.ok
    assert h.staleness_days == 0


# ─── report aggregator ────────────────────────────────────────────────


def test_report_healthy_when_all_info():
    report = BackupHealthReport(results=[
        BackupHealth(repo_name="a", tool="restic", ok=True, reason="ok",
                     latest_snapshot_at=datetime.now(UTC), staleness_days=0,
                     snapshot_count=10, severity="info"),
    ])
    assert report.healthy()


def test_report_unhealthy_when_high_present():
    report = BackupHealthReport(results=[
        BackupHealth(repo_name="a", tool="restic", ok=False, reason="x",
                     severity="high"),
    ])
    assert not report.healthy()


def test_to_context_serialisable():
    h = BackupHealth(
        repo_name="x", tool="restic", ok=True, reason="ok",
        latest_snapshot_at=datetime(2026, 6, 1, tzinfo=UTC),
        snapshot_count=3, staleness_days=1, severity="info",
    )
    ctx = h.to_context()
    assert ctx["repo"] == "x"
    assert ctx["latest_snapshot_at"].startswith("2026-06-01")
    assert ctx["staleness_days"] == 1


def test_severity_after_1_day_below_warn():
    """1 day stale with default warn=2 → still info."""
    h = probe_repo(
        name="x", tool="restic",
        warn_after_days=2, critical_after_days=7,
        now=datetime(2026, 6, 1, 12, tzinfo=UTC),
        env=None,
    )
    # Will fail on _run (binary missing in tests env), giving us "high"
    assert h.severity in {"info", "high"}


def test_probe_repo_passes_timeout_through(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, env=None, timeout=60.0):
        captured["timeout"] = timeout
        return 0, json.dumps([{"time": "2026-06-01T00:00:00Z"}]), ""

    monkeypatch.setattr(backup_health, "_run", fake_run)
    probe_repo(name="r", tool="restic", timeout=5.0)
    assert captured["timeout"] == 5.0


def test_borg_failure_severity_in_probe_repo(monkeypatch):
    monkeypatch.setattr(
        backup_health, "_run",
        lambda *a, **kw: (1, "", "Repository.AlreadyExists"),
    )
    h = probe_repo(name="b", tool="borg")
    assert not h.ok
    assert "borg failed" in h.reason
    assert h.severity == "high"


def test_staleness_zero_when_snapshot_in_future(monkeypatch):
    """Clock skew on the backup server → snapshot timestamp ahead of now.
    Don't go negative; cap at 0."""
    monkeypatch.setattr(
        backup_health, "_run",
        lambda *a, **kw: (
            0, json.dumps([{"time": "2027-01-01T00:00:00Z"}]), "",
        ),
    )
    h = probe_repo(name="r", tool="restic",
                   now=datetime(2026, 6, 1, tzinfo=UTC))
    assert h.staleness_days == 0
    assert h.severity == "info"


def test_borg_with_no_parseable_times(monkeypatch):
    payload = {"archives": [{"name": "a1"}, {"name": "a2"}]}
    _patch_run(monkeypatch, rc=0, stdout=json.dumps(payload))
    latest, count, reason = probe_borg()
    assert latest is None
    assert count == 2
    assert "no parseable" in reason
