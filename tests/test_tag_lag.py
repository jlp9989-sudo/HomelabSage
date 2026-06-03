"""Tests for the tag-promotion-lag detector."""

from __future__ import annotations

from datetime import UTC, datetime

from homelabsage.tag_lag import TagLag, _severity_from_lag, derive_tag_lag


def test_severity_from_lag_buckets():
    assert _severity_from_lag(0, warn_after_days=14, critical_after_days=60) == "info"
    assert _severity_from_lag(13, warn_after_days=14, critical_after_days=60) == "info"
    assert _severity_from_lag(14, warn_after_days=14, critical_after_days=60) == "medium"
    assert _severity_from_lag(45, warn_after_days=14, critical_after_days=60) == "medium"
    assert _severity_from_lag(60, warn_after_days=14, critical_after_days=60) == "high"
    assert _severity_from_lag(None, warn_after_days=14, critical_after_days=60) == "info"


def test_derive_tag_lag_returns_none_without_signals():
    assert derive_tag_lag({}) is None


def test_derive_tag_lag_returns_none_with_only_local():
    """Regression: local-only context has no actionable lag signal."""
    ctx = {"local_pulled_at": "2026-05-01T00:00:00Z"}
    assert derive_tag_lag(ctx) is None


def test_derive_tag_lag_local_behind_remote():
    """Remote pushed 2026-06-01, local pulled 2026-05-01 → 31 days behind."""
    ctx = {
        "remote_pushed_at": "2026-06-01T00:00:00Z",
        "local_pulled_at": "2026-05-01T00:00:00Z",
    }
    now = datetime(2026, 6, 2, tzinfo=UTC)
    lag = derive_tag_lag(ctx, now=now)
    assert lag is not None
    assert lag.days_local_behind == 31
    assert lag.severity == "medium"


def test_derive_tag_lag_no_local_treated_as_full_behind():
    ctx = {"remote_pushed_at": "2026-04-01T00:00:00Z"}
    now = datetime(2026, 6, 1, tzinfo=UTC)
    lag = derive_tag_lag(ctx, now=now)
    assert lag is not None
    assert lag.days_local_behind == 61
    assert lag.severity == "high"


def test_derive_tag_lag_caught_up_is_info():
    """Local pulled AFTER the remote push — no lag at all."""
    ctx = {
        "remote_pushed_at": "2026-05-01T00:00:00Z",
        "local_pulled_at": "2026-05-15T00:00:00Z",
    }
    now = datetime(2026, 6, 1, tzinfo=UTC)
    lag = derive_tag_lag(ctx, now=now)
    assert lag is not None
    assert lag.days_local_behind == 0
    assert lag.severity == "info"


def test_derive_tag_lag_naive_now_upgraded():
    ctx = {
        "remote_pushed_at": "2026-06-01T00:00:00Z",
        "local_pulled_at": "2026-05-01T00:00:00Z",
    }
    lag = derive_tag_lag(ctx, now=datetime(2026, 6, 5))
    assert lag is not None
    assert lag.days_local_behind == 31


def test_derive_tag_lag_thresholds_override():
    ctx = {
        "remote_pushed_at": "2026-05-15T00:00:00Z",
        "local_pulled_at": "2026-05-01T00:00:00Z",
    }
    now = datetime(2026, 6, 1, tzinfo=UTC)
    lag = derive_tag_lag(
        ctx, now=now,
        warn_after_days=10, critical_after_days=20,
    )
    assert lag is not None
    assert lag.days_local_behind == 14
    assert lag.severity == "medium"


def test_to_context_round_trips():
    lag = TagLag(days_since_remote_push=20, days_local_behind=15, severity="medium")
    ctx = lag.to_context()
    assert ctx["days_local_behind"] == 15
    assert ctx["severity"] == "medium"
