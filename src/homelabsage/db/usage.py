"""LLM-usage tracking — per-call rows for cost/quota visibility.

The user runs against multiple providers (Halo local, Groq free tier,
Gemini paid Tier-1, OpenRouter). They've asked more than once "is the
free tier going to cover my scans?". This table answers honestly: row
per call, rolling sums by provider/model exposed via `/usage`.

Token counts come from the provider response when available
(`usage.prompt_tokens` + `usage.completion_tokens` for OpenAI-compat;
`prompt_eval_count` + `eval_count` for Ollama). When the provider omits
them (or refuses to populate them in error responses) we estimate via a
4-chars-per-token rule of thumb and set `estimated=1` so totals can be
qualified honestly.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .._time import utcnow

# Estimation constant — close enough for tracking; not for billing.
CHARS_PER_TOKEN_ESTIMATE = 4


def estimate_tokens(text: str) -> int:
    """Crude chars / 4 estimate. Used only when the provider omits counts."""
    return max(1, len(text) // CHARS_PER_TOKEN_ESTIMATE)


class UsageMixin:
    """Methods touching the `llm_usage` table."""

    _conn: sqlite3.Connection

    def record_llm_call(
        self,
        *,
        provider: str,
        model: str,
        update_id: str | None,
        tokens_in: int,
        tokens_out: int,
        estimated: bool,
        duration_ms: int,
        succeeded: bool,
    ) -> int:
        """Persist a single call. Returns the new row id."""
        cur = self._conn.execute(
            """
            INSERT INTO llm_usage
                (provider, model, update_id, tokens_in, tokens_out,
                 estimated, duration_ms, succeeded, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider, model, update_id, tokens_in, tokens_out,
                1 if estimated else 0,
                duration_ms,
                1 if succeeded else 0,
                utcnow().isoformat(),
            ),
        )
        return int(cur.lastrowid or 0)

    def usage_summary(self, *, days: int = 30) -> dict[str, Any]:
        """Aggregate stats for the last N days.

        Returns per-provider + per-model totals so the `/usage` page can
        render a small table without doing math in the template.
        """
        cutoff = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = cutoff.replace(day=max(1, cutoff.day - days))
        # `created_at` is ISO-8601 lexicographic — string compare is safe
        # in the same time zone.
        rows = self._conn.execute(
            """
            SELECT provider, model,
                   COUNT(*)              AS calls,
                   SUM(tokens_in)        AS tokens_in,
                   SUM(tokens_out)       AS tokens_out,
                   SUM(duration_ms)      AS duration_ms,
                   SUM(succeeded)        AS succeeded,
                   SUM(estimated)        AS estimated_count
              FROM llm_usage
             WHERE created_at >= ?
             GROUP BY provider, model
             ORDER BY tokens_in DESC
            """,
            (cutoff.isoformat(),),
        ).fetchall()
        per_combo = [dict(r) for r in rows]
        totals = {
            "calls": sum(r["calls"] for r in per_combo),
            "tokens_in": sum(r["tokens_in"] or 0 for r in per_combo),
            "tokens_out": sum(r["tokens_out"] or 0 for r in per_combo),
            "succeeded": sum(r["succeeded"] or 0 for r in per_combo),
        }
        return {
            "days": days,
            "since": cutoff.isoformat(),
            "totals": totals,
            "by_provider_model": per_combo,
        }

    def recent_llm_calls(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Most-recent N calls for the `/usage` page detail list."""
        rows = self._conn.execute(
            "SELECT * FROM llm_usage ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
