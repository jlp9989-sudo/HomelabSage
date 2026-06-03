"""Tests for the post-update health check pipeline."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from homelabsage.config import HealthCheckConfig
from homelabsage.db import Database
from homelabsage.health_check import (
    ProbeResult,
    queue_for_update,
    run_due_checks,
    scan_lines,
)

# ─── scan_lines ──────────────────────────────────────────────────────


def test_scan_lines_detects_cuda_fallback():
    lines = [
        "2026-06-01 INFO starting up",
        "2026-06-01 WARN: falling back to CPU; CUDA initialisation failed",
    ]
    match = scan_lines(lines)
    assert match is not None
    assert match[0] == "cuda_fallback"
    assert "falling back to CPU" in match[1]


def test_scan_lines_detects_rocm_fallback():
    lines = ["ERROR: rocm initialization failed, falling back from rocm to CPU"]
    match = scan_lines(lines)
    assert match is not None
    assert match[0] == "rocm_fallback"


def test_scan_lines_detects_oom():
    lines = ["container OOMKilled at 2026-06-01"]
    match = scan_lines(lines)
    assert match is not None
    assert match[0] == "oom"


def test_scan_lines_detects_panic():
    lines = ["panic: runtime error: invalid memory address or nil pointer"]
    match = scan_lines(lines)
    assert match is not None
    assert match[0] == "panic"


def test_scan_lines_returns_none_on_clean_log():
    match = scan_lines(["INFO started", "INFO ready", "DEBUG hb"])
    assert match is None


def test_scan_lines_extra_patterns_compile_and_match():
    match = scan_lines(["custom_failure: db connection lost"],
                       extra_patterns=["custom_failure"])
    assert match is not None
    assert match[0].startswith("custom:")


def test_scan_lines_invalid_extra_pattern_skipped():
    # Bad regex must not crash the scan; the valid built-in still fires.
    match = scan_lines(["panic: hi"], extra_patterns=["(unclosed"])
    assert match is not None
    assert match[0] == "panic"


# ─── queue_for_update ────────────────────────────────────────────────


def test_queue_for_update_noop_when_disabled(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    cfg = HealthCheckConfig(enabled=False)
    try:
        queue_for_update(db, update_id="docker:x:1", container_name="x", cfg=cfg)
        assert db.due_health_checks(now=datetime.now(UTC) + timedelta(hours=24)) == []
    finally:
        db.close()


def test_queue_for_update_inserts_row(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    cfg = HealthCheckConfig(enabled=True, grace_minutes=10)
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    try:
        queue_for_update(db, update_id="u1", container_name="c1", cfg=cfg, now=now)
        # Not yet due — exactly at +10 mins it IS due (≤ comparison)
        rows = db.due_health_checks(now=now + timedelta(minutes=10))
        assert len(rows) == 1
        assert rows[0]["update_id"] == "u1"
        # Not yet due 5 mins after queueing
        rows = db.due_health_checks(now=now + timedelta(minutes=5))
        assert rows == []
    finally:
        db.close()


def test_queue_for_update_replaces_existing(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    cfg = HealthCheckConfig(enabled=True, grace_minutes=10)
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    try:
        queue_for_update(db, update_id="u1", container_name="c1", cfg=cfg, now=now)
        # Second queueing with later fire_at — replaces the first.
        later = now + timedelta(hours=2)
        queue_for_update(db, update_id="u1", container_name="c1", cfg=cfg, now=later)
        rows = db.due_health_checks(now=now + timedelta(minutes=15))
        assert rows == []  # First fire_at was overwritten
        rows = db.due_health_checks(now=later + timedelta(minutes=15))
        assert len(rows) == 1
    finally:
        db.close()


# ─── run_due_checks ──────────────────────────────────────────────────


def test_run_due_checks_persists_and_dequeues(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "s.sqlite")
    cfg = HealthCheckConfig(enabled=True, grace_minutes=0)
    try:
        queue_for_update(db, update_id="u1", container_name="c1", cfg=cfg)
        # Stub out probe_one to skip the docker SDK call.
        from homelabsage import health_check as hc
        monkeypatch.setattr(
            hc, "probe_one",
            lambda **kw: ProbeResult(
                update_id=kw["update_id"],
                container_name=kw["container_name"],
                ok=False, signal="cuda_fallback",
                excerpt="falling back to CPU",
            ),
        )
        results = asyncio.run(run_due_checks(cfg, db,
                                             now=datetime.now(UTC) + timedelta(hours=1)))
        assert len(results) == 1
        assert results[0].ok is False
        # Queue cleared
        assert db.due_health_checks(now=datetime.now(UTC) + timedelta(hours=1)) == []
        # Result persisted in history table
        rows = db.list_recent_health_checks()
        assert len(rows) == 1
        assert rows[0]["signal"] == "cuda_fallback"
    finally:
        db.close()


def test_run_due_checks_skips_when_disabled(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    cfg = HealthCheckConfig(enabled=False)
    try:
        results = asyncio.run(run_due_checks(cfg, db))
        assert results == []
    finally:
        db.close()


def test_run_due_checks_runs_only_due_rows(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "s.sqlite")
    cfg = HealthCheckConfig(enabled=True, grace_minutes=30)
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    try:
        queue_for_update(db, update_id="u1", container_name="c1", cfg=cfg, now=now)
        # Stub probe → all probes succeed.
        from homelabsage import health_check as hc
        monkeypatch.setattr(
            hc, "probe_one",
            lambda **kw: ProbeResult(
                update_id=kw["update_id"],
                container_name=kw["container_name"], ok=True,
            ),
        )
        # 5 mins in — not yet due, no probes run
        results = asyncio.run(run_due_checks(cfg, db, now=now + timedelta(minutes=5)))
        assert results == []
        # 35 mins in — due
        results = asyncio.run(run_due_checks(cfg, db, now=now + timedelta(minutes=35)))
        assert len(results) == 1
    finally:
        db.close()
