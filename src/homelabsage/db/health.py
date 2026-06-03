"""Post-update health check + queue tables.

When an update flips to APPLIED the engine queues a probe N minutes out.
The probe scans recent logs for a small fixed list of bad signals
(silent CPU fallback from CUDA/ROCm, OOM, panics) and writes one row to
`health_checks` per probe. The auditor surfaces failures; the table
keeps a small history so the user can see whether a container has
flapped.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .._time import utcnow


class HealthCheckMixin:
    """Methods touching `health_checks` + `health_check_queue`."""

    _conn: sqlite3.Connection

    # ─── queue ─────────────────────────────────────────────────────────

    def queue_health_check(
        self, *, update_id: str, container_name: str, fire_at: datetime,
    ) -> None:
        """Insert (or replace) a queued probe. Re-applying the same update
        simply pushes `fire_at` forward — that's the right behaviour: it
        gives the freshly-restarted container the full grace window again."""
        if fire_at.tzinfo is None:
            from datetime import UTC
            fire_at = fire_at.replace(tzinfo=UTC)
        self._conn.execute(
            """
            INSERT INTO health_check_queue
                (update_id, container_name, queued_at, fire_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(update_id) DO UPDATE SET
                container_name = excluded.container_name,
                queued_at      = excluded.queued_at,
                fire_at        = excluded.fire_at
            """,
            (update_id, container_name, utcnow().isoformat(), fire_at.isoformat()),
        )

    def due_health_checks(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """All rows where `fire_at <= now`. Caller deletes after processing."""
        end = now or utcnow()
        if end.tzinfo is None:
            from datetime import UTC
            end = end.replace(tzinfo=UTC)
        rows = self._conn.execute(
            "SELECT * FROM health_check_queue WHERE fire_at <= ? ORDER BY fire_at ASC",
            (end.isoformat(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_health_check_queue(self, *, update_id: str) -> None:
        self._conn.execute(
            "DELETE FROM health_check_queue WHERE update_id = ?", (update_id,),
        )

    # ─── results ───────────────────────────────────────────────────────

    def record_health_check(
        self,
        *,
        update_id: str,
        container_name: str,
        ok: bool,
        signal: str | None = None,
        excerpt: str | None = None,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO health_checks
                (update_id, container_name, ok, signal, excerpt, checked_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                update_id, container_name, 1 if ok else 0,
                signal, (excerpt or "")[:500], utcnow().isoformat(),
            ),
        )
        return int(cur.lastrowid or 0)

    def list_recent_health_checks(
        self, *, limit: int = 200, days: int = 14,
    ) -> list[dict[str, Any]]:
        """Recent results, newest first. Default window matches the chronicle.

        Only returns rows newer than `days` days so the auditor doesn't
        keep flagging regressions that were fixed weeks ago.
        """
        cutoff = utcnow() - timedelta(days=days)
        rows = self._conn.execute(
            """
            SELECT * FROM health_checks
             WHERE checked_at >= ?
             ORDER BY id DESC LIMIT ?
            """,
            (cutoff.isoformat(), limit),
        ).fetchall()
        return [dict(r) for r in rows]
