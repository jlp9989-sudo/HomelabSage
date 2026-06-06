"""Tests for v0.6.5: healthcheck-stale, disk-pressure, compose-override,
scan-window."""

from __future__ import annotations

from datetime import UTC, datetime

from homelabsage.compose_override import OverridePresence
from homelabsage.compose_override import scan as scan_overrides
from homelabsage.disk_pressure import DiskFinding, _severity
from homelabsage.disk_pressure import evaluate as eval_disk
from homelabsage.healthcheck_stale import (
    _severity as hs_severity,
)
from homelabsage.healthcheck_stale import (
    evaluate as eval_health,
)
from homelabsage.scan_window import is_scan_blocked

# ─── healthcheck_stale ──────────────────────────────────────────────


def test_health_severity_buckets():
    assert hs_severity(100, 5) == "critical"
    assert hs_severity(48, 5) == "high"
    assert hs_severity(6, 5) == "medium"
    assert hs_severity(1, 5) == "info"
    assert hs_severity(None, 5) == "medium"
    assert hs_severity(None, 1) == "info"


def test_health_skips_when_no_health_block():
    out = eval_health({}, now=datetime.now(UTC))
    assert out is None


def test_health_skips_when_starting():
    state = {"Health": {"Status": "starting", "FailingStreak": 0}}
    assert eval_health(state, now=datetime.now(UTC)) is None


def test_health_skips_when_healthy():
    state = {"Health": {"Status": "healthy", "FailingStreak": 0}}
    assert eval_health(state, now=datetime.now(UTC)) is None


def test_health_skips_below_min_streak():
    state = {"Health": {"Status": "unhealthy", "FailingStreak": 2}}
    assert eval_health(state, now=datetime.now(UTC)) is None


def test_health_unhealthy_returns_finding_with_no_log():
    state = {"Health": {"Status": "unhealthy", "FailingStreak": 5}}
    out = eval_health(state, now=datetime.now(UTC))
    assert out is not None
    assert out.failing_streak == 5
    assert out.severity == "medium"  # streak ≥3, no duration
    assert out.stale_since is None
    assert out.hours_unhealthy is None


def test_health_unhealthy_with_log_computes_duration():
    now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)
    state = {
        "Health": {
            "Status": "unhealthy",
            "FailingStreak": 5,
            "Log": [
                # 5 hours ago
                {"Start": "2026-06-04T07:00:00Z",
                 "End": "2026-06-04T07:00:05Z",
                 "ExitCode": 1, "Output": "fail"},
                {"Start": "2026-06-04T08:00:00Z",
                 "End": "2026-06-04T08:00:05Z",
                 "ExitCode": 1, "Output": "fail"},
            ],
        },
    }
    out = eval_health(state, now=now)
    assert out is not None
    assert out.hours_unhealthy is not None
    assert 4.9 <= out.hours_unhealthy <= 5.1
    assert out.severity == "medium"  # 5h ≥ 4h, < 24h


def test_health_unhealthy_72h_critical():
    now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)
    state = {
        "Health": {
            "Status": "unhealthy",
            "FailingStreak": 10,
            "Log": [
                {"Start": "2026-06-01T11:00:00Z",
                 "End": "2026-06-01T11:00:01Z",
                 "ExitCode": 1, "Output": ""},
            ],
        },
    }
    out = eval_health(state, now=now)
    assert out is not None
    assert out.severity == "critical"


def test_health_unhealthy_handles_nano_timestamps():
    """Docker may emit 9-digit nanosecond fractions Python rejects."""
    now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)
    state = {
        "Health": {
            "Status": "unhealthy",
            "FailingStreak": 4,
            "Log": [
                {"Start": "2026-06-04T08:00:00.123456789Z",
                 "End": "2026-06-04T08:00:01.987654321Z",
                 "ExitCode": 1, "Output": ""},
            ],
        },
    }
    out = eval_health(state, now=now)
    assert out is not None
    assert out.hours_unhealthy is not None


# ─── disk_pressure ──────────────────────────────────────────────────


def test_disk_severity_buckets():
    gib = 1024 ** 3
    # 4 GiB free is critical regardless of percent
    assert _severity(4 * gib, 50.0) == "critical"
    # 3% free is critical regardless of absolute
    assert _severity(500 * gib, 3.0) == "critical"
    # 15 GiB / 8% → high (both axes)
    assert _severity(15 * gib, 8.0) == "high"
    # 40 GiB / 15% → medium
    assert _severity(40 * gib, 15.0) == "medium"
    # 100 GiB / 50% → no finding
    assert _severity(100 * gib, 50.0) is None


def test_disk_evaluate_skips_nonexistent_paths():
    out = eval_disk(["/no/such/path/x12345"])
    assert out == []


