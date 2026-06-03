"""Tests for the release-cadence stagnation detector."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homelabsage.release_cadence import (
    CadenceFinding,
    _severity_from_ratio,
    detect_stagnation,
)


def _release(when: datetime, *, prerelease: bool = False, draft: bool = False) -> dict:
    return {
        "published_at": when.isoformat(),
        "prerelease": prerelease,
        "draft": draft,
        "tag_name": when.strftime("v%Y.%m.%d"),
    }


# ─── severity ladder ────────────────────────────────────────────────


def test_severity_buckets():
    assert _severity_from_ratio(1.5, medium_at=2.0, high_at=4.0) == "info"
    assert _severity_from_ratio(2.0, medium_at=2.0, high_at=4.0) == "medium"
    assert _severity_from_ratio(3.9, medium_at=2.0, high_at=4.0) == "medium"
    assert _severity_from_ratio(4.0, medium_at=2.0, high_at=4.0) == "high"
    assert _severity_from_ratio(10.0, medium_at=2.0, high_at=4.0) == "high"


# ─── detect_stagnation ──────────────────────────────────────────────


def test_detect_returns_none_below_min_samples():
    """3 releases < min_releases=5 → no signal."""
    now = datetime(2026, 6, 1, tzinfo=UTC)
    releases = [
        _release(now - timedelta(days=10)),
        _release(now - timedelta(days=40)),
        _release(now - timedelta(days=70)),
    ]
    assert detect_stagnation(releases, now=now) is None


def test_detect_returns_none_when_healthy():
    """Latest release within the median cadence → no flag."""
    now = datetime(2026, 6, 1, tzinfo=UTC)
    releases = [_release(now - timedelta(days=10 * i)) for i in range(8)]
    # median gap ≈ 10 days; current gap = 10 days → ratio 1.0
    assert detect_stagnation(releases, now=now) is None


def test_detect_fires_medium_when_2x_overdue():
    """Median 10d, current gap 22d → ratio 2.2 → medium."""
    now = datetime(2026, 6, 1, tzinfo=UTC)
    # 7 releases each 10 days apart, then a 22-day gap to "now"
    base = now - timedelta(days=22)
    releases = [_release(base - timedelta(days=10 * i)) for i in range(7)]
    finding = detect_stagnation(releases, now=now)
    assert finding is not None
    assert isinstance(finding, CadenceFinding)
    assert finding.severity == "medium"
    assert finding.median_days == 10.0
    assert finding.current_gap_days == 22.0
    assert finding.overdue_ratio == 2.2


def test_detect_fires_high_when_4x_overdue():
    """Median 10d, current gap 50d → ratio 5 → high."""
    now = datetime(2026, 6, 1, tzinfo=UTC)
    base = now - timedelta(days=50)
    releases = [_release(base - timedelta(days=10 * i)) for i in range(7)]
    finding = detect_stagnation(releases, now=now)
    assert finding is not None
    assert finding.severity == "high"


def test_detect_skips_drafts_and_prereleases():
    """Drafts/prereleases shouldn't count toward the median."""
    now = datetime(2026, 6, 1, tzinfo=UTC)
    base = now - timedelta(days=30)
    rels: list[dict] = []
    for i in range(7):
        rels.append(_release(base - timedelta(days=10 * i)))
    # Add a draft + prerelease that would skew the median if counted
    rels.append(_release(now - timedelta(days=1), draft=True))
    rels.append(_release(now - timedelta(days=2), prerelease=True))
    finding = detect_stagnation(rels, now=now)
    assert finding is not None
    assert finding.median_days == 10.0


def test_detect_handles_malformed_timestamps():
    """Bad timestamps must not crash; they're just skipped."""
    now = datetime(2026, 6, 1, tzinfo=UTC)
    base = now - timedelta(days=30)
    rels = [_release(base - timedelta(days=10 * i)) for i in range(6)]
    rels.append({"published_at": "not-a-date", "draft": False, "prerelease": False})
    finding = detect_stagnation(rels, now=now)
    assert finding is not None
    # Bad row dropped; remaining 6 still produce a valid median


def test_to_context_round_trips():
    f = CadenceFinding(
        sample_count=10, median_days=14.5,
        current_gap_days=60.0, overdue_ratio=4.14, severity="high",
    )
    ctx = f.to_context()
    assert ctx["sample_count"] == 10
    assert ctx["median_days"] == 14.5
    assert ctx["overdue_ratio"] == 4.14
    assert ctx["severity"] == "high"


def test_detect_naive_now_upgraded():
    """Caller passing naive `now` shouldn't crash."""
    base = datetime(2026, 5, 1, tzinfo=UTC)
    rels = [_release(base - timedelta(days=10 * i)) for i in range(7)]
    # Naive now ~ 31 days after base
    naive = datetime(2026, 6, 1)
    finding = detect_stagnation(rels, now=naive)
    # ~31 days vs median 10 → ratio 3.1 → medium
    assert finding is not None
    assert finding.severity == "medium"


def test_detect_returns_none_when_median_zero():
    """All releases at the same instant — median 0 → can't compute ratio."""
    now = datetime(2026, 6, 1, tzinfo=UTC)
    same = now - timedelta(days=10)
    rels = [_release(same) for _ in range(7)]
    assert detect_stagnation(rels, now=now) is None
