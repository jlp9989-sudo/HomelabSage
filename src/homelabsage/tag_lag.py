"""Tag-promotion-lag detector — measure days between the registry's
last push of a floating tag (`latest` / `main` / `stable`) and the local
container's image pull date.

The floating-tag tracker already attaches `remote_pushed_at` +
`local_pulled_at` to `Update.context` for every container whose image
tag isn't a semver. This module turns those two timestamps into a
single `tag_lag` block the auditor reads:

    {
        "days_since_remote_push": int,
        "days_local_behind": int,
        "severity": "info" | "medium" | "high",
    }

`days_local_behind` is the user-facing number — "how stale am I?". The
detector is conservative: a tag pushed yesterday and pulled yesterday
is healthy (`info`) even if the user has been running it for months.
We only fire when the registry shows movement the local copy missed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ._time import parse_iso, utcnow


@dataclass
class TagLag:
    """Per-update lag verdict."""

    days_since_remote_push: int | None
    days_local_behind: int | None
    severity: str

    def to_context(self) -> dict:
        return {
            "days_since_remote_push": self.days_since_remote_push,
            "days_local_behind": self.days_local_behind,
            "severity": self.severity,
        }


def _delta_days(a: datetime | None, b: datetime | None) -> int | None:
    if a is None or b is None:
        return None
    return max(0, int((b - a).total_seconds() // 86400))


def _severity_from_lag(
    days_local_behind: int | None,
    *,
    warn_after_days: int,
    critical_after_days: int,
) -> str:
    if days_local_behind is None:
        return "info"
    if days_local_behind >= critical_after_days:
        return "high"
    if days_local_behind >= warn_after_days:
        return "medium"
    return "info"


def derive_tag_lag(
    context: dict,
    *,
    now: datetime | None = None,
    warn_after_days: int = 14,
    critical_after_days: int = 60,
) -> TagLag | None:
    """Return a TagLag for an Update.context with the floating-tag fields.

    Returns None when the context doesn't carry both timestamps — the
    detector only fires for the floating-tag pipeline. The analyzer
    treats absent `tag_lag` as "no signal", not "no issue".
    """
    remote_raw = context.get("remote_pushed_at")
    local_raw = context.get("local_pulled_at")
    if not remote_raw and not local_raw:
        return None
    end = now or utcnow()
    if end.tzinfo is None:
        from datetime import UTC
        end = end.replace(tzinfo=UTC)

    remote = parse_iso(remote_raw) if remote_raw else None
    local = parse_iso(local_raw) if local_raw else None

    days_since_remote = _delta_days(remote, end) if remote else None
    days_behind: int | None
    if remote and local:
        # Local-behind only counts when remote is strictly newer than local.
        days_behind = _delta_days(local, remote) if remote > local else 0
    elif remote and not local:
        # Registry has a timestamp but local doesn't — treat as full
        # behind so the user investigates.
        days_behind = days_since_remote
    else:
        days_behind = None

    return TagLag(
        days_since_remote_push=days_since_remote,
        days_local_behind=days_behind,
        severity=_severity_from_lag(
            days_behind,
            warn_after_days=warn_after_days,
            critical_after_days=critical_after_days,
        ),
    )


__all__ = ["TagLag", "derive_tag_lag"]
