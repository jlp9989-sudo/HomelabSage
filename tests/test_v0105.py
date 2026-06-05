"""Tests for v0.10.5: punch-list C4 (restart_freq false-positive) + I12 (log cap)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homelabsage.csi import fetch_docker_logs
from homelabsage.log_anomaly import scan_container
from homelabsage.restart_freq import evaluate

# ─── C4: restart_freq requires min uptime before flagging ─────────


def _now():
    return datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)


def test_restart_freq_suppressed_with_short_uptime():
    """The pathological case from the audit: 47 lifetime crashes, container
    manually restarted 5 minutes ago. Old code: rate=564/h → critical.
    New code: uptime < 1h → return None."""
    finding = evaluate(
        restart_count=47,
        started_at=_now() - timedelta(minutes=5),
        now=_now(),
    )
    assert finding is None


def test_restart_freq_still_flags_real_flapping():
    """High lifetime count + reasonable uptime → still flags."""
    # 50 restarts across 24h = ~2/h → "high" tier
    finding = evaluate(
        restart_count=50,
        started_at=_now() - timedelta(hours=24),
        now=_now(),
    )
    assert finding is not None
    assert finding.severity == "high"


def test_restart_freq_critical_at_minute_rate():
    """≥60 restarts/h → critical. Uptime 2h, 120 restarts → 60/h."""
    finding = evaluate(
        restart_count=120,
        started_at=_now() - timedelta(hours=2),
        now=_now(),
    )
    assert finding is not None
    assert finding.severity == "critical"


def test_restart_freq_minute_old_uptime_returns_none():
    """Even with huge restart_count, short uptime suppresses."""
    finding = evaluate(
        restart_count=1000,
        started_at=_now() - timedelta(minutes=30),  # below 1h floor
        now=_now(),
    )
    assert finding is None


def test_restart_freq_custom_uptime_floor_relaxable():
    """Operator can lower the floor if they understand the trade-off."""
    finding = evaluate(
        restart_count=10,
        started_at=_now() - timedelta(minutes=30),
        now=_now(),
        min_uptime_hours=0.1,  # 6 min
    )
    # rate = 10 / 0.5 = 20/h → "high"
    assert finding is not None
    assert finding.severity == "high"


def test_restart_freq_below_min_restart_count_returns_none():
    """Existing behaviour preserved: 2 restarts is noise floor."""
    finding = evaluate(
        restart_count=2, started_at=_now() - timedelta(hours=10), now=_now(),
    )
    assert finding is None


def test_restart_freq_no_started_at_returns_none():
    """Existing behaviour preserved: missing uptime → can't compute."""
    finding = evaluate(restart_count=10, started_at=None, now=_now())
    assert finding is None


# ─── I12: fetch_docker_logs caps + log_anomaly tighter window ─────


def test_fetch_docker_logs_truncates_long_lines(monkeypatch):
    """A 30 KB JSON log entry per line shouldn't return 30 KB strings."""
    big_line = "x" * 30_000
    raw = "\n".join([big_line] * 5).encode()

    class _C:
        def logs(self, **_kw): return raw

    class _Containers:
        def get(self, _name): return _C()

    class _Client:
        containers = _Containers()

    class _SDK:
        @staticmethod
        def DockerClient(**_kw): return _Client()

    import sys
    monkeypatch.setitem(sys.modules, "docker", _SDK)
    lines = fetch_docker_logs(
        "x", since=None, socket="/var/run/docker.sock",
        max_line_bytes=200,
    )
    assert len(lines) == 5
    assert all(len(ln) <= 200 for ln in lines)


def test_fetch_docker_logs_passes_max_lines_to_sdk(monkeypatch):
    """`max_lines` becomes the SDK's `tail=` kwarg so the wire stays small."""
    seen_kwargs = {}

    class _C:
        def logs(self, **kw):
            seen_kwargs.update(kw)
            return b""

    class _Containers:
        def get(self, _name): return _C()

    class _Client:
        containers = _Containers()

    class _SDK:
        @staticmethod
        def DockerClient(**_kw): return _Client()

    import sys
    monkeypatch.setitem(sys.modules, "docker", _SDK)
    fetch_docker_logs("x", since=None, max_lines=250)
    assert seen_kwargs["tail"] == 250


def test_scan_container_uses_tight_cap(monkeypatch):
    """log_anomaly should ask for far fewer lines than the CSI default
    of 4000 — we only count regex hits, no LLM context needed."""
    seen_caps = []

    def _fake_fetch(name, *, since, socket=None, max_lines=4000, **kw):
        seen_caps.append(max_lines)
        return ["ERROR x"]

    import homelabsage.log_anomaly as la
    monkeypatch.setattr(la, "fetch_docker_logs", _fake_fetch)
    scan_container("c", lookback_minutes=15)
    assert seen_caps == [max(500, 15 * 200)]
    assert seen_caps[0] < 4000  # tighter than CSI default
