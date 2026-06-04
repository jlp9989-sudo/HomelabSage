"""Healthcheck staleness detector.

A container can be `unhealthy` without crashing — `restart: always`
keeps it running but the healthcheck has been failing for hours.
Restart-frequency doesn't catch this (no restarts; it's running),
and `restart_freq.evaluate` returns None.

We read `State.Health` from `docker inspect`:

    {"Status": "unhealthy", "FailingStreak": 27,
     "Log": [{"Start": "...", "End": "...", "ExitCode": 1, "Output": "..."}]}

`Log` is capped at the last 5 entries by docker. The earliest `End`
timestamp + `FailingStreak` give us "how long has this been broken?".
We approximate "stale since" as the `End` of the OLDEST log entry
when `FailingStreak >= len(log)` (i.e. every retained entry failed),
falling back to the most recent `End` otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ._time import parse_iso


@dataclass
class HealthStaleFinding:
    """Per-container unhealthy verdict."""

    status: str             # unhealthy | starting | none
    failing_streak: int
    stale_since: str | None     # ISO when known
    hours_unhealthy: float | None
    severity: str           # info | medium | high | critical

    def to_context(self) -> dict:
        return {
            "status": self.status,
            "failing_streak": self.failing_streak,
            "stale_since": self.stale_since,
            "hours_unhealthy": (
                round(self.hours_unhealthy, 2)
                if self.hours_unhealthy is not None else None
            ),
            "severity": self.severity,
        }


def _severity(hours: float | None, streak: int) -> str:
    """Map duration + streak to severity.

    Long-failing containers escalate by HOW LONG more than by streak —
    a streak of 1000 in 5 minutes is a flapping bug for restart_freq
    to catch; a streak of 5 over 48 hours is a slow burn that needs
    eyes on it.
    """
    if hours is None:
        # No timestamps but a streak ≥ 3 still rates a flag.
        return "medium" if streak >= 3 else "info"
    if hours >= 72:
        return "critical"
    if hours >= 24:
        return "high"
    if hours >= 4:
        return "medium"
    return "info"


def evaluate(
    state: dict,
    *,
    now: datetime,
    min_streak: int = 3,
) -> HealthStaleFinding | None:
    """Return a finding when a container has been unhealthy for a while.

    `state` is `container.attrs["State"]` from the docker SDK. We pull
    `State.Health.{Status,FailingStreak,Log}`. Returns None when:
    - there's no healthcheck (`Health` absent or `Status` == "none")
    - status is "healthy" or "starting" (starting → too early to judge)
    - failing streak below `min_streak`

    Best-effort: malformed log entries or timestamps degrade silently.
    """
    health = state.get("Health") if isinstance(state, dict) else None
    if not isinstance(health, dict):
        return None
    status = (health.get("Status") or "").lower()
    if status not in ("unhealthy",):
        return None
    streak = int(health.get("FailingStreak") or 0)
    if streak < min_streak:
        return None

    log = health.get("Log")
    if not isinstance(log, list) or not log:
        return HealthStaleFinding(
            status=status, failing_streak=streak,
            stale_since=None, hours_unhealthy=None,
            severity=_severity(None, streak),
        )

    # If every retained log entry failed, the OLDEST `End` is our
    # best lower bound on when health broke. Otherwise the streak
    # started after some earlier entries succeeded — use the most
    # recent successful→failing transition.
    failed_entries = [
        e for e in log
        if isinstance(e, dict) and int(e.get("ExitCode") or 0) != 0
    ]
    if not failed_entries:
        # Status says unhealthy but no failing entries — partial data.
        return HealthStaleFinding(
            status=status, failing_streak=streak,
            stale_since=None, hours_unhealthy=None,
            severity=_severity(None, streak),
        )

    anchor: dict
    if streak >= len(log) and len(failed_entries) == len(log):
        anchor = failed_entries[0]
    else:
        # First failing entry after the last passing one.
        anchor = failed_entries[0]
        for entry in log:
            if (isinstance(entry, dict)
                    and int(entry.get("ExitCode") or 0) != 0):
                anchor = entry
                break

    end_raw = anchor.get("End") or anchor.get("Start") or ""
    stale_since_dt: datetime | None = None
    if isinstance(end_raw, str) and end_raw:
        # Trim 9-digit nanos same as docker plugin does.
        s = end_raw
        if "." in s:
            head, _, tail = s.partition(".")
            tz = ""
            for m in ("Z", "+", "-"):
                i = tail.find(m)
                if i != -1:
                    tz = tail[i:]
                    tail = tail[:i]
                    break
            s = f"{head}.{tail[:6]}{tz}"
        try:
            stale_since_dt = parse_iso(s)
        except ValueError:
            stale_since_dt = None

    hours: float | None = None
    if stale_since_dt is not None:
        if stale_since_dt.tzinfo is None:
            from datetime import UTC
            stale_since_dt = stale_since_dt.replace(tzinfo=UTC)
        if now.tzinfo is None:
            from datetime import UTC
            now = now.replace(tzinfo=UTC)
        delta: timedelta = now - stale_since_dt
        hours = max(0.0, delta.total_seconds() / 3600)

    return HealthStaleFinding(
        status=status,
        failing_streak=streak,
        stale_since=stale_since_dt.isoformat() if stale_since_dt else None,
        hours_unhealthy=hours,
        severity=_severity(hours, streak),
    )


__all__ = ["HealthStaleFinding", "evaluate"]
