"""Container age tracker.

A container created 18 months ago and never recreated is carrying:
  - The image digest of whenever the user last pulled.
  - The env vars they set up at the time, which may now be wrong.
  - File-permission state that's drifted with PUID changes.

Recreate ≠ restart. `docker restart` keeps everything; `docker compose
up -d` after an image change creates a NEW container with the same
name. We track the `Created` timestamp on `c.attrs.State.Created` (or
`c.attrs.Created` on older daemons) and emit a context flag when it
crosses a configurable age.

Heuristic only — some containers SHOULD live forever (storage daemons,
persistent IPC brokers). The analyzer prompt mentions but never blocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ._time import parse_docker_ts, utcnow


@dataclass
class AgeFinding:
    """Per-container container-age verdict."""

    days_old: int
    created_at: str | None  # ISO

    def to_context(self) -> dict:
        return {
            "days_old": self.days_old,
            "created_at": self.created_at,
        }


def _extract_created(attrs: dict) -> datetime | None:
    """Pull the container's `Created` timestamp from docker SDK attrs.

    Docker emits ISO-8601 with `Z` suffix and up to 9 fractional digits;
    `parse_docker_ts` handles the trim. Falls back silently when the field
    is missing or unparseable.
    """
    if not isinstance(attrs, dict):
        return None
    raw = attrs.get("Created") or (attrs.get("State") or {}).get("Created")
    if not isinstance(raw, str):
        return None
    return parse_docker_ts(raw)


def evaluate(
    attrs: dict,
    *,
    warn_after_days: int = 180,
    now: datetime | None = None,
) -> AgeFinding | None:
    """Return a finding when the container is older than `warn_after_days`."""
    created = _extract_created(attrs)
    if created is None:
        return None
    end = now or utcnow()
    if end.tzinfo is None:
        from datetime import UTC
        end = end.replace(tzinfo=UTC)
    if created.tzinfo is None:
        from datetime import UTC
        created = created.replace(tzinfo=UTC)
    days = max(0, int((end - created).total_seconds() // 86400))
    if days < warn_after_days:
        return None
    return AgeFinding(days_old=days, created_at=created.isoformat())


__all__ = ["AgeFinding", "evaluate"]
