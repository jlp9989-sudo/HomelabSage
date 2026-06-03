"""Tests for the v0.4.9 grab-bag: container_age, compose_validate,
retry_queue, stack_digest, renovate_config, pr_changelog."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from homelabsage.container_age import (
    AgeFinding,
    _extract_created,
)
from homelabsage.container_age import (
    evaluate as age_eval,
)
from homelabsage.models import Analysis, AnalyzedUpdate, Severity, Update
from homelabsage.pr_changelog import PRChangelog, _extract_pr_refs
from homelabsage.renovate_config import RenovateConfig, parse_config
from homelabsage.retry_queue import (
    next_delay_minutes,
    should_retry_now,
)
from homelabsage.stack_digest import (
    StackGroup,
    group_by_project,
    render_group_markdown,
    render_groups_summary,
)

# ─── container_age ──────────────────────────────────────────────────


def test_extract_created_from_state():
    attrs = {"State": {"Created": "2026-01-01T12:00:00Z"}}
    out = _extract_created(attrs)
    assert out == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_extract_created_truncates_nanos():
    """Docker emits 9-digit fractional seconds; python parses ≤6."""
    attrs = {"Created": "2026-01-01T12:00:00.123456789Z"}
    out = _extract_created(attrs)
    assert out is not None
    assert out.microsecond == 123456


def test_extract_created_returns_none_when_missing():
    assert _extract_created({}) is None
    assert _extract_created({"Created": ""}) is None
    assert _extract_created({"State": {}}) is None


def test_age_evaluate_below_threshold_is_none():
    attrs = {"Created": (datetime.now(UTC) - timedelta(days=10)).isoformat()}
    assert age_eval(attrs, warn_after_days=180) is None


def test_age_evaluate_above_threshold_returns_finding():
    created = datetime(2025, 1, 1, tzinfo=UTC)
    now = datetime(2026, 6, 1, tzinfo=UTC)
    attrs = {"Created": created.isoformat()}
    f = age_eval(attrs, warn_after_days=180, now=now)
    assert isinstance(f, AgeFinding)
    assert f.days_old > 180


def test_age_evaluate_handles_naive_now():
    attrs = {"Created": (datetime.now(UTC) - timedelta(days=200)).isoformat()}
    f = age_eval(attrs, warn_after_days=180, now=datetime.now())
    assert f is not None


# ─── retry_queue ────────────────────────────────────────────────────


def test_next_delay_minutes_progression():
    assert next_delay_minutes(0) == 0
    assert next_delay_minutes(1) == 15
    assert next_delay_minutes(2) == 60
    assert next_delay_minutes(3) == 360
    assert next_delay_minutes(4) == 1440
    # Past the schedule → None (give up)
    assert next_delay_minutes(5) is None
    assert next_delay_minutes(99) is None


def test_should_retry_now_inside_window():
    last = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    now = last + timedelta(minutes=20)
    d = should_retry_now(attempts=1, last_attempt_at=last, now=now)
    # attempts=1 → wait 15 min. 20 min elapsed → ready.
    assert d.should_retry
    assert not d.permanently_failed


def test_should_retry_now_outside_window():
    last = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    now = last + timedelta(minutes=5)
    d = should_retry_now(attempts=1, last_attempt_at=last, now=now)
    assert not d.should_retry


def test_should_retry_now_gives_up_past_schedule():
    last = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    now = last + timedelta(days=30)
    d = should_retry_now(attempts=10, last_attempt_at=last, now=now)
    assert d.permanently_failed
    assert not d.should_retry


def test_should_retry_now_handles_naive_inputs():
    last = datetime(2026, 6, 1, 12, 0)  # naive
    now = datetime(2026, 6, 1, 13, 0)
    d = should_retry_now(attempts=2, last_attempt_at=last, now=now)
    # attempts=2 → 60 min wait, 60 min elapsed exactly → ready
    assert d.should_retry


# ─── stack_digest ───────────────────────────────────────────────────


def _make_item(subject: str, project: str, sev: Severity = Severity.HIGH):
    return AnalyzedUpdate(
        update=Update(
            source="docker", subject=subject,
            current_version="1", new_version="2",
            context={"compose_project": project},
        ),
        analysis=Analysis(severity=sev, summary=f"{subject} summary"),
    )


def test_group_by_project_under_threshold_stays_singleton():
    items = [_make_item("a", "stack-x"), _make_item("b", "stack-x")]
    groups, singletons = group_by_project(items, min_group_size=3)
    assert groups == []
    assert len(singletons) == 2


def test_group_by_project_above_threshold_forms_group():
    items = [
        _make_item("a", "p"), _make_item("b", "p"), _make_item("c", "p"),
        _make_item("solo", "other"),
    ]
    groups, singletons = group_by_project(items, min_group_size=3)
    assert len(groups) == 1
    assert groups[0].project == "p"
    assert len(groups[0].items) == 3
    assert len(singletons) == 1
    assert singletons[0].update.subject == "solo"


def test_group_max_severity():
    g = StackGroup(project="p", items=[
        _make_item("a", "p", Severity.INFO),
        _make_item("b", "p", Severity.CRITICAL),
        _make_item("c", "p", Severity.MEDIUM),
    ])
    assert g.max_severity == "critical"


def test_render_group_markdown_lists_each_item():
    items = [_make_item(f"svc{i}", "stack") for i in range(3)]
    groups, _ = group_by_project(items, min_group_size=3)
    body = render_group_markdown(groups[0])
    assert "stack" in body
    for i in range(3):
        assert f"svc{i}" in body


def test_render_groups_summary_empty_is_empty():
    assert render_groups_summary([]) == ""


def test_group_by_project_no_project_falls_to_singletons():
    items = [
        AnalyzedUpdate(
            update=Update(
                source="docker", subject="x",
                current_version="1", new_version="2",
                context={},
            ),
            analysis=Analysis(severity=Severity.HIGH, summary="x"),
        ),
    ]
    groups, singletons = group_by_project(items)
    assert groups == []
    assert len(singletons) == 1


# ─── renovate_config ────────────────────────────────────────────────


def test_parse_config_strict_json():
    text = (
        '{"extends": ["config:base"], "automerge": true, '
        '"automergeType": "pr", "packageRules": [{}, {}]}'
    )
    cfg = parse_config(text)
    assert isinstance(cfg, RenovateConfig)
    assert cfg.extends == ["config:base"]
    assert cfg.automerge is True
    assert cfg.automerge_type == "pr"
    assert cfg.package_rules_count == 2


def test_parse_config_json5_with_comments_and_trailing_commas():
    text = """
    // Renovate config
    {
        "extends": ["config:js-app"], /* preset */
        "automerge": false,
    }
    """
    cfg = parse_config(text)
    assert cfg is not None
    assert cfg.extends == ["config:js-app"]
    assert cfg.automerge is False


def test_parse_config_extends_as_string_normalised_to_list():
    cfg = parse_config('{"extends": "config:base"}')
    assert cfg is not None
    assert cfg.extends == ["config:base"]


def test_parse_config_empty_returns_none():
    assert parse_config("") is None
    assert parse_config("   ") is None


def test_parse_config_unparseable_returns_none():
    assert parse_config("definitely not json at all 123 {{}") is None


def test_parse_config_array_at_root_returns_none():
    """Renovate config MUST be an object at the root."""
    assert parse_config('["config:base"]') is None


def test_parse_config_no_package_rules_means_zero():
    cfg = parse_config('{"extends": []}')
    assert cfg is not None
    assert cfg.package_rules_count == 0


# ─── pr_changelog ───────────────────────────────────────────────────


def test_extract_pr_refs_from_squash_merge():
    commits = [
        {"commit": {"message": "Fix the thing (#123)\n\nLonger body"}},
        {"commit": {"message": "Bump deps (#124)"}},
    ]
    entries = _extract_pr_refs(commits)
    assert len(entries) == 2
    assert entries[0].number == 123
    assert "Fix the thing" in entries[0].subject


def test_extract_pr_refs_skips_commits_without_pr_number():
    commits = [
        {"commit": {"message": "just a manual commit"}},
        {"commit": {"message": "Merge pull request #99 from x/y"}},
    ]
    entries = _extract_pr_refs(commits)
    assert len(entries) == 1
    assert entries[0].number == 99


def test_extract_pr_refs_dedups():
    commits = [
        {"commit": {"message": "Fix (#1)"}},
        {"commit": {"message": "Revert (#1) follow-up"}},
    ]
    entries = _extract_pr_refs(commits)
    assert len(entries) == 1
    assert entries[0].number == 1


def test_extract_pr_refs_handles_malformed_commit():
    commits = [
        {"commit": "not a dict"},
        None,
        {"not_commit": "skip"},
        {"commit": {"message": ""}},
    ]
    assert _extract_pr_refs(commits) == []


def test_build_changelog_skips_equal_base_head():
    from homelabsage.pr_changelog import build_changelog
    out = asyncio.run(build_changelog("foo/bar", base="v1", head="v1"))
    assert out is None


def test_build_changelog_handles_404(monkeypatch):
    from homelabsage import pr_changelog
    async def fake(repo, base, head, *, timeout=20.0):
        return None
    monkeypatch.setattr(pr_changelog, "_fetch_compare", fake)
    out = asyncio.run(pr_changelog.build_changelog(
        "foo/bar", base="v1", head="v2",
    ))
    assert out is None


def test_pr_changelog_to_context_round_trip():
    cl = PRChangelog(
        base="v1", head="v2", commits_in_range=5,
        entries=[],
    )
    ctx = cl.to_context()
    assert ctx["commits_in_range"] == 5
    assert ctx["base"] == "v1"


# ─── compose_validate ──────────────────────────────────────────────


def test_compose_validate_no_docker_returns_none(monkeypatch, tmp_path):
    """Missing docker binary → silent skip."""
    from homelabsage import compose_validate
    monkeypatch.setattr("shutil.which", lambda name: None)
    f = tmp_path / "compose.yaml"
    f.write_text("services:\n  x:\n    image: foo:1.0\n", encoding="utf-8")
    assert compose_validate.validate_compose_file(f) is None


def test_compose_validate_failure_surfaces_first_line(monkeypatch, tmp_path):
    from homelabsage import compose_validate

    def fake_run_config(file):
        return 1, "first error\nlots\nof\nlines"

    monkeypatch.setattr(
        compose_validate, "_run_docker_compose_config", fake_run_config,
    )
    f = tmp_path / "compose.yaml"
    f.write_text("garbage", encoding="utf-8")
    finding = compose_validate.validate_compose_file(f)
    assert finding is not None
    assert finding.error == "first error"


def test_compose_validate_success_returns_none(monkeypatch, tmp_path):
    from homelabsage import compose_validate
    monkeypatch.setattr(
        compose_validate, "_run_docker_compose_config",
        lambda file: (0, ""),
    )
    f = tmp_path / "compose.yaml"
    f.write_text("ok", encoding="utf-8")
    assert compose_validate.validate_compose_file(f) is None
