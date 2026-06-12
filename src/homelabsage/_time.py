"""Single source of truth for `datetime.now()` calls.

`datetime.utcnow()` is deprecated since Python 3.12 and the deprecation
warning turns into removal in 3.14. Migrating one call at a time leaves the
codebase in a mixed state; the recipe is "import `utcnow` from here, never
call the stdlib version".

The helper returns tz-AWARE `datetime` objects (`UTC`). Old SQLite rows
written by HomelabSage ≤ v0.2 stored naive UTC ISO strings; the parser
below upgrades them to tz-aware at read time so comparisons against new
`utcnow()` calls don't raise `TypeError: can't compare offset-naive and
offset-aware`. Cheap, safe, no migration script needed.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Tz-aware `datetime` representing "now" in UTC."""
    return datetime.now(UTC)


def parse_iso(text: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime string and ensure tz-awareness.

    Returns `None` for falsy input. Always emits UTC for strings without
    an explicit offset (the convention HomelabSage uses everywhere). Any
    unparseable string raises `ValueError` — there's no silently-wrong
    fallback because that would mask real data corruption.
    """
    if not text:
        return None
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def parse_docker_ts(text: str | None) -> datetime | None:
    """Parse a Docker-emitted timestamp, tolerating 9-digit nanoseconds.

    Docker inspect emits RFC 3339 with nanosecond precision
    (`2026-06-12T09:00:26.123456789Z`); Python's `fromisoformat` accepts
    at most microseconds, so the fractional part is trimmed to 6 digits
    before parsing. This used to be copy-pasted in three detectors —
    import it from here instead.

    Returns `None` for falsy input, for docker's `0001-01-01…` "unset"
    sentinel, or for an unparseable string (docker metadata is
    best-effort: a malformed timestamp means "no signal", never a
    crashed scan — unlike `parse_iso`, which guards data WE wrote and
    raises on corruption).
    """
    if not text or text.startswith("0001-"):
        return None
    s = text
    if "." in s:
        head, _, tail = s.partition(".")
        tz = ""
        for marker in ("Z", "+", "-"):
            i = tail.find(marker)
            if i != -1:
                tz = tail[i:]
                tail = tail[:i]
                break
        s = f"{head}.{tail[:6]}{tz}"
    try:
        return parse_iso(s)
    except ValueError:
        return None
