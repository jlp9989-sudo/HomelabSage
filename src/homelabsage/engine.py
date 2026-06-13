"""Engine — orchestrates scan → LLM analyze → persist → route to outputs."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from ._time import utcnow
from .backup_health import BackupHealthReport, probe_repo
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
from .outputs.msteams import MSTeamsOutput
from .outputs.notion import NotionOutput
from .outputs.ntfy import NtfyOutput
from .outputs.pushover import PushoverOutput
from .outputs.slack import SlackOutput
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

# A queued dispatch that never delivers (output permanently broken, item
# snoozed/quiet far longer than expected) would otherwise sit in the queue
# forever. Parity windows resolve in well under a day, so two weeks is a
# generous floor: anything older is genuinely stuck, not in-flight, and is
# purged so the queue can't grow without bound.
_PENDING_DISPATCH_TTL_DAYS = 14


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
    if cfg.outputs.slack.enabled:
        outputs.append(SlackOutput(cfg.outputs.slack))
    if cfg.outputs.pushover.enabled:
        outputs.append(PushoverOutput(cfg.outputs.pushover))
    if cfg.outputs.msteams.enabled:
        outputs.append(MSTeamsOutput(cfg.outputs.msteams))
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
        # Serialises scan cycles. APScheduler's max_instances=1 only
        # protects scheduler-vs-scheduler; the web POST /run path could
        # overlap with a cron scan, double-analyzing (and double-pushing)
        # the same updates and cross-contaminating `llm.last_call` between
        # rows. One lock, both entry points.
        self._run_lock = asyncio.Lock()

    async def run_once(self) -> dict[str, int]:
        """Single full cycle. Returns counts (`scanned`, `new`, `analyzed`,
        `failed`).

        Concurrent calls don't queue up — a second scan started while one
        is in flight would just re-scan the same state, so it reports
        `skipped_concurrent` and returns immediately.
        """
        if self._run_lock.locked():
            log.info("run_once skipped — another scan is already in flight")
            return {
                "scanned": 0, "new": 0, "analyzed": 0, "failed": 0,
                "skipped_concurrent": 1,
            }
        async with self._run_lock:
            return await self._run_cycle()

    async def _run_cycle(self) -> dict[str, int]:
        """The actual scan cycle — only ever entered under `_run_lock`.

        Split into thin sub-stages in v0.11.4 so each piece is independently
        testable + readable. The orchestration shape is intentionally linear:
        scan-gate → parity-gate → per-plugin loop → batch flush → heartbeat.
        """
        skipped = await self._maybe_skip_scan()
        if skipped is not None:
            return skipped

        log.info("Run start — plugins=%s outputs=%s",
                 [p.id for p in self.plugins], [o.id for o in self.outputs])
        stats = {"scanned": 0, "new": 0, "analyzed": 0, "failed": 0}

        push_gated = self._check_parity_gate()
        # Auto-flush the gated-window queue once the gate clears. Older
        # items first; failures stay queued and retry on the next scan.
        if not push_gated:
            await self._flush_pending_dispatches()

        # Batching state — populated as the loop runs, flushed at the end.
        batched: list[AnalyzedUpdate] = []
        batch_threshold = self._batch_threshold()

        # Scan-level cross-signal context (disk free space, backup freshness)
        # is the same for every update this cycle — compute it once and
        # thread it into each row rather than re-probing per update.
        scan_ctx = await self._scan_enrichments()

        for plugin in self.plugins:
            try:
                items = await plugin.scan()
            except Exception as e:
                log.exception("plugin %s scan failed: %s", plugin.id, e)
                stats["failed"] += 1
                continue
            stats["scanned"] += len(items)
            for update in items:
                analyzed = await self._analyze_single(update, stats, scan_ctx)
                if analyzed is None:
                    continue  # dedup: already analyzed in a prior run
                await self._dispatch_single(
                    analyzed, push_gated, batched, batch_threshold,
                )

        await self._finalise_batch(batched, batch_threshold, push_gated)
        await self._heartbeat_ok()
        log.info("Run end — %s", stats)
        return stats

    # ─── run_once sub-stages ─────────────────────────────────────────

    async def _maybe_skip_scan(self) -> dict[str, int] | None:
        """Pre-scan gates: scan-window + LLM-backend health.

        Returns the early-return stats dict when the scan should skip
        (counted as `skipped: 1` so monitoring can see we didn't accidentally
        sleep the process), or None to proceed.
        """
        # Scan-window: skip when the local time is inside the configured
        # window — no plugin polling, no LLM call, no upstream API hits.
        if self.cfg.scan_window.enabled:
            from .scan_window import is_scan_blocked
            blocked = is_scan_blocked(
                enabled=True,
                window_spec=self.cfg.scan_window.window,
                timezone_name=self.cfg.scan_window.timezone,
            )
            if blocked:
                log.info("Scan skipped: %s", blocked.reason)
                return {
                    "scanned": 0, "new": 0, "analyzed": 0, "failed": 0,
                    "skipped": 1,
                }

        # LLM-backend health: probe the endpoint before plugins do upstream
        # registry work that would die on the analyser call anyway.
        if self.cfg.llm_health_gate.enabled:
            from .config import get_active_llm_config
            from .llm_health import probe as probe_llm
            llm_cfg = get_active_llm_config(self.cfg)
            llm_status = await asyncio.to_thread(
                probe_llm,
                llm_cfg.endpoint,
                api_key=llm_cfg.api_key,
                timeout=self.cfg.llm_health_gate.timeout_seconds,
            )
            if not llm_status.ok:
                log.warning(
                    "Scan skipped: LLM backend unreachable (%s)",
                    llm_status.reason,
                )
                return {
                    "scanned": 0, "new": 0, "analyzed": 0, "failed": 0,
                    "skipped": 1,
                }
        return None

    def _check_parity_gate(self) -> bool:
        """True iff push outputs should be queued instead of fired.

        Persistent outputs (Notion) keep running so we don't lose state;
        the user just doesn't get a phone buzz mid-resync.
        """
        if not self.cfg.parity_gate.enabled:
            return False
        state = is_parity_running(mdstat_path=self.cfg.parity_gate.mdstat_path)
        if state.running:
            log.info("Push notifications gated by parity: %s", state.reason)
            return True
        return False

    def _batch_threshold(self) -> Severity | None:
        """The severity floor below which items are deferred to the
        end-of-scan rollup. None when batching is disabled."""
        batch_cfg = self.cfg.outputs.batching
        return Severity(batch_cfg.below_severity) if batch_cfg.enabled else None

    async def _scan_enrichments(self) -> dict:
        """Compute the cross-signal context shared by every update this scan.

        Some signals don't vary per update — the host's free disk space and
        the freshness of the backup repos are the same for every row in the
        scan. Probing them once here (instead of per update) keeps the
        per-row path cheap and the subprocess/stat cost bounded to once per
        cycle. Returns a dict with optional keys consumed by
        `_inject_cross_signals`:
          - `free_space`: `{free_bytes, path}` of the tightest configured
            filesystem (for will-it-fit).
          - `backup_health`: `{repos: [...]}` listing only stale/failed
            repos (healthy backups are omitted so they don't noise up rows).
        """
        enrich: dict = {}

        docker_cfg = self.cfg.sources.docker
        if docker_cfg.image_fit_check and self.cfg.disk_pressure.paths:
            from .disk_pressure import tightest_free
            free = tightest_free(self.cfg.disk_pressure.paths)
            if free is not None:
                enrich["free_space"] = {"free_bytes": free[0], "path": free[1]}

        bh = self.cfg.backup_health
        if bh.enabled and bh.inject_into_updates and bh.repos:
            report = await asyncio.to_thread(self._probe_backups)
            stale = [
                r.to_context() for r in report.results
                if not r.ok or r.severity in ("medium", "high", "critical")
            ]
            if stale:
                enrich["backup_health"] = {"repos": stale}

        return enrich

    def _probe_backups(self) -> BackupHealthReport:
        """Probe every configured backup repo (blocking subprocess work —
        always call via `asyncio.to_thread`). Best-effort: each `probe_repo`
        returns a failure verdict rather than raising."""
        results = [
            probe_repo(
                name=r.name, tool=r.tool, env=r.env or None,
                binary=r.binary or None,
                warn_after_days=r.warn_after_days,
                critical_after_days=r.critical_after_days,
                timeout=r.timeout_seconds,
            )
            for r in self.cfg.backup_health.repos
        ]
        return BackupHealthReport(results=results)

    def _inject_cross_signals(self, update, scan_ctx: dict):
        """Attach cross-signal verdicts to `update.context` before the LLM call.

        Each signal crosses data the analyzer would otherwise see only in
        pieces (or not at all) into one framed verdict the prompt has a rule
        for:
          - `pin_violation`: a user pin vs the new version (per-update).
          - `image_fit`: candidate image size vs free disk (per-update size,
            scan-level free space).
          - `backup_health`: snapshot freshness, so applying a breaking
            update on stale backups gets a warning (scan-level).

        The context dict is REPLACED, never mutated in place, so cached
        cross-run state on the Update isn't polluted.
        """
        extra: dict = {}

        if self.cfg.image_pins.enabled and self.cfg.image_pins.pins:
            from .image_pins import evaluate as evaluate_pin
            verdict = evaluate_pin(
                subject=update.subject,
                new_version=update.new_version,
                pins=self.cfg.image_pins.pins,
            )
            if verdict is not None:
                extra["pin_violation"] = verdict.to_context()

        free_space = scan_ctx.get("free_space")
        growth = (update.context or {}).get("image_size_growth")
        if free_space and isinstance(growth, dict) and growth.get("new_mib"):
            from .image_fit import evaluate as evaluate_fit
            fit = evaluate_fit(
                new_image_bytes=int(float(growth["new_mib"]) * 1024 * 1024),
                free_bytes=int(free_space["free_bytes"]),
                path=str(free_space["path"]),
            )
            if fit is not None:
                extra["image_fit"] = fit.to_context()

        if "backup_health" in scan_ctx:
            extra["backup_health"] = scan_ctx["backup_health"]

        if extra:
            update.context = {**(update.context or {}), **extra}
        return update

    async def _analyze_single(
        self, update, stats: dict[str, int], scan_ctx: dict,
    ) -> AnalyzedUpdate | None:
        """Process one Update through cross-signals → dedup → LLM → persist →
        auto-apply → curator hook. Returns the analyzed item, or None when
        the update is a duplicate we already analyzed in a prior run.
        """
        # Cross-signal verdicts (pin, will-it-fit, backup freshness) run
        # BEFORE the analyzer so the LLM sees them framed in `context` and
        # the prompt rules can act on them.
        update = self._inject_cross_signals(update, scan_ctx)

        analyzed = AnalyzedUpdate(update=update)
        existing = self.db.get(analyzed.id)
        # Skip LLM call when we already analyzed this exact (subject, new_version).
        if existing and existing.analysis is not None:
            return None
        # A row the user already ruled on must NOT be resurrected by a
        # re-emit. Without this, a row left with analysis=None (LLM was
        # down, or provider=disabled) that the user then DISMISSED came
        # back on the next scan: re-analyzed, upserted back to ANALYZED
        # (clobbering the user's status), and re-notified — on every scan.
        if existing and existing.status in (
            UpdateStatus.DISMISSED, UpdateStatus.APPLIED, UpdateStatus.FAILED,
        ):
            return None
        # Re-emitting an existing-but-unanalyzed item (previous LLM failed).
        # Carry the Notion page_id so the output PATCHes instead of POSTing.
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
                # Persist explainer + usage regardless of parse success.
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
        # Auto-apply runs AFTER analysis so severity + breaking_changes
        # inform the decision.
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
        return analyzed

    async def _dispatch_single(
        self,
        analyzed: AnalyzedUpdate,
        push_gated: bool,
        batched: list[AnalyzedUpdate],
        batch_threshold: Severity | None,
    ) -> None:
        """Route one analyzed update to every wired output, honoring the
        parity / snooze / quiet-hours / batching gates per channel.

        Persistent outputs (Notion) run through every path so the DB stays
        consistent. Push outputs are gated and may be queued for later.
        """
        # Snooze gate is per-update and trumps everything else — including
        # the batch rollup. A snoozed item appearing in the end-of-scan
        # rollup would defeat the user's explicit "later".
        snoozed_until = self._snooze_active_for(analyzed.id)

        # Batching short-circuit: append to the rollup; push outputs are
        # skipped in the loop below. Persistent outputs still run.
        deferred_to_batch = (
            not snoozed_until
            and batch_threshold is not None
            and below_threshold(analyzed, batch_threshold)
        )
        if deferred_to_batch:
            batched.append(analyzed)

        for output in self.outputs:
            if snoozed_until and output.is_push:
                # Don't queue — snooze is the user's explicit "later",
                # replaying on a future scan would ambush them.
                continue
            if push_gated and output.is_push:
                # Queue for later flush (auto-flush at the top of run_once
                # picks them up when the gate clears).
                self.db.queue_pending_dispatch(analyzed.id, output.id)
                continue
            if deferred_to_batch and output.is_push:
                # Will be sent via dispatch_batch at end-of-scan.
                continue
            # Quiet-hours is per-output (each push channel has its own window)
            # and only applies to push outputs — Notion always writes.
            if output.is_push and self._quiet_blocks(output, analyzed):
                self.db.queue_pending_dispatch(analyzed.id, output.id)
                continue
            try:
                ok = await output.send(analyzed)
            except Exception as e:
                log.exception("output %s failed: %s", output.id, e)
                ok = False
            # `send() is False` = transient delivery failure (per the
            # Output contract). Queue push items for the next scan's
            # flush; `None` from legacy test doubles counts as success.
            if ok is False and output.is_push:
                self.db.queue_pending_dispatch(analyzed.id, output.id)

    async def _finalise_batch(
        self,
        batched: list[AnalyzedUpdate],
        batch_threshold: Severity | None,
        push_gated: bool,
    ) -> None:
        """Send the low-severity rollup if we crossed the min-count
        threshold. Below it we drop the batched items — the whole point of
        batching is "don't ping for trivia"; flushing a 1-item batch defeats
        that. Also a no-op while the parity gate is active.
        """
        if batch_threshold is None or push_gated:
            return
        batch_cfg = self.cfg.outputs.batching
        if len(batched) < batch_cfg.min_count:
            return
        results = await dispatch_batch(self.cfg, batched)
        log.info("Batched %d updates → %s", len(batched), results)

    async def _flush_pending_dispatches(self) -> None:
        """Replay every queued push dispatch through the matching output.

        Each successful send DELETEs its queue row. Failures stay in the
        queue and retry on the next scan — same semantics as the original
        per-update loop, just resumed across runs.

        Items whose `analysis` is gone (very old row, manually purged) are
        skipped and their queue rows removed: stale entries shouldn't
        block the queue forever.

        As a final backstop, rows older than the TTL are purged outright
        before the replay: a permanently-broken output or an item snoozed
        far longer than any real parity window would otherwise keep its
        row indefinitely.
        """
        purged = self.db.purge_pending_dispatches_older_than(
            days=_PENDING_DISPATCH_TTL_DAYS
        )
        if purged:
            log.info(
                "Purged %d pending dispatch(es) older than %d days",
                purged, _PENDING_DISPATCH_TTL_DAYS,
            )
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
            # Honour snooze in the flush path too. Without this check
            # an update snoozed AFTER it was queued during a parity
            # window would still fire when the parity gate clears —
            # contradicting the contract that snooze suppresses pushes.
            # We keep the queue row so the flush retries naturally
            # after the snooze expires.
            if output.is_push and self._snooze_active_for(update_id):
                continue
            # Honour quiet hours too: an item queued at 23:30 must not
            # fire from the 00:00 scan's flush mid-window. Keep the row;
            # it replays on the first scan after the window ends.
            if output.is_push and self._quiet_blocks(output, item):
                continue
            try:
                ok = await output.send(item)
            except Exception as e:
                log.exception(
                    "pending dispatch %s → %s failed; will retry: %s",
                    update_id, output_id, e,
                )
                continue
            # Only a confirmed delivery clears the row. `send() is False`
            # means the push output reported a transient failure (HTTP
            # error) — before the Output contract carried this signal,
            # outputs swallowed their own errors and the row was deleted
            # even when Telegram was down, silently losing the message.
            if ok is not False:
                self.db.delete_pending_dispatch(update_id, output_id)

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

    def _snooze_active_for(self, update_id: str) -> str | None:
        """Return the snooze timestamp if still in the future, else None.

        Best-effort: corrupt timestamp logs at WARN and returns None so
        a malformed value never silences an update permanently.
        """
        if not hasattr(self.db, "get_snooze"):
            return None
        raw = self.db.get_snooze(update_id)
        if not raw:
            return None
        from ._time import parse_iso, utcnow
        try:
            until = parse_iso(raw)
        except ValueError:
            log.warning("snooze: unparseable timestamp for %s: %r",
                        update_id, raw)
            return None
        if until is None:
            return None
        if until.tzinfo is None:
            from datetime import UTC
            until = until.replace(tzinfo=UTC)
        if until <= utcnow():
            return None
        return raw

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
