"""Skip scheduled scans during a quiet window.

Distinct from `quiet_hours.py`: that one queues PUSH notifications
during sleep hours; this skips the SCAN entirely. Reason for the
split: a user may want sub-hour scans during the day for fast
feedback but skip overnight scans because the LLM backend is shared
with a workstation that's asleep or because their ISP off-peak
window matters.

We reuse `quiet_hours.parse_window` + `time_in_window` so the same
`HH:MM-HH:MM` semantics and cross-midnight handling apply.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from .quiet_hours import parse_window, time_in_window

log = logging.getLogger(__name__)


@dataclass
class ScanWindowState:
    """Result of the probe — `__bool__` lets callers `if state: skip()`."""

    in_quiet_window: bool
    reason: str

    def __bool__(self) -> bool:
        return self.in_quiet_window


def is_scan_blocked(
    *,
    enabled: bool,
    window_spec: str,
    timezone_name: str = "UTC",
    now: datetime | None = None,
) -> ScanWindowState:
    """Should the engine skip starting a scan right now?

    Best-effort: bad timezone or unparseable window logs and returns
    `False` — we never silently swallow scans because of a typo.
    """
    if not enabled:
        return ScanWindowState(False, "scan_window disabled")
    window = parse_window(window_spec)
    if window is None:
        return ScanWindowState(False, "no window configured")
    if now is None:
        from ._time import utcnow
        now = utcnow()
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        tz = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        log.warning("scan_window: unknown timezone %r: %s", timezone_name, e)
        return ScanWindowState(False, "unknown timezone")
    local = now.astimezone(tz)
    if time_in_window(local.time(), window):
        return ScanWindowState(
            True,
            (
                f"local time {local.strftime('%H:%M')} ({timezone_name}) "
                f"inside scan-quiet window {window_spec}"
            ),
        )
    return ScanWindowState(False, "outside scan-quiet window")


__all__ = ["ScanWindowState", "is_scan_blocked"]
