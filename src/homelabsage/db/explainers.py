"""Analysis explainers — store the LLM prompt + raw response per update.

Powers the "Why is this verdict?" UI card. One row per analysed update;
re-analyses overwrite (UPSERT on update_id) so the explainer always
reflects the LATEST verdict the user is looking at.
"""

from __future__ import annotations

import sqlite3

from .._time import parse_iso, utcnow


class ExplainersMixin:
    """Methods touching the `analysis_explainers` table."""

    _conn: sqlite3.Connection

    def upsert_explainer(
        self,
        update_id: str,
        *,
        prompt: str,
        raw_response: str,
        notes_used: str | None,
        provider: str | None,
        model: str | None,
    ) -> None:
        """Store the (prompt, raw_response, …) trio for one update.

        Idempotent: re-analysing the same update replaces the previous
        explainer (the prompt and the response BOTH change since we
        ask again). We never accumulate history — the user wants to
        audit the LATEST decision.
        """
        self._conn.execute(
            """
            INSERT INTO analysis_explainers
                (update_id, prompt, raw_response, notes_used, provider, model, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(update_id) DO UPDATE SET
                prompt       = excluded.prompt,
                raw_response = excluded.raw_response,
                notes_used   = excluded.notes_used,
                provider     = excluded.provider,
                model        = excluded.model,
                created_at   = excluded.created_at
            """,
            (
                update_id, prompt, raw_response, notes_used,
                provider, model, utcnow().isoformat(),
            ),
        )

    def get_explainer(self, update_id: str) -> dict | None:
        """Return the explainer dict for an update, or None if not stored."""
        row = self._conn.execute(
            "SELECT * FROM analysis_explainers WHERE update_id = ?",
            (update_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "update_id": row["update_id"],
            "prompt": row["prompt"],
            "raw_response": row["raw_response"],
            "notes_used": row["notes_used"],
            "provider": row["provider"],
            "model": row["model"],
            "created_at": parse_iso(row["created_at"]),
        }

    def list_explained_ids(self) -> set[str]:
        """Return the set of update_ids that have a stored explainer.

        Used by the updates index to decide whether to show an
        `explain` link per row — one query instead of one-per-row.
        """
        rows = self._conn.execute(
            "SELECT update_id FROM analysis_explainers",
        ).fetchall()
        return {r["update_id"] for r in rows}
