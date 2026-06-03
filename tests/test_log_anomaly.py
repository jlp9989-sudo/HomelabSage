"""Tests for the proactive log-anomaly detector."""

from __future__ import annotations

import asyncio
from pathlib import Path

from homelabsage.config import LogAnomalyConfig
from homelabsage.db import Database
from homelabsage.log_anomaly import (
    AnomalySignal,
    LogScanSample,
    count_errors_warns,
    evaluate,
    run_log_anomaly_scan,
)


def test_count_errors_warns_basic():
    lines = [
        "2026-06-01 INFO server up",
        "2026-06-01 ERROR connection refused",
        "2026-06-01 WARN slow query",
        "2026-06-01 INFO request handled",
        "2026-06-01 panic: nil pointer",
        "  ",
        "2026-06-01T12:00:00",
    ]
    err, warn = count_errors_warns(lines)
    assert err == 2          # ERROR + panic
    assert warn == 1


def test_count_errors_warns_skips_pure_timestamps():
    err, warn = count_errors_warns(["2026-06-01T12:00:00.123456", "   "])
    assert err == 0
    assert warn == 0


def test_evaluate_returns_none_before_min_samples(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        for _ in range(3):
            db.record_log_sample(
                container_name="x", error_count=0, warn_count=0, window_minutes=60,
            )
        signal = evaluate(
            db,
            LogScanSample(container_name="x", error_count=10, warn_count=0,
                          window_minutes=60),
            sigma_threshold=3.0, min_samples=7,
        )
        assert signal is None
    finally:
        db.close()


def test_evaluate_fires_above_sigma_threshold(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        # 14 quiet samples — mean=0.05, std≈0 (set zero count + 1 outlier)
        for _ in range(10):
            db.record_log_sample(
                container_name="x", error_count=3, warn_count=0, window_minutes=60,
            )
        for _ in range(4):
            db.record_log_sample(
                container_name="x", error_count=4, warn_count=0, window_minutes=60,
            )
        # Spike: 120 errors in 60 minutes → 2.0/min vs ~0.055/min baseline.
        signal = evaluate(
            db,
            LogScanSample(container_name="x", error_count=120, warn_count=0,
                          window_minutes=60),
            sigma_threshold=3.0, min_samples=7,
        )
        assert signal is not None
        assert signal.sigma > 3.0
        assert signal.error_rate > signal.baseline_mean
    finally:
        db.close()


def test_evaluate_zero_variance_baseline_handles_first_error(tmp_path: Path):
    """Always-quiet container: any non-zero rate after warm-up fires."""
    db = Database(tmp_path / "s.sqlite")
    try:
        for _ in range(10):
            db.record_log_sample(
                container_name="x", error_count=0, warn_count=0, window_minutes=60,
            )
        signal = evaluate(
            db,
            LogScanSample(container_name="x", error_count=60, warn_count=0,
                          window_minutes=60),
            sigma_threshold=3.0, min_samples=7,
        )
        assert signal is not None
    finally:
        db.close()


def test_evaluate_zero_variance_no_fire_below_floor(tmp_path: Path):
    """Zero-variance baseline ignores trickle errors below 1/min floor."""
    db = Database(tmp_path / "s.sqlite")
    try:
        for _ in range(10):
            db.record_log_sample(
                container_name="x", error_count=0, warn_count=0, window_minutes=60,
            )
        # 30 errors / 60 min = 0.5/min — below the 1.0 floor
        signal = evaluate(
            db,
            LogScanSample(container_name="x", error_count=30, warn_count=0,
                          window_minutes=60),
            sigma_threshold=3.0, min_samples=7,
        )
        assert signal is None
    finally:
        db.close()


def test_run_scan_skips_when_disabled(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    cfg = LogAnomalyConfig(enabled=False)
    try:
        result = asyncio.run(run_log_anomaly_scan(cfg, db, container_names=["x"]))
        assert result == []
    finally:
        db.close()


def test_run_scan_persists_samples_and_anomalies(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "s.sqlite")
    try:
        # Pre-seed baseline: 10 quiet samples.
        for _ in range(10):
            db.record_log_sample(
                container_name="loud-container",
                error_count=2, warn_count=0, window_minutes=60,
            )
        # Monkeypatch scan_container to return a spike sample.
        from homelabsage import log_anomaly as la
        spike = LogScanSample(container_name="loud-container",
                              error_count=300, warn_count=0,
                              window_minutes=60)
        monkeypatch.setattr(la, "scan_container",
                            lambda name, **kwargs: spike)
        cfg = LogAnomalyConfig(
            enabled=True, sigma_threshold=3.0, min_samples=7,
            container_skip=[],
        )
        signals = asyncio.run(run_log_anomaly_scan(
            cfg, db, container_names=["loud-container"],
        ))
        assert len(signals) == 1
        # Anomaly persisted
        rows = db.list_recent_log_anomalies()
        assert len(rows) == 1
        # Sample persisted (baseline now grew)
        _, _, n = db.baseline_for(container_name="loud-container")
        assert n == 11
    finally:
        db.close()


def test_run_scan_skip_regex(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "s.sqlite")
    try:
        called = []

        def fake_scan(name, **kwargs):
            called.append(name)
            return LogScanSample(
                container_name=name, error_count=0, warn_count=0,
                window_minutes=60,
            )

        from homelabsage import log_anomaly as la
        monkeypatch.setattr(la, "scan_container", fake_scan)
        cfg = LogAnomalyConfig(
            enabled=True, sigma_threshold=3.0, min_samples=7,
            container_skip=["tailscale", "^caddy"],
        )
        asyncio.run(run_log_anomaly_scan(
            cfg, db,
            container_names=["caddy-proxy", "tailscale", "interesting"],
        ))
        assert called == ["interesting"]
    finally:
        db.close()


def test_anomaly_signal_carries_baseline():
    sig = AnomalySignal(
        container_name="x", error_rate=10.0,
        baseline_mean=1.0, baseline_std=0.5, sigma=18.0,
    )
    assert sig.sigma == 18.0
    assert sig.baseline_mean == 1.0
