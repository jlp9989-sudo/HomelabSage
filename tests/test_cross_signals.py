"""Cross-signal enrichment in the engine: will-it-fit + backup freshness +
the pin-injection refactor that moved into `_inject_cross_signals`.

These exercise the engine glue (scan-level probe → per-row injection), not
the pure verdict math — that lives in test_image_fit / test_image_pins.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from homelabsage.backup_health import BackupHealth, BackupHealthReport
from homelabsage.config import Config
from homelabsage.db import Database
from homelabsage.engine import Engine
from homelabsage.models import Update

_MIB = 1024 * 1024


def _engine(tmp_path: Path, cfg: Config | None = None) -> tuple[Engine, Database]:
    cfg = cfg or Config()
    cfg.llm.provider = "disabled"
    cfg.scheduler.enabled = False
    db = Database(tmp_path / "s.sqlite")
    return Engine(cfg, db), db


def _docker_update(context: dict | None = None) -> Update:
    return Update(
        source="docker", subject="mealie",
        current_version="1.0", new_version="2.0",
        context=context or {},
    )


# ─── _inject_cross_signals ────────────────────────────────────────


def test_inject_attaches_image_fit_from_growth_and_free_space(tmp_path: Path):
    engine, db = _engine(tmp_path)
    try:
        update = _docker_update({"image_size_growth": {"new_mib": 600.0}})
        scan_ctx = {"free_space": {"free_bytes": 400 * _MIB, "path": "/mnt/cache"}}
        out = engine._inject_cross_signals(update, scan_ctx)
        fit = out.context["image_fit"]
        assert fit["verdict"] == "wont_fit"
        assert fit["path"] == "/mnt/cache"
        # The original growth signal is preserved alongside the new one.
        assert out.context["image_size_growth"]["new_mib"] == 600.0
    finally:
        db.close()


def test_inject_no_image_fit_without_free_space(tmp_path: Path):
    engine, db = _engine(tmp_path)
    try:
        update = _docker_update({"image_size_growth": {"new_mib": 600.0}})
        out = engine._inject_cross_signals(update, {})   # no free_space
        assert "image_fit" not in out.context
    finally:
        db.close()


def test_inject_no_image_fit_without_growth(tmp_path: Path):
    engine, db = _engine(tmp_path)
    try:
        update = _docker_update({})   # no image_size_growth
        scan_ctx = {"free_space": {"free_bytes": 1 * _MIB, "path": "/d"}}
        out = engine._inject_cross_signals(update, scan_ctx)
        assert "image_fit" not in out.context
    finally:
        db.close()


def test_inject_attaches_backup_health_from_scan_ctx(tmp_path: Path):
    engine, db = _engine(tmp_path)
    try:
        update = _docker_update({})
        scan_ctx = {"backup_health": {"repos": [{"repo": "main", "staleness_days": 9}]}}
        out = engine._inject_cross_signals(update, scan_ctx)
        assert out.context["backup_health"]["repos"][0]["repo"] == "main"
    finally:
        db.close()


def test_inject_still_attaches_pin_violation(tmp_path: Path):
    """Regression: moving pin enforcement into _inject_cross_signals must
    keep it working exactly as the old inline block did."""
    cfg = Config()
    cfg.image_pins.enabled = True
    cfg.image_pins.pins = {"mealie": "1.*"}
    engine, db = _engine(tmp_path, cfg)
    try:
        update = _docker_update({})   # new_version 2.0 crosses the 1.* pin
        out = engine._inject_cross_signals(update, {})
        assert out.context["pin_violation"]["pin"] == "1.*"
    finally:
        db.close()


# ─── _scan_enrichments ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scan_enrichments_free_space_when_fit_check_on(tmp_path: Path):
    cfg = Config()
    cfg.sources.docker.image_fit_check = True
    cfg.disk_pressure.paths = [str(tmp_path)]
    engine, db = _engine(tmp_path, cfg)
    try:
        enrich = await engine._scan_enrichments()
        assert "free_space" in enrich
        assert enrich["free_space"]["free_bytes"] > 0
        assert enrich["free_space"]["path"] == str(tmp_path)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_scan_enrichments_no_free_space_without_paths(tmp_path: Path):
    cfg = Config()
    cfg.sources.docker.image_fit_check = True
    cfg.disk_pressure.paths = []   # nothing to measure
    engine, db = _engine(tmp_path, cfg)
    try:
        assert "free_space" not in await engine._scan_enrichments()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_scan_enrichments_injects_only_stale_backups(tmp_path: Path, monkeypatch):
    cfg = Config()
    cfg.backup_health.enabled = True
    cfg.backup_health.inject_into_updates = True
    cfg.backup_health.repos = [
        {"name": "main", "tool": "restic"},
        {"name": "media", "tool": "restic"},
    ]
    engine, db = _engine(tmp_path, cfg)
    try:
        report = BackupHealthReport(results=[
            BackupHealth(repo_name="main", tool="restic", ok=True,
                         reason="restic ok", staleness_days=9, severity="high"),
            BackupHealth(repo_name="media", tool="restic", ok=True,
                         reason="restic ok", staleness_days=0, severity="info"),
        ])
        monkeypatch.setattr(engine, "_probe_backups", lambda: report)
        enrich = await engine._scan_enrichments()
        repos = enrich["backup_health"]["repos"]
        # Only the stale (high) repo is injected; the healthy (info) one isn't.
        assert [r["repo"] for r in repos] == ["main"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_scan_enrichments_omits_backup_when_all_healthy(tmp_path: Path, monkeypatch):
    cfg = Config()
    cfg.backup_health.enabled = True
    cfg.backup_health.inject_into_updates = True
    cfg.backup_health.repos = [{"name": "main", "tool": "restic"}]
    engine, db = _engine(tmp_path, cfg)
    try:
        report = BackupHealthReport(results=[
            BackupHealth(repo_name="main", tool="restic", ok=True,
                         reason="restic ok", staleness_days=0, severity="info"),
        ])
        monkeypatch.setattr(engine, "_probe_backups", lambda: report)
        assert "backup_health" not in await engine._scan_enrichments()
    finally:
        db.close()


# ─── prompt rules ─────────────────────────────────────────────────


def test_prompt_template_carries_cross_signal_rules():
    prompt = (
        Path(__file__).resolve().parent.parent
        / "src" / "homelabsage" / "prompts" / "analyzer.md"
    ).read_text(encoding="utf-8")
    assert "image_fit" in prompt
    assert "oom_killed" in prompt
    assert "backup_health" in prompt
