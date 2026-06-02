"""Unit tests for CSI mode.

The docker SDK + LLM are stubbed; we exercise:
  - filter_interesting against representative log shapes,
  - latest_update_for against a real on-disk SQLite,
  - build_prompt rendering with and without an update on file,
  - run_csi end-to-end with a monkeypatched LLM.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from homelabsage.config import Config
from homelabsage.csi import (
    CsiResult,
    build_prompt,
    build_update_block,
    filter_interesting,
    gather_evidence,
    latest_update_for,
    run_csi,
)
from homelabsage.db import Database
from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
    UpdateStatus,
)

SAMPLE_LOGS = [
    "2026-06-01T10:00:00Z INFO  starting up",
    "2026-06-01T10:00:01Z INFO  loaded config",
    "2026-06-01T10:00:02Z WARNING  deprecated env LEGACY_PORT",
    "2026-06-01T10:00:03Z ERROR  failed to bind on :8000: address in use",
    "2026-06-01T10:00:04Z Traceback (most recent call last):",
    "2026-06-01T10:00:05Z   File 'foo.py', line 42, in main",
    "2026-06-01T10:00:06Z FATAL  shutting down",
    "",
    "  2026-06-01T10:00:07Z  ",
]


def test_filter_interesting_keeps_signal_drops_noise():
    out = filter_interesting(SAMPLE_LOGS)
    joined = "\n".join(out)
    assert "ERROR" in joined
    assert "FATAL" in joined
    assert "WARNING" in joined
    assert "Traceback" in joined
    # Pure-timestamp / empty lines get dropped
    assert "  2026-06-01T10:00:07Z  " not in out
    assert "" not in out


def test_filter_interesting_caps_tail_to_n():
    bulk = [f"ERROR line {i}" for i in range(200)]
    out = filter_interesting(bulk, tail=10)
    assert len(out) == 10
    assert out[-1] == "ERROR line 199"


def _seed_one_update(db: Database, container: str, when: datetime) -> AnalyzedUpdate:
    item = AnalyzedUpdate(
        update=Update(
            source="docker",
            subject=container,
            current_version="1.0",
            new_version="2.0",
            release_url="https://example.com/r",
            release_notes="",
            context={},
        ),
        analysis=Analysis(
            severity=Severity.HIGH,
            summary="schema migration on first start",
            breaking_changes=["env: OLD → NEW"],
            recommended_action="run `docker compose up -d` and wait",
        ),
        status=UpdateStatus.ANALYZED,
        detected_at=when,
    )
    db.upsert(item)
    return item


def test_latest_update_for_returns_matching_only(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        _seed_one_update(db, "openclaw", datetime(2026, 5, 30))
        _seed_one_update(db, "mealie",   datetime(2026, 5, 31))
        hit = latest_update_for(db, "openclaw")
        assert hit and hit.update.subject == "openclaw"
        miss = latest_update_for(db, "nonexistent")
        assert miss is None
    finally:
        db.close()


def test_build_update_block_without_update_is_explicit():
    s = build_update_block(None)
    assert "no update on record" in s


def test_build_update_block_with_update_includes_action(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    try:
        item = _seed_one_update(db, "openclaw", datetime(2026, 5, 30))
    finally:
        db.close()
    s = build_update_block(item)
    assert "1.0 → 2.0" in s
    assert "high" in s
    assert "OLD → NEW" in s
    assert "docker compose up" in s


def test_build_prompt_renders_all_sections():
    evidence = CsiResult(
        container_name="openclaw",
        last_update=None,
        log_lines=["ERROR something exploded"],
        notes_excerpt="versionlocked at 4.x because of foo",
        log_since=datetime(2026, 5, 30),
    )
    prompt = build_prompt(evidence)
    assert "openclaw" in prompt
    assert "ERROR something exploded" in prompt
    assert "versionlocked at 4.x" in prompt
    assert "no update on record" in prompt
    # Schema we expect the LLM to follow
    assert "What likely broke" in prompt


def test_gather_evidence_falls_back_to_window_when_no_update(tmp_path: Path, monkeypatch):
    """No row in DB + docker SDK absent → log_lines empty but no crash."""
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    cfg.notes.notes_dir = str(tmp_path / "notes")

    # Force fetch_docker_logs to return nothing without importing docker.
    monkeypatch.setattr("homelabsage.csi.fetch_docker_logs", lambda *a, **kw: [])

    ev = gather_evidence(cfg, "ghost-container")
    assert ev.container_name == "ghost-container"
    assert ev.last_update is None
    assert ev.log_lines == []
    # Falls back to "no notes" message rather than crashing
    assert "no notes" in ev.notes_excerpt.lower()


async def test_run_csi_returns_evidence_only_when_llm_disabled(tmp_path, monkeypatch):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    cfg.llm.provider = "disabled"

    monkeypatch.setattr(
        "homelabsage.csi.fetch_docker_logs",
        lambda *a, **kw: ["2026-06-01 ERROR boom"],
    )
    report, evidence = await run_csi(cfg, "alpha")
    assert "CSI evidence — alpha" in report
    assert "ERROR boom" in report
    assert evidence.log_lines == ["2026-06-01 ERROR boom"]


async def test_run_csi_calls_llm_when_enabled(tmp_path, monkeypatch):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "s.sqlite")
    cfg.llm.provider = "ollama"
    cfg.llm.endpoint = "http://x"
    cfg.llm.model = "fake"

    monkeypatch.setattr(
        "homelabsage.csi.fetch_docker_logs",
        lambda *a, **kw: ["2026-06-01 ERROR boom"],
    )

    captured: dict = {}

    async def fake_generate(self, prompt: str, *, temperature: float = 0.0) -> str:
        captured["prompt"] = prompt
        captured["temperature"] = temperature
        return "## Diagnosis\n- bound to busy port"

    # Patch the LLMClient.generate_text used inside run_csi.
    monkeypatch.setattr(
        "homelabsage.llm.LLMClient.generate_text",
        fake_generate,
    )

    report, _ = await run_csi(cfg, "alpha")
    assert "Diagnosis" in report
    assert captured["temperature"] == 0.0
    assert "alpha" in captured["prompt"]
    assert "ERROR boom" in captured["prompt"]
