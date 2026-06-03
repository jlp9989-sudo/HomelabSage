"""Per-container log-anomaly rolling baseline + flagged spikes.

`log_samples` stores one row per (container, scan) with the recent
error/warn counts. `log_anomalies` stores the events where the latest
sample crossed `sigma_threshold` standard deviations above the baseline.
The auditor reads the spike table; the analyzer can use a single
`Update.context.log_anomaly` derived from the latest sample to mention
"this container is noisier than usual" when relevant.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import timedelta
from typing import Any

from .._time import utcnow


class LogAnomalyMixin:
    """Methods touching `log_samples` + `log_anomalies`."""

    _conn: sqlite3.Connection

    # ─── samples ──────────────────────────────────────────────────────

    def record_log_sample(
        self,
        *,
        container_name: str,
        error_count: int,
        warn_count: int,
        window_minutes: int,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO log_samples
                (container_name, error_count, warn_count, window_minutes, sampled_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                container_name, error_count, warn_count, window_minutes,
                utcnow().isoformat(),
            ),
        )
        return int(cur.lastrowid or 0)

    def baseline_for(
        self, *, container_name: str, lookback_days: int = 14,
    ) -> tuple[float, float, int]:
        """Mean + stddev of `error_count / window_minutes` over the lookback.

        Returns (mean_per_min, std_per_min, n_samples). When n_samples == 0
        the caller should skip alerting.
        """
        cutoff = utcnow() - timedelta(days=lookback_days)
        rows = self._conn.execute(
            """
            SELECT error_count, window_minutes FROM log_samples
             WHERE container_name = ? AND sampled_at >= ?
            """,
            (container_name, cutoff.isoformat()),
        ).fetchall()
        rates = [
            (r["error_count"] / r["window_minutes"]) if r["window_minutes"] else 0.0
            for r in rows
        ]
        n = len(rates)
        if n == 0:
            return 0.0, 0.0, 0
        mean = sum(rates) / n
        # Population stddev — we have the full sample.
        variance = sum((r - mean) ** 2 for r in rates) / n
        return mean, math.sqrt(variance), n

    # ─── anomalies ────────────────────────────────────────────────────

    def record_log_anomaly(
        self,
        *,
        container_name: str,
        error_rate: float,
        baseline_mean: float,
        baseline_std: float,
        sigma: float,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO log_anomalies
                (container_name, error_rate, baseline_mean, baseline_std,
                 sigma, detected_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                container_name, error_rate, baseline_mean, baseline_std,
                sigma, utcnow().isoformat(),
            ),
        )
        return int(cur.lastrowid or 0)

    def list_recent_log_anomalies(
        self, *, limit: int = 200, days: int = 7,
    ) -> list[dict[str, Any]]:
        cutoff = utcnow() - timedelta(days=days)
        rows = self._conn.execute(
            """
            SELECT * FROM log_anomalies
             WHERE detected_at >= ?
             ORDER BY id DESC LIMIT ?
            """,
            (cutoff.isoformat(), limit),
        ).fetchall()
        return [dict(r) for r in rows]
