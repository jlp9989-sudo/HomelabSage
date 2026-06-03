"""Per-push-output quiet hours.

Why: severity-based gating is great when a critical alert legitimately
should wake the user up. Most homelab notifications are not that — they
are weekly-rollup-noise that ambient-light a phone at 03:00. The
parity gate handles array activity; the digest covers backstop. Neither
handles "I do not want my phone making noise between 23:00 and 07:00".

This module is the missing piece. Each push output (Telegram / Discord /
Ntfy / Gotify) gains a `quiet_hours: "23:00-07:00"` field. When the
local time (in `quiet_hours.timezone`, default UTC) falls inside the
window, the push is queued into `pending_dispatches` (the same table
the parity gate uses) and replays on the next ungated scan.

Composes with parity_gate: both gates run, both queue into the same
table, the auto-flush works for both. Critical-severity messages can
optionally bypass quiet hours via `bypass_severity: critical`.

Pure-data; never schedules its own jobs. The engine asks
`is_quiet_now(cfg)` at dispatch time and the answer is yes / no.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time

from .models import Severity

log = logging.getLogger(__name__)


@dataclass
class QuietState:
    """Result of a quiet-hours probe — composable with `ParityState`."""

    quiet: bool
    reason: str

    def __bool__(self) -> bool:
        return self.quiet


def parse_window(spec: str) -> tuple[time, time] | None:
    """Parse `HH:MM-HH:MM` into (start, end). None for unparseable / empty.

    The two times are *inclusive on the start*, *exclusive on the end*:
    `23:00-07:00` covers 23:00 through 06:59. We allow the window to
    cross midnight — `23:00-07:00` is a 8-hour overnight window, not
    the empty interval `[23:00, 07:00)` going forward in time.
    """
    if not spec or spec.strip() == "":
        return None
    try:
        start_str, end_str = spec.strip().split("-", 1)
        start = time.fromisoformat(start_str.strip())
        end = time.fromisoformat(end_str.strip())
    except (ValueError, AttributeError) as e:
        log.warning("quiet_hours: cannot parse window %r: %s", spec, e)
        return None
    return start, end


def time_in_window(now_t: time, window: tuple[time, time]) -> bool:
    """True iff `now_t` falls inside the window.

    Handles wrap-around: `23:00-07:00` matches 23:30 and 06:59.
    """
    start, end = window
    if start == end:
        return False  # zero-width window
    if start < end:
        return start <= now_t < end
    # Crosses midnight
    return now_t >= start or now_t < end


def is_quiet_now(
    *,
    window_spec: str,
    timezone_name: str = "UTC",
    now: datetime | None = None,
) -> QuietState:
    """One call per push dispatch — answer "is this a quiet moment?".

    Best-effort: an unparseable spec or unknown timezone is treated as
    "not quiet" (logged at WARNING) rather than failing the dispatch.
    """
    window = parse_window(window_spec)
    if window is None:
        return QuietState(quiet=False, reason="no window configured")
    if now is None:
        from ._time import utcnow
        now = utcnow()
    # Resolve to local in the configured tz
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        tz = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        log.warning("quiet_hours: unknown timezone %r: %s", timezone_name, e)
        return QuietState(quiet=False, reason="unknown timezone")
    local = now.astimezone(tz)
    in_window = time_in_window(local.time(), window)
    if in_window:
        return QuietState(
            quiet=True,
            reason=(
                f"local time {local.strftime('%H:%M')} ({timezone_name}) "
                f"inside quiet window {window_spec}"
            ),
        )
    return QuietState(quiet=False, reason="outside quiet window")


def should_bypass(severity: Severity | None, bypass_at: str | None) -> bool:
    """Return True iff `severity` is high enough to escape the quiet gate.

    `bypass_at` is the configured floor (one of `critical`, `high`,
    `medium`, `info`, or empty/`null` for no bypass). Empty == "the
    quiet window applies regardless of severity" — the strict reading.
    """
    if not bypass_at:
        return False
    if severity is None:
        return False
    floor = (bypass_at or "").strip().lower()
    order = {"info": 0, "medium": 1, "high": 2, "critical": 3}
    if floor not in order:
        return False
    return severity.order >= order[floor]


__all__ = [
    "QuietState",
    "is_quiet_now",
    "parse_window",
    "time_in_window",
    "should_bypass",
]
