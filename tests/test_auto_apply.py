"""Tests for the auto-apply whitelist."""

from __future__ import annotations

from homelabsage.auto_apply import (
    AutoApplyDecision,
    severity_ceiling_value,
    should_auto_apply,
)
from homelabsage.models import Analysis, AnalyzedUpdate, Severity, Update


def _item(
    *,
    subject: str = "blog",
    severity: Severity = Severity.INFO,
    breaking: list[str] | None = None,
    pin_violation: bool = False,
    analyzed: bool = True,
) -> AnalyzedUpdate:
    ctx: dict = {}
    if pin_violation:
        ctx["pin_violation"] = {"pin": "1.*", "reason": "crosses"}
    return AnalyzedUpdate(
        update=Update(source="docker", subject=subject,
                      current_version="1", new_version="2",
                      context=ctx),
        analysis=(
            Analysis(severity=severity, summary="x", breaking_changes=breaking or [])
            if analyzed else None
        ),
    )


def test_not_in_allowlist_blocks():
    d = should_auto_apply(_item(subject="other"), allowlist=["blog"])
    assert not d.should_apply
    assert "not in" in d.reason


def test_allowlist_match_with_info_severity_applies():
    d = should_auto_apply(_item(), allowlist=["blog"])
    assert d.should_apply
    assert isinstance(d, AutoApplyDecision)


def test_severity_above_ceiling_blocks():
    d = should_auto_apply(
        _item(severity=Severity.MEDIUM),
        allowlist=["blog"], max_severity="info",
    )
    assert not d.should_apply
    assert "ceiling" in d.reason


def test_higher_ceiling_lets_through():
    d = should_auto_apply(
        _item(severity=Severity.MEDIUM),
        allowlist=["blog"], max_severity="medium",
    )
    assert d.should_apply


def test_breaking_changes_block_even_when_allowlisted():
    d = should_auto_apply(
        _item(breaking=["env A→B"]),
        allowlist=["blog"], max_severity="info",
    )
    assert not d.should_apply
    assert "breaking_changes" in d.reason


def test_pin_violation_blocks_even_when_allowlisted():
    d = should_auto_apply(
        _item(pin_violation=True),
        allowlist=["blog"], max_severity="info",
    )
    assert not d.should_apply
    assert "pin" in d.reason


def test_no_analysis_yet_blocks():
    d = should_auto_apply(
        _item(analyzed=False),
        allowlist=["blog"], max_severity="info",
    )
    assert not d.should_apply
    assert "not analyzed" in d.reason


def test_allowlist_case_insensitive():
    d = should_auto_apply(
        _item(subject="MyBlog"),
        allowlist=["myblog"], max_severity="info",
    )
    assert d.should_apply


def test_empty_allowlist_blocks_everything():
    d = should_auto_apply(_item(), allowlist=[])
    assert not d.should_apply


def test_unknown_max_severity_treated_as_info():
    """Bogus ceiling string → ladder defaults to info (the safest reading)."""
    d = should_auto_apply(
        _item(severity=Severity.MEDIUM),
        allowlist=["blog"], max_severity="bogus",
    )
    assert not d.should_apply  # medium > info → block


def test_severity_ceiling_value_helper():
    assert severity_ceiling_value("info") == Severity.INFO
    assert severity_ceiling_value("critical") == Severity.CRITICAL
    # Unknown falls back to INFO
    assert severity_ceiling_value("nonsense") == Severity.INFO
