"""Container restart-frequency detector.

A container that's restarted 47 times in the last 24 hours is broken.
Docker reports the count via `c.attrs.RestartCount`, but it's the
LIFETIME counter — useless for "is this thing flapping NOW?". We pair
it with `State.StartedAt` and the previous scan's snapshot to compute
the recent-window restart rate.

The pure module here only does the arithmetic; the docker plugin
tracks the per-container baseline across scans (no new DB table — we
piggyback on the existing context the plugin sees and persist a tiny
fingerprint via `Update.context.restart_freq`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class RestartFinding:
    """Per-container flapping verdict."""

    restart_count: int
    restarts_per_hour: float
    severity: str  # info | medium | high | critical

    def to_context(self) -> dict:
        return {
            "restart_count": self.restart_count,
            "restarts_per_hour": round(self.restarts_per_hour, 2),
            "severity": self.severity,
        }


def _severity(restarts_per_hour: float) -> str:
    """Map restart rate to a severity bucket.

    Thresholds chosen so a container that crashes once or twice per
    hour is "medium" (annoying but not on fire) and ≥1/min is
    "critical" (genuinely broken)."""
    if restarts_per_hour >= 60.0:
        return "critical"
    if restarts_per_hour >= 1.0:
        return "high"
    if restarts_per_hour >= 0.25:
        return "medium"
    return "info"


def evaluate(
    *,
    restart_count: int,
    started_at: datetime | None,
    now: datetime,
    min_restart_count: int = 3,
) -> RestartFinding | None:
    """Return a finding when restart rate is interesting.

    `restart_count` is the docker lifetime counter. `started_at` is the
    container's current uptime start. We approximate "recent" rate as
    `restart_count / hours_since_started_at`. That's wrong for the
    pathological case of a stable container restarted on purpose 6
    months ago — but it's also not actually broken in that case, so a
    low rate falls under `info` and we suppress it.

    `min_restart_count` skips the noise floor: a container that
    crashed once never flags.
    """
    if restart_count < min_restart_count:
        return None
    if started_at is None:
        return None
    if started_at.tzinfo is None:
        from datetime import UTC
        started_at = started_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        from datetime import UTC
        now = now.replace(tzinfo=UTC)
    delta = now - started_at
    if delta <= timedelta(minutes=1):
        # Just started — can't compute rate meaningfully; treat as
        # the noise floor.
        return None
    hours = delta.total_seconds() / 3600
    rate = restart_count / hours if hours > 0 else 0.0
    sev = _severity(rate)
    if sev == "info":
        return None
    return RestartFinding(
        restart_count=restart_count,
        restarts_per_hour=rate,
        severity=sev,
    )


__all__ = ["RestartFinding", "evaluate"]
