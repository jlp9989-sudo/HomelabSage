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
    min_uptime_hours: float = 1.0,
) -> RestartFinding | None:
    """Return a finding when restart rate is interesting.

    `restart_count` is the docker LIFETIME counter; `started_at` is the
    container's *current* uptime start. We approximate "recent" rate as
    `restart_count / hours_since_started_at`.

    C4 (v1.0 punch list): the old version flagged stable containers as
    "critical" after a routine manual restart — a 47-crash-across-90-days
    container restarted 5 min ago computed `rate = 47 / 0.083 = 564/h`.
    To suppress that false-positive we require **at least one hour of
    uptime** before emitting any rate-based finding. Below that the
    rate is too distorted by the short window to be trustworthy —
    short-window flapping will resurface on the next scan once
    `delta >= 1h`. (A full fix would compute the rate against the
    container event log; tracked as future work.)

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
    if delta <= timedelta(hours=min_uptime_hours):
        # Too soon after the most recent restart to trust the rate —
        # `restart_count` is lifetime, `delta` is uptime, the ratio
        # spikes pathologically when uptime is small.
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
