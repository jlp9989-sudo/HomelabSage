"""Engine — orchestrates scan → LLM analyze → persist → route to outputs."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import httpx

from .config import Config
from .db import Database
from .llm import LLMClient
from .models import AnalyzedUpdate, UpdateStatus
from .notes import NotesProvider
from .outputs import Output
from .outputs.notion import NotionOutput
from .outputs.telegram import TelegramOutput
from .plugins import Plugin
from .plugins.docker import DockerPlugin
from .plugins.homeassistant import HomeAssistantPlugin

log = logging.getLogger(__name__)


def build_plugins(cfg: Config) -> list[Plugin]:
    plugins: list[Plugin] = []
    if cfg.sources.docker.enabled:
        plugins.append(DockerPlugin(cfg.sources.docker))
    if cfg.sources.homeassistant.enabled:
        plugins.append(HomeAssistantPlugin(cfg.sources.homeassistant))
    return plugins


def build_outputs(cfg: Config) -> list[Output]:
    outputs: list[Output] = []
    if cfg.outputs.notion.enabled:
        outputs.append(NotionOutput(cfg.outputs.notion))
    if cfg.outputs.telegram.enabled:
        outputs.append(TelegramOutput(cfg.outputs.telegram))
    return outputs


class Engine:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.llm = LLMClient(cfg.llm)
        self.notes = NotesProvider(
            notes_dir=cfg.notes.notes_dir or None,
            extra_docs=cfg.notes.extra_docs,
            max_chars=cfg.notes.max_chars,
        )
        self.plugins = build_plugins(cfg)
        self.outputs = build_outputs(cfg)

    async def run_once(self) -> dict[str, int]:
        """Single full cycle. Returns counts (`scanned`, `new`, `analyzed`, `failed`)."""
        log.info("Run start — plugins=%s outputs=%s",
                 [p.id for p in self.plugins], [o.id for o in self.outputs])
        stats = {"scanned": 0, "new": 0, "analyzed": 0, "failed": 0}

        for plugin in self.plugins:
            try:
                items = await plugin.scan()
            except Exception as e:
                log.exception("plugin %s scan failed: %s", plugin.id, e)
                stats["failed"] += 1
                continue
            stats["scanned"] += len(items)
            for update in items:
                analyzed = AnalyzedUpdate(update=update)
                # Skip LLM call if we already analyzed this exact (subject, new_version)
                existing = self.db.get(analyzed.id)
                if existing and existing.analysis is not None:
                    continue
                stats["new"] += 1
                if self.llm.is_enabled():
                    try:
                        notes_ctx = self.notes.context_for(update.subject)
                        analyzed.analysis = await self.llm.analyze(update, notes=notes_ctx)
                        if analyzed.analysis:
                            analyzed.status = UpdateStatus.ANALYZED
                            analyzed.analyzed_at = datetime.utcnow()
                            stats["analyzed"] += 1
                    except Exception as e:
                        log.exception("LLM failed on %s: %s", update.subject, e)
                self.db.upsert(analyzed)
                for output in self.outputs:
                    try:
                        await output.send(analyzed)
                    except Exception as e:
                        log.exception("output %s failed: %s", output.id, e)

        await self._heartbeat_ok()
        log.info("Run end — %s", stats)
        return stats

    async def _heartbeat_ok(self) -> None:
        url = self.cfg.scheduler.heartbeat_url
        if not url:
            return
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.get(url)
        except httpx.HTTPError:
            pass  # best-effort; never break the run on a heartbeat failure

    def close(self) -> None:
        self.db.close()


# Convenience for one-shot CLI use
async def run_once(cfg: Config) -> dict[str, int]:
    db = Database(cfg.storage.database_path)
    engine = Engine(cfg, db)
    try:
        return await engine.run_once()
    finally:
        engine.close()


def run_blocking(cfg: Config) -> dict[str, int]:
    return asyncio.run(run_once(cfg))
