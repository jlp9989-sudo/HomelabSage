"""Engine — orchestrates scan → LLM analyze → persist → route to outputs."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

import httpx

from .config import Config
from .curator.incremental import append_update_to_note
from .db import Database
from .llm import LLMClient
from .models import AnalyzedUpdate, UpdateStatus
from .notes import NotesProvider
from .outputs import Output
from .outputs.notion import NotionOutput
from .outputs.telegram import TelegramOutput
from .plugins import Plugin
from .plugins.docker import DockerPlugin
from .plugins.fedora import FedoraPlugin
from .plugins.homeassistant import HomeAssistantPlugin

log = logging.getLogger(__name__)


def build_plugins(cfg: Config) -> list[Plugin]:
    plugins: list[Plugin] = []
    if cfg.sources.docker.enabled:
        plugins.append(DockerPlugin(cfg.sources.docker))
    if cfg.sources.homeassistant.enabled:
        plugins.append(HomeAssistantPlugin(cfg.sources.homeassistant))
    if cfg.sources.fedora.enabled:
        plugins.append(FedoraPlugin(cfg.sources.fedora))
    return plugins


def build_outputs(cfg: Config, db: Database) -> list[Output]:
    outputs: list[Output] = []
    if cfg.outputs.notion.enabled:
        outputs.append(NotionOutput(cfg.outputs.notion, db))
    if cfg.outputs.telegram.enabled:
        outputs.append(TelegramOutput(cfg.outputs.telegram))
    return outputs


class Engine:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        *,
        cfg_path: Path | None = None,
    ):
        self.cfg = cfg
        self.db = db
        self._cfg_path = cfg_path

        # Resolve the active LLM profile on every call so a UI-driven switch
        # of `llm_active` (or any other overlay edit) takes effect on the
        # NEXT LLM call without restarting the process. When `cfg_path` is
        # None (tests, programmatic callers), we resolve against the
        # in-memory cfg — still correct, just not hot-reloaded.
        from .config import get_active_llm_config, load_config

        def _resolve_llm():
            if cfg_path is not None:
                try:
                    return get_active_llm_config(load_config(cfg_path))
                except Exception:
                    pass  # fall through to the startup cfg on a bad read
            return get_active_llm_config(self.cfg)

        self.llm = LLMClient(_resolve_llm)
        self.notes = NotesProvider(
            notes_dir=cfg.notes.notes_dir or None,
            extra_docs=cfg.notes.extra_docs,
            max_chars=cfg.notes.max_chars,
        )
        self.plugins = build_plugins(cfg)
        self.outputs = build_outputs(cfg, db)

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
                # Re-emitting an existing-but-unanalyzed item (e.g. previous
                # LLM failed). Carry forward the Notion page_id so the output
                # PATCHes the existing row instead of creating a duplicate.
                if existing and existing.notion_page_id:
                    analyzed.notion_page_id = existing.notion_page_id
                stats["new"] += 1
                if self.llm.is_enabled():
                    try:
                        kw = update.context.get("_note_keywords") or []
                        notes_ctx = self.notes.context_for(update.subject, keywords=kw)
                        analyzed.analysis = await self.llm.analyze(update, notes=notes_ctx)
                        if analyzed.analysis:
                            analyzed.status = UpdateStatus.ANALYZED
                            analyzed.analyzed_at = datetime.utcnow()
                            stats["analyzed"] += 1
                    except Exception as e:
                        log.exception("LLM failed on %s: %s", update.subject, e)
                self.db.upsert(analyzed)
                self._incremental_hook(analyzed)
                for output in self.outputs:
                    try:
                        await output.send(analyzed)
                    except Exception as e:
                        log.exception("output %s failed: %s", output.id, e)

        await self._heartbeat_ok()
        log.info("Run end — %s", stats)
        return stats

    def _incremental_hook(self, analyzed: AnalyzedUpdate) -> None:
        """Pin a one-line summary of a risky update to the curator's note.

        Disabled when no notes_dir is configured, when the user has
        disabled the feature, or when the analyzed update isn't risky.
        Failures are swallowed: the note hook is a nice-to-have, never
        a reason to break the scan loop.
        """
        if not self.cfg.curator.append_analysis_log:
            return
        notes_dir = self.cfg.curator.output_dir or self.cfg.notes.notes_dir
        if not notes_dir:
            return
        try:
            path = append_update_to_note(
                notes_dir,
                analyzed,
                max_lines=self.cfg.curator.analysis_log_max_lines,
            )
        except Exception:
            log.exception("incremental hook failed on %s", analyzed.update.subject)
            return
        if path is not None:
            log.info("incremental: appended %s → %s", analyzed.id, path)

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
async def run_once(cfg: Config, *, cfg_path: Path | None = None) -> dict[str, int]:
    db = Database(cfg.storage.database_path)
    engine = Engine(cfg, db, cfg_path=cfg_path)
    try:
        return await engine.run_once()
    finally:
        engine.close()


def run_blocking(cfg: Config, *, cfg_path: Path | None = None) -> dict[str, int]:
    return asyncio.run(run_once(cfg, cfg_path=cfg_path))