def test_disk_evaluate_returns_findings_on_real_path(tmp_path):
    # Real filesystem — we don't know the actual free %; just verify
    # the function runs and either returns empty or a valid finding.
    out = eval_disk([str(tmp_path)])
    for f in out:
        assert isinstance(f, DiskFinding)
        assert f.severity in ("medium", "high", "critical")
        assert f.percent_free >= 0


def test_disk_evaluate_dedupes_same_device(tmp_path):
    """Two paths on the same st_dev collapse to one finding."""
    sub = tmp_path / "sub"
    sub.mkdir()
    out = eval_disk([str(tmp_path), str(sub)])
    # Either both skipped (high free%) or exactly one finding.
    assert len(out) <= 1


# ─── compose_override ──────────────────────────────────────────────


def test_compose_override_detects_pair(tmp_path):
    base = tmp_path / "docker-compose.yml"
    base.write_text("services: {}\n")
    ovr = tmp_path / "docker-compose.override.yml"
    ovr.write_text("services: {}\n")
    out = scan_overrides([str(tmp_path)])
    assert len(out) == 1
    assert out[0].base_path == str(base)
    assert out[0].override_path == str(ovr)


def test_compose_override_skips_when_no_base(tmp_path):
    """Lonely override file isn't reported — no base to override."""
    (tmp_path / "docker-compose.override.yml").write_text("services: {}\n")
    out = scan_overrides([str(tmp_path)])
    assert out == []


def test_compose_override_walks_children(tmp_path):
    """Dockge-style: stacks/<name>/docker-compose.yml + override."""
    stack = tmp_path / "mealie"
    stack.mkdir()
    (stack / "docker-compose.yml").write_text("services: {}\n")
    (stack / "docker-compose.override.yml").write_text("services: {}\n")
    out = scan_overrides([str(tmp_path)])
    assert len(out) == 1
    assert out[0].project_dir == str(stack)


def test_compose_override_accepts_modern_names(tmp_path):
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    (tmp_path / "compose.override.yaml").write_text("services: {}\n")
    out = scan_overrides([str(tmp_path)])
    assert len(out) == 1


def test_compose_override_handles_file_path(tmp_path):
    """When the user lists a file directly, look in its parent."""
    base = tmp_path / "docker-compose.yml"
    base.write_text("services: {}\n")
    (tmp_path / "docker-compose.override.yml").write_text("services: {}\n")
    out = scan_overrides([str(base)])
    assert len(out) == 1


def test_compose_override_no_duplicates_when_paths_overlap(tmp_path):
    """A dir AND its child both listed shouldn't double-report."""
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "docker-compose.override.yml").write_text("services: {}\n")
    out = scan_overrides([str(tmp_path), str(tmp_path)])
    assert len(out) == 1


def test_compose_override_to_context_shape():
    p = OverridePresence(
        base_path="/a/base.yml",
        override_path="/a/override.yml",
        project_dir="/a",
    )
    ctx = p.to_context()
    assert ctx["base_path"] == "/a/base.yml"
    assert ctx["override_path"] == "/a/override.yml"


# ─── scan_window ────────────────────────────────────────────────────


def test_scan_window_disabled_never_blocks():
    out = is_scan_blocked(enabled=False, window_spec="00:00-23:59")
    assert not out
    assert "disabled" in out.reason


def test_scan_window_empty_spec_never_blocks():
    out = is_scan_blocked(enabled=True, window_spec="")
    assert not out


def test_scan_window_inside_window_blocks():
    # 09:00 UTC, window 08:00-10:00 → blocked
    now = datetime(2026, 6, 4, 9, 0, 0, tzinfo=UTC)
    out = is_scan_blocked(
        enabled=True, window_spec="08:00-10:00",
        timezone_name="UTC", now=now,
    )
    assert out
    assert "inside scan-quiet window" in out.reason


def test_scan_window_outside_window_passes():
    now = datetime(2026, 6, 4, 11, 0, 0, tzinfo=UTC)
    out = is_scan_blocked(
        enabled=True, window_spec="08:00-10:00",
        timezone_name="UTC", now=now,
    )
    assert not out


def test_scan_window_wraps_midnight():
    # 02:00 UTC, window 23:00-07:00 → blocked
    now = datetime(2026, 6, 4, 2, 0, 0, tzinfo=UTC)
    out = is_scan_blocked(
        enabled=True, window_spec="23:00-07:00",
        timezone_name="UTC", now=now,
    )
    assert out


def test_scan_window_unknown_tz_does_not_block():
    """A typo in tz should never silently swallow scans."""
    out = is_scan_blocked(
        enabled=True, window_spec="00:00-23:59",
        timezone_name="Mars/Olympus",
    )
    assert not out
    assert "unknown timezone" in out.reason
