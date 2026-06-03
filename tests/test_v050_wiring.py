"""Tests for v0.5.0 wiring: container_age + renovate + pr_changelog
flowing through the engine + auditor + prompt rules."""

from __future__ import annotations

from pathlib import Path

from homelabsage.audit import _container_age_findings
from homelabsage.config import Config, DockerSourceConfig
from homelabsage.models import (
    AnalyzedUpdate,
    Update,
)


def test_container_age_finding_severity_buckets():
    def _item(days: int) -> AnalyzedUpdate:
        return AnalyzedUpdate(update=Update(
            source="docker", subject="svc",
            current_version="1", new_version="2",
            context={"container_age": {"days_old": days}},
        ))

    # 200d → info
    assert _container_age_findings(_item(200))[0].severity == "info"
    # 400d → medium
    assert _container_age_findings(_item(400))[0].severity == "medium"
    # 800d → high
    assert _container_age_findings(_item(800))[0].severity == "high"


def test_container_age_finding_skipped_when_no_context():
    item = AnalyzedUpdate(update=Update(
        source="docker", subject="x",
        current_version="1", new_version="2",
        context={},
    ))
    assert _container_age_findings(item) == []


def test_docker_source_config_has_age_knob():
    cfg = DockerSourceConfig()
    assert cfg.container_age_warn_after_days == 180
    cfg2 = DockerSourceConfig(container_age_warn_after_days=0)
    assert cfg2.container_age_warn_after_days == 0


def test_prompt_template_mentions_new_signals():
    """Smoke test: the prompt template carries rules for the new
    context keys we attach."""
    prompt = (
        Path(__file__).resolve().parent.parent
        / "src" / "homelabsage" / "prompts" / "analyzer.md"
    ).read_text(encoding="utf-8")
    assert "renovate" in prompt
    assert "pr_changelog" in prompt
    assert "container_age" in prompt


def test_config_default_emits_known_keys():
    """Top-level Config still parses cleanly with the new docker knob."""
    cfg = Config()
    # New field exists
    assert hasattr(cfg.sources.docker, "container_age_warn_after_days")
    # Default is 180
    assert cfg.sources.docker.container_age_warn_after_days == 180
