"""Pending push dispatches — parity-gate catch-up queue.

Each row marks one (update, push_output) pair waiting to fire. Populated by
the engine when a real-time push is skipped while parity is active; drained
on the next scan that finds the gate clear. The PRIMARY KEY enforces
idempotency, so a multi-day parity window never inflates the table per
scan.
"""

from __future__ import annotations

import sqlite3

from .._time import utcnow


class PendingMixin:
    """Methods touching the `pending_dispatches` table."""

    _conn: sqlite3.Connection

    def queue_pending_dispatch(self, update_id: str, output_id: str) -> None:
        """Mark `(update, output)` as needing a push next time the gate clears.

        Idempotent — the PRIMARY KEY on `(update_id, output_id)` collapses
        repeat calls so a multi-day parity window doesn't grow the table
        per scan.
        """
        self._conn.execute(
            """
            INSERT OR IGNORE INTO pending_dispatches
                (update_id, output_id, queued_at)
            VALUES (?, ?, ?)
            """,
            (update_id, output_id, utcnow().isoformat()),
        )

    def list_pending_dispatches(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT update_id, output_id, queued_at FROM pending_dispatches "
            "ORDER BY queued_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_pending_dispatch(self, update_id: str, output_id: str) -> None:
        self._conn.execute(
            "DELETE FROM pending_dispatches WHERE update_id = ? AND output_id = ?",
            (update_id, output_id),
        )
