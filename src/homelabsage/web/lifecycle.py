"""FastAPI startup / shutdown wiring for the scheduler.

Kept in its own module because the scheduler instance is closure state and
the import (`apscheduler`) is heavyweight enough to want a single owner.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI

from ..config import Config
from ..digest import run_digest
from ..engine import Engine

log = logging.getLogger(__name__)


def register_lifecycle(app: FastAPI, cfg: Config, engine: Engine) -> None:
    """Register `startup` / `shutdown` handlers that own the scheduler."""
    scheduler: AsyncIOScheduler | None = None

    @app.on_event("startup")
    async def _start() -> None:
        nonlocal scheduler
        if cfg.scheduler.enabled:
            scheduler = AsyncIOScheduler(timezone=cfg.scheduler.timezone)
            scheduler.add_job(
                engine.run_once,
                CronTrigger.from_crontab(cfg.scheduler.cron, timezone=cfg.scheduler.timezone),
                id="run_once",
                misfire_grace_time=3600,
            )
            if cfg.digest.enabled:
                async def _digest_job() -> None:
                    # When the user opted into `parity_gate.skip_digest_too`,
                    # mirror the per-update gate behaviour for the digest:
                    # suppress this firing if parity is active. Otherwise the
                    # digest fires unconditionally — it's the backstop for
                    # missed real-time pings, so silencing it during parity
                    # leaves no signal at all.
                    if (
                        cfg.parity_gate.enabled
                        and cfg.parity_gate.skip_digest_too
                    ):
                        from ..parity import is_parity_running
                        state = is_parity_running(
                            mdstat_path=cfg.parity_gate.mdstat_path
                        )
                        if state.running:
                            log.info(
                                "Digest suppressed by parity gate: %s",
                                state.reason,
                            )
                            return
                    _, results, _ = await run_digest(
                        cfg, days=cfg.digest.lookback_days
                    )
                    log.info("Digest delivered: %s", results)

                scheduler.add_job(
                    _digest_job,
                    CronTrigger.from_crontab(
                        cfg.digest.cron, timezone=cfg.scheduler.timezone
                    ),
                    id="digest",
                    misfire_grace_time=3600,
                )
            scheduler.start()
            log.info(
                "Scheduler started: cron=%r tz=%s%s",
                cfg.scheduler.cron, cfg.scheduler.timezone,
                f" digest_cron={cfg.digest.cron!r}" if cfg.digest.enabled else "",
            )

    @app.on_event("shutdown")
    async def _stop() -> None:
        if scheduler:
            scheduler.shutdown(wait=False)
        engine.close()
