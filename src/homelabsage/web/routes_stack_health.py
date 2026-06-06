"""`/api/stack-health` — one-shot aggregator endpoint.

Pulls every health signal HomelabSage tracks into a single JSON
response that dashboards (Homepage, Homarr, custom widgets) can scrape:

  - The proactive auditor's findings summary (counts by severity).
  - Pending dispatches (notifications waiting on parity).
  - Parity state.
  - Backup-staleness verdicts (when enabled).
  - Recent heartbeat success/fail counts.
  - Update counts by status (new / analyzed / applied / dismissed).
  - Recent health-check failures.

Schema is stable: every key is always present. Empty arrays / zero
counts replace missing signals so widget templates don't need
`coalesce()` logic.

Read-only, auth-bypassed (mirrors `/widget/*` for the same reason —
dashboards scrape without bearer credentials).
"""

from __future__ import annotations

from fastapi import FastAPI

from .. import __version__
from ..audit import build_report
from ..config import Config
from ..db import Database
from ..models import UpdateStatus
from ..parity import is_parity_running


def register_stack_health_routes(app: FastAPI, cfg: Config, db: Database) -> None:
    @app.get("/api/doctor")
    async def api_doctor(skip_llm: bool = False) -> dict:
        """JSON mirror of `homelabsage doctor`.

        Same probes (LLM / TLS / DNS / disk / compose / audit) in a
        structured shape suitable for HA / Homepage widgets / Kuma.
        Read-only, auth-bypassed (matches `/api/stack-health`).
        Set `skip_llm=1` for offline runs.
        """
        import asyncio

        from ..doctor import build_report as build_doctor_report
        # Doctor probes are sync + can do network I/O; offload.
        return await asyncio.to_thread(
            build_doctor_report, cfg, db, skip_llm=skip_llm,
        )

    @app.get("/api/stack-health")
    async def stack_health() -> dict:
        report = build_report(cfg, db)
        counts = {s.value: 0 for s in UpdateStatus}
        for it in db.list(limit=500):
            counts[it.status.value] = counts.get(it.status.value, 0) + 1

        parity_active = False
        parity_reason = ""
        if cfg.parity_gate.enabled:
            state = is_parity_running(mdstat_path=cfg.parity_gate.mdstat_path)
            parity_active, parity_reason = state.running, state.reason

        heartbeat = db.heartbeat_summary(hours=24)

        backup_results: list[dict] = []
        if cfg.backup_health.enabled and cfg.backup_health.repos:
            from ..backup_health import probe_repo
            for repo in cfg.backup_health.repos:
                h = probe_repo(
                    name=repo.name, tool=repo.tool, env=repo.env,
                    binary=(repo.binary or None),
                    warn_after_days=repo.warn_after_days,
                    critical_after_days=repo.critical_after_days,
                    timeout=float(repo.timeout_seconds),
                )
                backup_results.append(h.to_context())

        recent_health_check_fails: list[dict] = [
            row for row in db.list_recent_health_checks(limit=50)
            if not row.get("ok")
        ]

        return {
            "version": __version__,
            "audit": {
                "healthy": report.healthy,
                "counts_by_severity": report.counts_by_severity,
                "counts_by_category": report.counts_by_category,
                "finding_count": len(report.findings),
            },
            "updates": {
                "counts_by_status": counts,
            },
            "parity": {
                "enabled": cfg.parity_gate.enabled,
                "active": parity_active,
                "reason": parity_reason,
            },
            "pending_dispatches": {
                "count": len(db.list_pending_dispatches()),
            },
            "heartbeat_24h": heartbeat,
            "backup_health": backup_results,
            "post_update_failures": recent_health_check_fails,
        }
