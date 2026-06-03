"""Release-cadence stagnation detector.

A repo can be "active" by `repo_health` heuristics (recent commits exist)
while its *release* cadence has quietly slowed to a crawl. This module
spots that pattern: it computes the typical days-between-releases over
the last ~20 releases, then flags when the time since the most-recent
release exceeds the historical median by a configurable multiplier.

Why this is distinct from `repo_health`:
  - `repo_health.abandoned` requires NO commits / archived flag — a
    project still developing on `main` but cutting no releases for
    8 months passes that check.
  - This module operates on the release timeline only, so it catches
    "active dev, no releases" (the worst pre-fork signal).

Output: a `CadenceFinding` dataclass with `median_days`, `current_gap`,
`overdue_ratio`, attached to `Update.context.release_cadence` when the
gap crosses the threshold.

Pure deterministic math; no LLM. Best-effort: empty / single-release
repos return None.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime

from ._time import parse_iso, utcnow


@dataclass
class CadenceFinding:
    """Per-repo stagnation verdict."""

    sample_count: int          # how many releases informed the median
    median_days: float         # historical gap, days
    current_gap_days: float    # days since the most recent release
    overdue_ratio: float       # current_gap / median
    severity: str              # info | medium | high

    def to_context(self) -> dict:
        return {
            "sample_count": self.sample_count,
            "median_days": round(self.median_days, 1),
            "current_gap_days": round(self.current_gap_days, 1),
            "overdue_ratio": round(self.overdue_ratio, 2),
            "severity": self.severity,
        }


def _release_timestamps(
    releases: list[dict],
    *,
    include_prereleases: bool = False,
) -> list[datetime]:
    """Pull and parse `published_at` from each release, newest first.

    `published_at` is GitHub's canonical timestamp. Codeberg / Gitea
    use the same field name. Drafts are always skipped; prereleases
    are excluded by default because they fire on a different cadence
    than mainline releases and would skew the median.
    """
    out: list[datetime] = []
    for r in releases:
        if r.get("draft"):
            continue
        if r.get("prerelease") and not include_prereleases:
            continue
        when_raw = r.get("published_at") or r.get("created_at")
        if not when_raw:
            continue
        try:
            when = parse_iso(when_raw)
        except ValueError:
            continue
        if when:
            out.append(when)
    return out


def _severity_from_ratio(
    overdue_ratio: float,
    *,
    medium_at: float,
    high_at: float,
) -> str:
    """Map the overdue-ratio to a severity bucket."""
    if overdue_ratio >= high_at:
        return "high"
    if overdue_ratio >= medium_at:
        return "medium"
    return "info"


def detect_stagnation(
    releases: list[dict],
    *,
    min_releases: int = 5,
    medium_at: float = 2.0,
    high_at: float = 4.0,
    now: datetime | None = None,
    include_prereleases: bool = False,
) -> CadenceFinding | None:
    """Return a `CadenceFinding` when the latest gap crosses the threshold.

    Returns None when:
      - fewer than `min_releases` non-draft releases exist (no signal)
      - the latest gap is BELOW the medium threshold (everything fine)

    The default `min_releases=5` is the smallest sample where the
    median is meaningful for fast-moving projects; bigger samples
    smooth out monthly cadences nicely.
    """
    times = _release_timestamps(releases, include_prereleases=include_prereleases)
    if len(times) < min_releases:
        return None
    times.sort(reverse=True)  # newest first
    # Gaps between consecutive releases (in days)
    gaps_days: list[float] = []
    for i in range(len(times) - 1):
        delta = times[i] - times[i + 1]
        gaps_days.append(delta.total_seconds() / 86400)
    if not gaps_days:
        return None
    median_days = statistics.median(gaps_days)
    # The "current gap" is now - latest release.
    end = now or utcnow()
    if end.tzinfo is None:
        from datetime import UTC
        end = end.replace(tzinfo=UTC)
    latest = times[0]
    if latest.tzinfo is None:
        from datetime import UTC
        latest = latest.replace(tzinfo=UTC)
    current_gap = (end - latest).total_seconds() / 86400
    if median_days <= 0:
        return None
    ratio = current_gap / median_days
    severity = _severity_from_ratio(ratio, medium_at=medium_at, high_at=high_at)
    if severity == "info":
        return None
    return CadenceFinding(
        sample_count=len(times),
        median_days=median_days,
        current_gap_days=current_gap,
        overdue_ratio=ratio,
        severity=severity,
    )


__all__ = ["CadenceFinding", "detect_stagnation"]
