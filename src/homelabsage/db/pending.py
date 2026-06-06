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

    def list_pending_dispatches(self, *, limit: int = 1000) -> list[dict]:
        """Oldest-first window of queued (update, output) pairs.

        N5: pre-v0.10.7 had no LIMIT and scaled linearly with the queue
        size. The PRIMARY KEY caps growth per (update_id, output_id) but
        a long parity window with many updates can still accumulate
        thousands of rows. Default 1000 is generous for the engine's
        flush loop, which drains one per scan anyway.
        """
        rows = self._conn.execute(
            "SELECT update_id, output_id, queued_at FROM pending_dispatches "
            "ORDER BY queued_at ASC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def purge_pending_dispatches_older_than(self, *, days: int) -> int:
        """Drop pending dispatches older than `days`.

        N3: a misconfigured push output during a long parity window
        keeps appending rows; nothing pruned them. After this TTL the
        operator can wire a periodic call (engine startup, scheduled
        task) to evict ancient rows that will never fire usefully.
        Returns the count purged. `days <= 0` is a no-op.
        """
        if days <= 0:
            return 0
        from datetime import timedelta
        cutoff = (utcnow() - timedelta(days=days)).isoformat()
        cur = self._conn.execute(
            "DELETE FROM pending_dispatches WHERE queued_at < ?",
            (cutoff,),
        )
        return int(cur.rowcount or 0)

    def delete_pending_dispatch(self, update_id: str, output_id: str) -> None:
        self._conn.execute(
            "DELETE FROM pending_dispatches WHERE update_id = ? AND output_id = ?",
            (update_id, output_id),
        )

    def clear_pending_dispatches(self) -> int:
        """Drop every row from the queue. Returns the count cleared.

        Useful for operator panic-button after a misconfigured output
        queued thousands of items, or after a parity gate stuck on a
        stale `mdstat` path. The flush loop normally drains rows one
        per cycle; this is the "I changed my mind" override.
        """
        cur = self._conn.execute("DELETE FROM pending_dispatches")
        return int(cur.rowcount or 0)
