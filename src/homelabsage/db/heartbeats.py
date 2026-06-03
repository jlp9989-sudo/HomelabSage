"""Heartbeat history mixin.

Track every Uptime-Kuma / Healthchecks.io ping the engine emits so the
user can answer "is my monitoring actually hearing me?". Stored as one
row per attempt — success or failure. Failures keep enough detail
(`status_code`, `error`) to debug a misconfigured URL without spelunking
in the logs.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from .._time import utcnow


class HeartbeatsMixin:
    """Methods touching the `heartbeats` table."""

    _conn: sqlite3.Connection

    def record_heartbeat(
        self,
        *,
        url: str,
        ok: bool,
        status_code: int | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO heartbeats
                (url, ok, status_code, error, duration_ms, pinged_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                url, 1 if ok else 0, status_code,
                (error or "")[:500] if error else None,
                duration_ms, utcnow().isoformat(),
            ),
        )
        return int(cur.lastrowid or 0)

    def list_recent_heartbeats(
        self, *, limit: int = 100, hours: int = 48,
    ) -> list[dict[str, Any]]:
        cutoff = utcnow() - timedelta(hours=hours)
        rows = self._conn.execute(
            """
            SELECT * FROM heartbeats
             WHERE pinged_at >= ?
             ORDER BY id DESC LIMIT ?
            """,
            (cutoff.isoformat(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def heartbeat_summary(self, *, hours: int = 24) -> dict[str, Any]:
        """`{succeeded, failed, last_ok_at, last_failure_at}` over the window."""
        cutoff = utcnow() - timedelta(hours=hours)
        rows = self._conn.execute(
            """
            SELECT
                SUM(ok) AS succeeded,
                COUNT(*) - SUM(ok) AS failed,
                MAX(CASE WHEN ok = 1 THEN pinged_at END) AS last_ok_at,
                MAX(CASE WHEN ok = 0 THEN pinged_at END) AS last_failure_at
              FROM heartbeats
             WHERE pinged_at >= ?
            """,
            (cutoff.isoformat(),),
        ).fetchone()
        return {
            "window_hours": hours,
            "succeeded": int(row_val(rows, "succeeded") or 0),
            "failed": int(row_val(rows, "failed") or 0),
            "last_ok_at": row_val(rows, "last_ok_at"),
            "last_failure_at": row_val(rows, "last_failure_at"),
        }


def row_val(row, key: str) -> Any:
    """Safe getter — `None` when the column is absent or row is None."""
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, IndexError):
        return None
