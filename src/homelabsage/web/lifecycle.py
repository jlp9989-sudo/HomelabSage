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
            scheduler.start()
            log.info(
                "Scheduler started: cron=%r tz=%s",
                cfg.scheduler.cron, cfg.scheduler.timezone,
            )

    @app.on_event("shutdown")
    async def _stop() -> None:
        if scheduler:
            scheduler.shutdown(wait=False)
        engine.close()
