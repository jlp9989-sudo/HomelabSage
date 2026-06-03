"""Per-output retry queue with exponential backoff.

The parity_gate + quiet_hours pipelines already queue items via
`pending_dispatches` when delivery is deliberately deferred. This
module piggybacks on the same table for the failure case: when an
output's `send()` raises (5xx, connection reset, DNS hiccup), the
engine adds a `retry_attempts` counter and the row gets retried with
backoff on the next ungated scan.

Backoff schedule (per attempt):
  1 → 0 minutes  (next scan)
  2 → 15 minutes
  3 → 1 hour
  4 → 6 hours
  5 → 24 hours
  6 → give up, mark `permanently_failed`

We track attempts via a NEW column on `pending_dispatches` so we don't
need a separate table. Forward-only ALTER triggers at migrate time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Index = attempts so far; 0-based for clarity (`_BACKOFF[0]` is the
# first retry's delay, applied AFTER the original failure).
_BACKOFF_MINUTES: tuple[int, ...] = (0, 15, 60, 360, 1440)
_GIVE_UP_AFTER = len(_BACKOFF_MINUTES)


@dataclass
class RetryDecision:
    """What `should_retry_now` returns."""

    should_retry: bool
    next_attempt_at: datetime | None  # None when give-up
    permanently_failed: bool


def next_delay_minutes(attempts: int) -> int | None:
    """Return the wait between this failure and the next attempt.

    `attempts` is the number of attempts that have ALREADY happened
    (1 == the original failed; the first retry has attempts=1). None
    means "give up, mark permanently_failed".
    """
    if attempts < 1:
        return _BACKOFF_MINUTES[0]
    if attempts >= _GIVE_UP_AFTER:
        return None
    return _BACKOFF_MINUTES[attempts]


def should_retry_now(
    *,
    attempts: int,
    last_attempt_at: datetime,
    now: datetime,
) -> RetryDecision:
    """Decide whether to retry an output dispatch right now.

    `attempts` is the count of attempts that already happened. We
    compute the required wait for the NEXT attempt and check whether
    `now - last_attempt_at >= wait`.
    """
    delay_m = next_delay_minutes(attempts)
    if delay_m is None:
        return RetryDecision(
            should_retry=False, next_attempt_at=None, permanently_failed=True,
        )
    if last_attempt_at.tzinfo is None:
        from datetime import UTC
        last_attempt_at = last_attempt_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        from datetime import UTC
        now = now.replace(tzinfo=UTC)
    next_at = last_attempt_at + timedelta(minutes=delay_m)
    return RetryDecision(
        should_retry=now >= next_at,
        next_attempt_at=next_at,
        permanently_failed=False,
    )


__all__ = [
    "RetryDecision",
    "next_delay_minutes",
    "should_retry_now",
]
