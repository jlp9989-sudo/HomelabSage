"""Tests for the quiet-hours gate."""

from __future__ import annotations

from datetime import UTC, datetime, time

from homelabsage.models import Severity
from homelabsage.quiet_hours import (
    is_quiet_now,
    parse_window,
    should_bypass,
    time_in_window,
)

# ─── parse_window ──────────────────────────────────────────────────


def test_parse_window_basic():
    assert parse_window("23:00-07:00") == (time(23, 0), time(7, 0))


def test_parse_window_with_spaces():
    assert parse_window(" 22:30 - 06:15 ") == (time(22, 30), time(6, 15))


def test_parse_window_empty_is_none():
    assert parse_window("") is None
    assert parse_window("   ") is None


def test_parse_window_invalid_is_none():
    assert parse_window("nonsense") is None
    assert parse_window("25:00-26:00") is None


# ─── time_in_window ─────────────────────────────────────────────────


def test_time_in_window_simple():
    w = (time(9, 0), time(17, 0))
    assert time_in_window(time(10, 0), w)
    assert time_in_window(time(9, 0), w)        # start inclusive
    assert not time_in_window(time(17, 0), w)   # end exclusive
    assert not time_in_window(time(8, 59), w)


def test_time_in_window_crosses_midnight():
    w = (time(23, 0), time(7, 0))
    assert time_in_window(time(23, 30), w)
    assert time_in_window(time(0, 0), w)
    assert time_in_window(time(6, 59), w)
    assert not time_in_window(time(7, 0), w)
    assert not time_in_window(time(12, 0), w)


def test_time_in_window_zero_width_is_false():
    w = (time(12, 0), time(12, 0))
    assert not time_in_window(time(12, 0), w)


# ─── is_quiet_now ───────────────────────────────────────────────────


def test_is_quiet_now_inside_window_utc():
    now = datetime(2026, 6, 1, 3, 0, tzinfo=UTC)
    state = is_quiet_now(window_spec="23:00-07:00", now=now)
    assert state.quiet
    assert "inside quiet window" in state.reason


def test_is_quiet_now_outside_window_utc():
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    state = is_quiet_now(window_spec="23:00-07:00", now=now)
    assert not state.quiet


def test_is_quiet_now_empty_spec_returns_not_quiet():
    state = is_quiet_now(window_spec="")
    assert not state.quiet


def test_is_quiet_now_unknown_timezone_returns_not_quiet():
    now = datetime(2026, 6, 1, 3, 0, tzinfo=UTC)
    state = is_quiet_now(
        window_spec="23:00-07:00", timezone_name="Mars/Olympus", now=now,
    )
    assert not state.quiet
    assert "unknown timezone" in state.reason


def test_is_quiet_now_respects_local_timezone():
    """03:00 UTC is 05:00 in Europe/Madrid (UTC+2 in June DST)."""
    now = datetime(2026, 6, 1, 3, 0, tzinfo=UTC)  # = 05:00 Madrid
    inside = is_quiet_now(
        window_spec="04:00-06:00", timezone_name="Europe/Madrid", now=now,
    )
    assert inside.quiet
    outside = is_quiet_now(
        window_spec="22:00-04:00", timezone_name="Europe/Madrid", now=now,
    )
    assert not outside.quiet


# ─── should_bypass ──────────────────────────────────────────────────


def test_should_bypass_critical_escapes_critical_floor():
    assert should_bypass(Severity.CRITICAL, "critical")


def test_should_bypass_high_does_not_escape_critical_floor():
    assert not should_bypass(Severity.HIGH, "critical")


def test_should_bypass_high_escapes_high_floor():
    assert should_bypass(Severity.HIGH, "high")


def test_should_bypass_empty_floor_never_bypasses():
    assert not should_bypass(Severity.CRITICAL, "")
    assert not should_bypass(Severity.CRITICAL, None)


def test_should_bypass_no_analysis_never_bypasses():
    assert not should_bypass(None, "info")
