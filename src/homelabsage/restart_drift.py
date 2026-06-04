"""Container last_started drift detector.

A container that restarted in the last hour without a corresponding
APPLIED update is either:
  - manually restarted by the user (fine, no signal)
  - crashed and restarted by docker (`restart_freq` catches if
    flapping, but a single crash slips through)
  - OOM-killed (a host-level signal we can't see directly)

The interesting case is "I'm looking at the dashboard at 09:00 and
mealie restarted at 08:53 — what happened?". We don't claim to know
the cause; we just surface "this is fresh" so the user clicks into
the logs to find out.

Pure function: takes `started_at`, current time, and an optional
`last_applied_at` from the updates DB. Returns a finding when started
within `min_hours` ago AND no apply event covers the restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class RestartDriftFinding:
    """Container started recently without an apparent cause."""

    started_at: str         # ISO 8601
    hours_since_start: float
    has_recent_apply: bool
    severity: str           # info

    def to_context(self) -> dict:
        return {
            "started_at": self.started_at,
            "hours_since_start": round(self.hours_since_start, 2),
            "has_recent_apply": self.has_recent_apply,
            "severity": self.severity,
        }


def evaluate(
    *,
    started_at: datetime | None,
    now: datetime,
    last_applied_at: datetime | None = None,
    min_hours: float = 0.25,    # 15 min — anything older isn't "fresh"
    max_hours: float = 4.0,     # don't flag day-old restarts as drift
    apply_window_hours: float = 1.0,
) -> RestartDriftFinding | None:
    """Return a finding when the restart looks unexplained.

    `min_hours` — too-recent restarts (the container is JUST starting)
    are skipped to avoid race with the scan.

    `max_hours` — beyond this the restart is old news and there's no
    actionable signal in surfacing it.

    `apply_window_hours` — if an APPLIED update for this container
    landed within this window of the restart, we attribute the
    restart to the apply (no flag).
    """
    if started_at is None:
        return None
    if started_at.tzinfo is None:
        from datetime import UTC
        started_at = started_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        from datetime import UTC
        now = now.replace(tzinfo=UTC)
    delta: timedelta = now - started_at
    hours = delta.total_seconds() / 3600
    if hours < min_hours:
        return None
    if hours > max_hours:
        return None
    has_recent_apply = False
    if last_applied_at is not None:
        if last_applied_at.tzinfo is None:
            from datetime import UTC
            last_applied_at = last_applied_at.replace(tzinfo=UTC)
        gap_hours = abs(
            (started_at - last_applied_at).total_seconds() / 3600
        )
        if gap_hours <= apply_window_hours:
            has_recent_apply = True
    if has_recent_apply:
        return None
    return RestartDriftFinding(
        started_at=started_at.isoformat(),
        hours_since_start=hours,
        has_recent_apply=False,
        severity="info",
    )


__all__ = ["RestartDriftFinding", "evaluate"]
