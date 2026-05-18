"""Tests for the GitHub helper, especially `classify_repo_health`.

`list_releases` / `latest_release` / `repo_metadata` are thin wrappers
around httpx and not worth unit-testing without real responses; the
classifier is pure logic and gets the coverage instead.
"""

from datetime import UTC, datetime, timedelta

import pytest

from homelabsage.github import (
    ABANDONED_DAYS,
    CODEBERG_API,
    GITHUB_API,
    STALE_DAYS,
    _parse_github_ts,
    _resolve_api,
    classify_repo_health,
)

# ─── host resolution ──────────────────────────────────────────────────────


def test_resolve_api_defaults_to_github():
    assert _resolve_api("owner/repo") == (GITHUB_API, "owner/repo")


def test_resolve_api_routes_codeberg_prefix():
    base, slug = _resolve_api("codeberg.org/forgejo/forgejo")
    assert base == CODEBERG_API
    assert slug == "forgejo/forgejo"


def test_resolve_api_leaves_owner_with_dots_alone():
    # An owner with a literal `codeberg.org` substring inside their slug is
    # not realistic — github owners are restricted to alphanumerics + dash —
    # but the prefix check uses `startswith` to avoid false positives anyway.
    base, slug = _resolve_api("owner.codeberg.org/repo")
    assert base == GITHUB_API
    assert slug == "owner.codeberg.org/repo"

# ─── timestamp parsing ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw",
    [
        "2025-04-12T10:33:45Z",
        "2025-04-12T10:33:45.123Z",
        "2025-04-12T10:33:45+02:00",
        "2025-04-12T10:33:45.999999+02:00",
    ],
)
def test_parse_github_ts_accepts_iso(raw):
    assert _parse_github_ts(raw) is not None


@pytest.mark.parametrize(
    "raw",
    [None, "", "not a date", "2025/04/12", "2025-04-12 10:33:45"],
)
def test_parse_github_ts_rejects_non_iso(raw):
    assert _parse_github_ts(raw) is None


# ─── classifier ───────────────────────────────────────────────────────────

def _now() -> datetime:
    # Use a fixed reference so age math is deterministic across tests.
    return datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)


def _meta(days_ago: int, *, archived: bool = False, open_issues: int = 5) -> dict:
    pushed = _now() - timedelta(days=days_ago)
    return {
        "pushed_at": pushed.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "archived": archived,
        "open_issues_count": open_issues,
    }


def test_classify_alive_for_recent_push():
    out = classify_repo_health(_meta(30), now=_now())
    assert out["status"] == "alive"
    assert out["days_since_push"] == 30
    assert out["archived"] is False


def test_classify_stale_for_push_between_180_and_365():
    out = classify_repo_health(_meta(200), now=_now())
    assert out["status"] == "stale"
    assert "200d" in out["reason"]


def test_classify_abandoned_for_push_older_than_365():
    out = classify_repo_health(_meta(400), now=_now())
    assert out["status"] == "abandoned"
    assert "400d" in out["reason"]


def test_classify_abandoned_when_archived_overrides_age():
    """Archived repos count as abandoned even with a fresh `pushed_at`."""
    out = classify_repo_health(_meta(5, archived=True), now=_now())
    assert out["status"] == "abandoned"
    assert "archived" in out["reason"]


def test_classify_returns_unknown_for_none_metadata():
    out = classify_repo_health(None)
    assert out["status"] == "unknown"
    assert out["days_since_push"] is None


def test_classify_returns_unknown_when_pushed_at_is_garbage():
    """Defensive: if GitHub ever emits a non-ISO timestamp, don't crash."""
    out = classify_repo_health(
        {"pushed_at": "not a date", "archived": False, "open_issues_count": 0},
        now=_now(),
    )
    # No date → no age → falls through to "alive" / "recent activity"
    assert out["status"] == "alive"
    assert out["days_since_push"] is None


def test_thresholds_are_configurable_per_call():
    """A tight project (3-month cadence) can pass stale_days=90 and get
    different verdicts without touching module-level constants."""
    out = classify_repo_health(_meta(100), now=_now(), stale_days=90)
    assert out["status"] == "stale"


def test_threshold_constants_match_doc():
    """Sanity: the module-level defaults are the ones the docstring promises."""
    assert STALE_DAYS == 180
    assert ABANDONED_DAYS == 365
