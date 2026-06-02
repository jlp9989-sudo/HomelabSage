"""Tests for the weekly digest module.

Exercises collect_stats → render_markdown → deliver as separate units, plus
one integration test that runs `run_digest_blocking` against a real on-disk
SQLite with a handful of fixture rows. The dispatchers are tested via
monkeypatched httpx.AsyncClient (same pattern as the per-update outputs).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from homelabsage.config import (
    Config,
    DigestConfig,
    DiscordOutputConfig,
    GotifyOutputConfig,
    NtfyOutputConfig,
    OutputsConfig,
    TelegramOutputConfig,
)
from homelabsage.db import Database
from homelabsage.digest import (
    DigestStats,
    collect_stats,
    deliver,
    render_markdown,
    run_digest,
    write_to_notes,
)
from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
    UpdateStatus,
)


def _seed_db(db: Database, now: datetime) -> None:
    """Populate a DB with one of each severity, one orphan, one abandoned."""
    rows = [
        ("docker", "openclaw", Severity.CRITICAL, now - timedelta(days=1)),
        ("docker", "tintes",   Severity.HIGH,     now - timedelta(days=2)),
        ("docker", "mealie",   Severity.MEDIUM,   now - timedelta(days=3)),
        ("docker", "kavita",   Severity.INFO,     now - timedelta(days=4)),
        # Older than the 7-day cutoff — must be excluded.
        ("docker", "ancient",  Severity.HIGH,     now - timedelta(days=30)),
    ]
    for source, subject, sev, detected_at in rows:
        item = AnalyzedUpdate(
            update=Update(
                source=source,
                subject=subject,
                current_version="1.0.0",
                new_version="2.0.0",
                release_url=f"https://example.com/r/{subject}",
                release_notes="",
                context={},
            ),
            analysis=Analysis(
                severity=sev,
                summary=f"{subject} summary",
                recommended_action=f"upgrade {subject}",
            ),
            status=UpdateStatus.ANALYZED,
            detected_at=detected_at,
        )
        db.upsert(item)
    # Orphan item: same shape but with context.orphan_since_days set.
    orphan = AnalyzedUpdate(
        update=Update(
            source="docker",
            subject="forgotten",
            current_version="0.9",
            new_version="1.0",
            release_notes="",
            context={"orphan_since_days": 90},
        ),
        analysis=Analysis(severity=Severity.MEDIUM, summary="forgotten container"),
        status=UpdateStatus.ANALYZED,
        detected_at=now - timedelta(hours=12),
    )
    db.upsert(orphan)
    # Abandoned upstream
    abandoned = AnalyzedUpdate(
        update=Update(
            source="docker",
            subject="ghosttown",
            current_version="3.0",
            new_version="3.1",
            release_notes="",
            context={"repo_health": {"status": "abandoned", "reason": "archived"}},
        ),
        analysis=Analysis(severity=Severity.HIGH, summary="upstream archived"),
        status=UpdateStatus.ANALYZED,
        detected_at=now - timedelta(days=2),
    )
    db.upsert(abandoned)


def test_collect_stats_respects_cutoff_and_orders_by_severity(tmp_path):
    now = datetime(2026, 6, 1, 12, 0, 0)
    db = Database(tmp_path / "state.sqlite")
    try:
        _seed_db(db, now)
        stats = collect_stats(db, days=7, now=now)
    finally:
        db.close()

    # 4 in-window severity rows + 1 orphan + 1 abandoned = 6, ancient excluded
    assert (stats.counts_by_severity.get("critical", 0)) == 1
    assert (stats.counts_by_severity.get("high", 0)) == 2  # tintes + abandoned
    assert "ancient" not in [it.update.subject for it in stats.top_items]
    # Top item is the critical one
    assert stats.top_items[0].update.subject == "openclaw"
    # Orphan / abandoned shortcuts
    assert [it.update.subject for it in stats.orphan_items] == ["forgotten"]
    assert [it.update.subject for it in stats.abandoned_repos] == ["ghosttown"]


def test_render_markdown_handles_empty_window():
    stats = DigestStats(
        period_start=datetime(2026, 5, 25),
        period_end=datetime(2026, 6, 1),
        counts_by_severity={},
        new_total=0,
        analyzed_total=0,
        top_items=[],
        orphan_items=[],
        abandoned_repos=[],
    )
    md = render_markdown(stats, public_ui_url="http://homelabsage.local")
    assert "Nothing detected" in md
    assert "http://homelabsage.local" in md


def test_render_markdown_includes_sections(tmp_path):
    now = datetime(2026, 6, 1, 12, 0, 0)
    db = Database(tmp_path / "state.sqlite")
    try:
        _seed_db(db, now)
        stats = collect_stats(db, days=7, now=now)
    finally:
        db.close()
    md = render_markdown(stats)
    assert "HomelabSage digest" in md
    assert "openclaw" in md  # critical highlighted
    assert "Orphans" in md
    assert "forgotten" in md
    assert "Abandoned upstreams" in md
    assert "ghosttown" in md
    # No fenced placeholders left over
    assert "{" not in md


def test_write_to_notes_atomic(tmp_path):
    body = "# digest body"
    out = write_to_notes(body, tmp_path / "notes")
    assert out and out.exists()
    assert out.read_text() == body + "\n"
    # Re-writing replaces, doesn't append
    out2 = write_to_notes("# v2", tmp_path / "notes")
    assert out2 and out2.read_text() == "# v2\n"


def test_write_to_notes_noop_when_empty():
    assert write_to_notes("body", "") is None


# ─── Dispatchers ─────────────────────────────────────────────────────────


class _Recorder:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        pass
    async def post(self, url, **kw):
        self.calls.append({"url": url, **kw})
        class _R:
            status_code = 200
            text = ""
            def raise_for_status(self): pass
        return _R()


@pytest.fixture
def httpx_recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: rec)
    return rec


async def test_deliver_routes_to_enabled_channels(httpx_recorder):
    cfg = Config(
        outputs=OutputsConfig(
            telegram=TelegramOutputConfig(
                enabled=True, bot_token="bot", chat_id="42"
            ),
            discord=DiscordOutputConfig(
                enabled=True, webhook_url="http://discord/hook"
            ),
            ntfy=NtfyOutputConfig(
                enabled=False, server_url="https://ntfy.sh", topic="x"
            ),
            gotify=GotifyOutputConfig(
                enabled=True, server_url="http://gotify", token="tk"
            ),
        ),
        digest=DigestConfig(enabled=True),
    )
    results = await deliver(
        cfg, "## body", channels=["telegram", "discord", "ntfy", "gotify", "bogus"]
    )
    assert results["telegram"] == "sent"
    assert results["discord"] == "sent"
    # ntfy disabled — sender returns silently, deliver records "skipped"
    assert "skipped" in results["ntfy"]
    assert results["gotify"] == "sent"
    assert results["bogus"] == "unknown channel"
    # Three real HTTP calls + zero for the disabled ntfy / bogus.
    assert len(httpx_recorder.calls) == 3


async def test_run_digest_dry_run_does_not_send(tmp_path, httpx_recorder):
    cfg = Config(
        outputs=OutputsConfig(
            telegram=TelegramOutputConfig(enabled=True, bot_token="b", chat_id="c"),
        ),
        digest=DigestConfig(enabled=True),
    )
    cfg.storage.database_path = str(tmp_path / "state.sqlite")
    body, results, notes_path = await run_digest(cfg, dry_run=True)
    assert "HomelabSage digest" in body
    assert results == {}
    assert notes_path is None
    # Critically: zero HTTP calls in dry-run.
    assert httpx_recorder.calls == []


async def test_run_digest_auto_picks_enabled_channels(tmp_path, httpx_recorder):
    cfg = Config(
        outputs=OutputsConfig(
            telegram=TelegramOutputConfig(enabled=True, bot_token="b", chat_id="c"),
            discord=DiscordOutputConfig(enabled=False, webhook_url=""),
        ),
        digest=DigestConfig(enabled=True, channels=[]),
    )
    cfg.storage.database_path = str(tmp_path / "state.sqlite")
    _, results, _ = await run_digest(cfg)
    # discord disabled → not in auto-pick → not in results
    assert "telegram" in results
    assert "discord" not in results


def test_digest_config_rejects_unknown_channel():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DigestConfig(channels=["pigeon"])
