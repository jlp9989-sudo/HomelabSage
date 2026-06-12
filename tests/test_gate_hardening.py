"""Gate-hardening regression tests (v0.12.2).

Four delivery-contract holes found by the 12-jun code review — each test
pins the guarantee that was leaking:

  1. A user-ruled row (DISMISSED/APPLIED/FAILED) left without analysis
     must not be resurrected, re-analyzed, or re-notified by a re-emit.
  2. `send()` reporting a transient failure (False) must keep / create the
     pending-dispatch row instead of silently losing the notification.
  3. The pending-dispatch flush must honour quiet hours — an item queued
     at 23:30 must not fire from the 00:00 scan mid-window.
  4. The batch rollup must honour snooze and each channel's min_severity.
  5. (Bonus) `run_once` is serialised — a concurrent call reports
     `skipped_concurrent` instead of double-scanning.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from homelabsage.config import Config
from homelabsage.db import Database
from homelabsage.engine import Engine
from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
    UpdateStatus,
)
from homelabsage.outputs import Output


class _RecordingPush(Output):
    """Push output that records sends and returns a scripted outcome."""

    id = "fake_push"
    is_push = True

    def __init__(self, *, ok: bool = True):
        self.ok = ok
        self.sent: list[str] = []

    async def send(self, item) -> bool:
        self.sent.append(item.id)
        return self.ok


class _FakePlugin:
    id = "fake_plugin"

    def __init__(self, updates: list[Update]):
        self._updates = updates

    async def scan(self):
        return self._updates


def _update(subject: str = "svc", new: str = "2.0") -> Update:
    return Update(
        source="fake_plugin", subject=subject,
        current_version="1.0", new_version=new,
    )


def _engine(tmp_path: Path) -> tuple[Engine, Database]:
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    cfg.llm.provider = "disabled"
    cfg.scheduler.enabled = False
    db = Database(cfg.storage.database_path)
    return Engine(cfg, db), db


# ─── 1. user-ruled rows don't resurrect ───────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ruled", [UpdateStatus.DISMISSED, UpdateStatus.APPLIED, UpdateStatus.FAILED],
)
async def test_user_ruled_unanalyzed_row_not_resurrected(tmp_path, ruled):
    """analysis=None + user-set status → re-emit must be a no-op. The old
    code re-created the row as ANALYZED (clobbering the user's decision)
    and re-notified — on EVERY scan when the LLM provider is disabled."""
    engine, db = _engine(tmp_path)
    push = _RecordingPush()
    engine.plugins = [_FakePlugin([_update()])]  # type: ignore[list-item]
    engine.outputs = [push]  # type: ignore[list-item]
    try:
        row = AnalyzedUpdate(update=_update(), status=ruled)
        assert row.analysis is None
        db.upsert(row)

        stats = await engine.run_once()

        kept = db.get(row.id)
        assert kept is not None and kept.status is ruled
        assert push.sent == []
        assert stats["new"] == 0
    finally:
        engine.close()


@pytest.mark.asyncio
async def test_plain_unanalyzed_row_still_reanalyzed(tmp_path):
    """Counter-case: a NEW row whose analysis failed last scan (LLM down)
    must still re-emit — that recovery path is intentional."""
    engine, db = _engine(tmp_path)
    engine.plugins = [_FakePlugin([_update()])]  # type: ignore[list-item]
    engine.outputs = []
    try:
        db.upsert(AnalyzedUpdate(update=_update(), status=UpdateStatus.NEW))
        stats = await engine.run_once()
        assert stats["new"] == 1
    finally:
        engine.close()


# ─── 2. transient send failure keeps the message ──────────────────


@pytest.mark.asyncio
async def test_failed_send_queues_for_retry_and_flush_redelivers(tmp_path):
    """send()→False queues the dispatch; once the output recovers, the
    next scan's flush delivers it and clears the row."""
    engine, db = _engine(tmp_path)
    push = _RecordingPush(ok=False)   # Telegram is down
    engine.outputs = [push]  # type: ignore[list-item]
    try:
        item = AnalyzedUpdate(
            update=_update(),
            analysis=Analysis(severity=Severity.HIGH, summary="x"),
            status=UpdateStatus.ANALYZED,
        )
        db.upsert(item)
        await engine._dispatch_single(item, False, [], None)
        queued = db.list_pending_dispatches()
        assert [(r["update_id"], r["output_id"]) for r in queued] == [
            (item.id, "fake_push"),
        ]

        # Output recovers; next scan's flush replays and clears the queue.
        push.ok = True
        engine.plugins = [_FakePlugin([])]  # type: ignore[list-item]
        await engine.run_once()
        assert db.list_pending_dispatches() == []
        # First (failed) attempt + successful flush replay.
        assert push.sent.count(item.id) == 2
    finally:
        engine.close()


@pytest.mark.asyncio
async def test_flush_keeps_row_while_send_still_failing(tmp_path):
    engine, db = _engine(tmp_path)
    push = _RecordingPush(ok=False)
    engine.plugins = [_FakePlugin([])]  # type: ignore[list-item]
    engine.outputs = [push]  # type: ignore[list-item]
    try:
        item = AnalyzedUpdate(
            update=_update(),
            analysis=Analysis(severity=Severity.HIGH, summary="x"),
            status=UpdateStatus.ANALYZED,
        )
        db.upsert(item)
        db.queue_pending_dispatch(item.id, "fake_push")

        await engine.run_once()
        assert len(db.list_pending_dispatches()) == 1   # still queued
    finally:
        engine.close()


# ─── 3. flush honours quiet hours ─────────────────────────────────


@pytest.mark.asyncio
async def test_flush_skips_push_during_quiet_hours(tmp_path, monkeypatch):
    """A queued dispatch must not fire mid-quiet-window; the row stays
    queued for the first scan after the window ends."""
    engine, db = _engine(tmp_path)
    push = _RecordingPush()
    engine.plugins = [_FakePlugin([])]  # type: ignore[list-item]
    engine.outputs = [push]  # type: ignore[list-item]
    try:
        item = AnalyzedUpdate(
            update=_update(),
            analysis=Analysis(severity=Severity.MEDIUM, summary="x"),
            status=UpdateStatus.ANALYZED,
        )
        db.upsert(item)
        db.queue_pending_dispatch(item.id, "fake_push")

        monkeypatch.setattr(engine, "_quiet_blocks", lambda output, it: True)
        await engine.run_once()
        assert push.sent == []
        assert len(db.list_pending_dispatches()) == 1

        # Window over → flush delivers and clears.
        monkeypatch.setattr(engine, "_quiet_blocks", lambda output, it: False)
        await engine.run_once()
        assert push.sent == [item.id]
        assert db.list_pending_dispatches() == []
    finally:
        engine.close()


# ─── 4. batch rollup honours snooze + per-channel min_severity ────


@pytest.mark.asyncio
async def test_snoozed_item_not_added_to_batch(tmp_path):
    engine, db = _engine(tmp_path)
    engine.outputs = []
    try:
        item = AnalyzedUpdate(
            update=_update(),
            analysis=Analysis(severity=Severity.INFO, summary="x"),
            status=UpdateStatus.ANALYZED,
        )
        db.upsert(item)
        db.set_snooze(item.id, "2099-01-01T00:00:00+00:00")

        batched: list[AnalyzedUpdate] = []
        await engine._dispatch_single(item, False, batched, Severity.HIGH)
        assert batched == []   # old code appended BEFORE the snooze check
    finally:
        engine.close()


def test_batch_channel_items_respect_min_severity():
    from homelabsage.config import TelegramOutputConfig
    from homelabsage.outputs.batch import _channel_items

    lo = AnalyzedUpdate(
        update=_update("a"),
        analysis=Analysis(severity=Severity.INFO, summary="x"),
    )
    mid = AnalyzedUpdate(
        update=_update("b"),
        analysis=Analysis(severity=Severity.MEDIUM, summary="x"),
    )
    sub = TelegramOutputConfig(min_severity="medium")
    assert _channel_items(sub, [lo, mid]) == [mid]


def test_batch_channel_quiet_window():
    from homelabsage.config import TelegramOutputConfig
    from homelabsage.outputs.batch import _channel_quiet

    assert _channel_quiet(TelegramOutputConfig()) is False         # no window
    always_quiet = TelegramOutputConfig(quiet_hours="00:00-23:59")
    assert _channel_quiet(always_quiet) is True


# ─── 5. run_once is serialised ────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_run_once_reports_skipped(tmp_path):
    engine, db = _engine(tmp_path)
    engine.plugins = []
    engine.outputs = []
    try:
        async with engine._run_lock:   # simulate a scan in flight
            stats = await engine.run_once()
        assert stats.get("skipped_concurrent") == 1
        # And once the lock is free, scans run normally again.
        stats = await engine.run_once()
        assert "skipped_concurrent" not in stats
    finally:
        engine.close()
