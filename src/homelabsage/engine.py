"""Engine — orchestrates scan → LLM analyze → persist → route to outputs."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from ._time import utcnow
from .config import Config
from .curator.incremental import append_update_to_note
from .db import Database
from .llm import LLMClient
from .models import AnalyzedUpdate, Severity, UpdateStatus
from .notes import NotesProvider
from .outputs import Output
from .outputs.apprise import AppriseOutput
from .outputs.batch import below_threshold, dispatch_batch
from .outputs.discord import DiscordOutput
from .outputs.gotify import GotifyOutput
from .outputs.notion import NotionOutput
from .outputs.ntfy import NtfyOutput
from .outputs.smtp import SMTPOutput
from .outputs.telegram import TelegramOutput
from .outputs.webhook import WebhookOutput
from .parity import is_parity_running
from .plugins import Plugin
from .plugins.docker import DockerPlugin
from .plugins.fedora import FedoraPlugin
from .plugins.github_watched import WatchedReposPlugin
from .plugins.homeassistant import HomeAssistantPlugin

log = logging.getLogger(__name__)


def build_plugins(cfg: Config, db: Database | None = None) -> list[Plugin]:
    plugins: list[Plugin] = []
    if cfg.sources.docker.enabled:
        plugins.append(DockerPlugin(cfg.sources.docker))
    if cfg.sources.homeassistant.enabled:
        plugins.append(HomeAssistantPlugin(cfg.sources.homeassistant))
    if cfg.sources.fedora.enabled:
        plugins.append(FedoraPlugin(cfg.sources.fedora))
    if cfg.sources.github_watched.enabled and db is not None:
        plugins.append(WatchedReposPlugin(db))
    return plugins


def build_outputs(cfg: Config, db: Database) -> list[Output]:
    outputs: list[Output] = []
    if cfg.outputs.notion.enabled:
        outputs.append(NotionOutput(cfg.outputs.notion, db))
    if cfg.outputs.telegram.enabled:
        outputs.append(TelegramOutput(cfg.outputs.telegram))
    if cfg.outputs.discord.enabled:
        outputs.append(DiscordOutput(cfg.outputs.discord))
    if cfg.outputs.ntfy.enabled:
        outputs.append(NtfyOutput(cfg.outputs.ntfy))
    if cfg.outputs.gotify.enabled:
        outputs.append(GotifyOutput(cfg.outputs.gotify))
    if cfg.outputs.apprise.enabled:
        outputs.append(AppriseOutput(cfg.outputs.apprise))
    if cfg.outputs.smtp.enabled:
        outputs.append(SMTPOutput(cfg.outputs.smtp))
    if cfg.outputs.webhook.enabled:
        outputs.append(WebhookOutput(cfg.outputs.webhook))
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
        self.plugins = build_plugins(cfg, db)
        self.outputs = build_outputs(cfg, db)

    async def run_once(self) -> dict[str, int]:
        """Single full cycle. Returns counts (`scanned`, `new`, `analyzed`, `failed`)."""
        log.info("Run start — plugins=%s outputs=%s",
                 [p.id for p in self.plugins], [o.id for o in self.outputs])
        stats = {"scanned": 0, "new": 0, "analyzed": 0, "failed": 0}

        # Probe parity once per run; outputs marked `is_push = True` are
        # skipped while it's active. Persistent outputs (Notion) keep
        # running so we don't lose state — the user just won't get a phone
        # buzz mid-resync. When the gate clears, we replay every push that
        # was skipped from the `pending_dispatches` queue (auto-flush).
        push_gated = False
        if self.cfg.parity_gate.enabled:
            state = is_parity_running(mdstat_path=self.cfg.parity_gate.mdstat_path)
            if state.running:
                push_gated = True
                log.info("Push notifications gated by parity: %s", state.reason)

        # Auto-flush: when the gate is NOT active, drain the queue of pushes
        # that piled up during the last gated window. We dispatch in
        # `queued_at` order so older items hit first, and DELETE the row on
        # successful delivery so retries are bounded by output errors only.
        if not push_gated:
            await self._flush_pending_dispatches()

        # Items collected for low-severity batching. Dispatched once at the
        # end of the scan as a single rollup per push channel rather than
        # one ping per item. Only populated when `outputs.batching.enabled`.
        batched: list[AnalyzedUpdate] = []
        batch_cfg = self.cfg.outputs.batching
        batch_threshold = Severity(batch_cfg.below_severity) if batch_cfg.enabled else None

        for plugin in self.plugins:
            try:
                items = await plugin.scan()
            except Exception as e:
                log.exception("plugin %s scan failed: %s", plugin.id, e)
                stats["failed"] += 1
                continue
            stats["scanned"] += len(items)
            for update in items:
                # Image-pin enforcement runs BEFORE the analyzer so the LLM
                # already sees the verdict in `context.pin_violation` and
                # frames its recommendation against it. Empty pins → no-op.
                # The Update's context dict is REPLACED (not mutated in
                # place) so plugins that cache context across runs aren't
                # polluted by this scan's pin verdict.
                if self.cfg.image_pins.enabled and self.cfg.image_pins.pins:
                    from .image_pins import evaluate as evaluate_pin
                    verdict = evaluate_pin(
                        subject=update.subject,
                        new_version=update.new_version,
                        pins=self.cfg.image_pins.pins,
                    )
                    if verdict is not None:
                        update.context = {
                            **(update.context or {}),
                            "pin_violation": verdict.to_context(),
                        }
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
                            analyzed.analyzed_at = utcnow()
                            stats["analyzed"] += 1
                        # Persist explainer (prompt + raw response + notes
                        # used) AND a usage row regardless of parse
                        # success. last_call is None only when analyze()
                        # early-returned because the LLM is disabled.
                        if self.llm.last_call is not None:
                            call = self.llm.last_call
                            cfg_now = self.llm.cfg
                            if call.succeeded:
                                self.db.upsert_explainer(
                                    analyzed.id,
                                    prompt=call.prompt,
                                    raw_response=call.raw_response,
                                    notes_used=notes_ctx or None,
                                    provider=cfg_now.provider,
                                    model=cfg_now.model,
                                )
                            self.db.record_llm_call(
                                provider=cfg_now.provider,
                                model=cfg_now.model,
                                update_id=analyzed.id,
                                tokens_in=call.tokens_in,
                                tokens_out=call.tokens_out,
                                estimated=call.tokens_estimated,
                                duration_ms=call.duration_ms,
                                succeeded=call.succeeded,
                            )
                    except Exception as e:
                        log.exception("LLM failed on %s: %s", update.subject, e)
                self.db.upsert(analyzed)
                # Auto-apply whitelist — runs AFTER analysis + persistence
                # so the LLM's `breaking_changes` + severity inform the
                # decision. Records a fresh status to the DB if it fires.
                if self.cfg.auto_apply.enabled and analyzed.analysis is not None:
                    from .auto_apply import should_auto_apply
                    decision = should_auto_apply(
                        analyzed,
                        allowlist=self.cfg.auto_apply.allowlist,
                        max_severity=self.cfg.auto_apply.max_severity,
                    )
                    if decision.should_apply:
                        log.info(
                            "auto_apply: %s → APPLIED (%s)",
                            analyzed.id, decision.reason,
                        )
                        self.db.set_status(analyzed.id, UpdateStatus.APPLIED)
                        analyzed.status = UpdateStatus.APPLIED
                        stats["auto_applied"] = stats.get("auto_applied", 0) + 1
                self._incremental_hook(analyzed)
                # Batching short-circuit: when the item is below the rollup
                # threshold AND we have a meaningful analysis, defer push
                # outputs to a single rollup at end-of-scan. Notion (and
                # other persistent outputs) still run per-item.
                deferred_to_batch = (
                    batch_threshold is not None
                    and below_threshold(analyzed, batch_threshold)
                )
                if deferred_to_batch:
                    batched.append(analyzed)
                for output in self.outputs:
                    if push_gated and output.is_push:
                        # Queue for later flush rather than relying on the
                        # item being re-detected — once analyzed, an Update
                        # is dedupped by `(source, subject, new_version)`
                        # and the per-item loop skips it entirely. The
                        # auto-flush at the top of run_once dispatches when
                        # the gate clears.
                        self.db.queue_pending_dispatch(analyzed.id, output.id)
                        continue
                    if deferred_to_batch and output.is_push:
                        # Sent later via dispatch_batch.
                        continue
                    # Quiet-hours gate composes with the parity gate. It is
                    # per-output (each push channel has its own window) and
                    # only applies to push outputs — Notion always writes.
                    if output.is_push and self._quiet_blocks(output, analyzed):
                        self.db.queue_pending_dispatch(analyzed.id, output.id)
                        continue
                    try:
                        await output.send(analyzed)
                    except Exception as e:
                        log.exception("output %s failed: %s", output.id, e)

        # End-of-scan: flush the low-severity batch if we crossed the
        # minimum count. Below min_count we DROP the batched items — the
        # whole point of batching is "don't ping the user for trivia";
        # forwarding the first message of a 1-item batch defeats that.
        if (
            batch_threshold is not None
            and not push_gated
            and len(batched) >= batch_cfg.min_count
        ):
            results = await dispatch_batch(self.cfg, batched)
            log.info("Batched %d updates → %s", len(batched), results)

        await self._heartbeat_ok()
        log.info("Run end — %s", stats)
        return stats

    async def _flush_pending_dispatches(self) -> None:
        """Replay every queued push dispatch through the matching output.

        Each successful send DELETEs its queue row. Failures stay in the
        queue and retry on the next scan — same semantics as the original
        per-update loop, just resumed across runs.

        Items whose `analysis` is gone (very old row, manually purged) are
        skipped and their queue rows removed: stale entries shouldn't
        block the queue forever.
        """
        queue = self.db.list_pending_dispatches()
        if not queue:
            return
        outputs_by_id = {o.id: o for o in self.outputs}
        for row in queue:
            update_id = row["update_id"]
            output_id = row["output_id"]
            output = outputs_by_id.get(output_id)
            if output is None:
                # The output is no longer enabled — clearing the row is the
                # safe call; without it the queue grows forever.
                self.db.delete_pending_dispatch(update_id, output_id)
                continue
            item = self.db.get(update_id)
            if item is None or item.analysis is None:
                self.db.delete_pending_dispatch(update_id, output_id)
                continue
            try:
                await output.send(item)
                self.db.delete_pending_dispatch(update_id, output_id)
            except Exception as e:
                log.exception(
                    "pending dispatch %s → %s failed; will retry: %s",
                    update_id, output_id, e,
                )

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

    def _quiet_blocks(self, output, analyzed) -> bool:
        """True iff this push output's quiet hours apply right now.

        Resolves the per-output config from `cfg.outputs.<id>`. Missing
        config (placeholder output, future-output) returns False so the
        gate is never accidentally active. `bypass_severity` lets
        critical messages escape — even at 3 AM the user wants to know
        their array is on fire.
        """
        from .quiet_hours import is_quiet_now, should_bypass
        outputs_cfg = self.cfg.outputs
        cfg = getattr(outputs_cfg, output.id, None)
        if cfg is None:
            return False
        spec = getattr(cfg, "quiet_hours", "")
        if not spec:
            return False
        if should_bypass(
            analyzed.analysis.severity if analyzed.analysis else None,
            getattr(cfg, "quiet_hours_bypass_severity", "critical"),
        ):
            return False
        state = is_quiet_now(
            window_spec=spec,
            timezone_name=getattr(cfg, "quiet_hours_timezone", "UTC"),
        )
        if state.quiet:
            log.info(
                "Quiet hours skip: %s → %s (%s)",
                analyzed.id, output.id, state.reason,
            )
        return state.quiet

    async def _heartbeat_ok(self) -> None:
        url = self.cfg.scheduler.heartbeat_url
        if not url:
            return
        import time as _time_mod
        started = _time_mod.monotonic()
        ok = False
        status_code: int | None = None
        err: str | None = None
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(url)
                status_code = r.status_code
                ok = 200 <= r.status_code < 300
                if not ok:
                    err = f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            err = str(e)[:200]
        duration_ms = int((_time_mod.monotonic() - started) * 1000)
        # Persist regardless of outcome; the failure path is the
        # interesting one for debugging a flaky Uptime Kuma URL.
        try:
            self.db.record_heartbeat(
                url=url, ok=ok, status_code=status_code,
                error=err, duration_ms=duration_ms,
            )
        except Exception:
            log.debug("heartbeat persistence failed (continuing)")

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
