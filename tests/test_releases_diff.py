"""Tests for the multi-release diff summariser."""

from __future__ import annotations

import pytest

from homelabsage import releases_diff as rd_mod
from homelabsage.releases_diff import (
    DEFAULT_MAX_CHARS,
    VersionDiff,
    _between,
    _strip_noise,
    build_diff,
)

# ─── pure helpers ────────────────────────────────────────────────────────


@pytest.mark.parametrize("current,candidate,new,expected", [
    ("1.0.0", "1.0.1", "1.2.0", True),    # squarely between
    ("1.0.0", "1.2.0", "1.2.0", True),    # candidate == new is included
    ("1.0.0", "1.0.0", "1.2.0", False),   # candidate == current is excluded
    ("1.0.0", "0.9.0", "1.2.0", False),   # older than current
    ("1.0.0", "1.5.0", "1.2.0", False),   # newer than target
    ("v1.0.0", "v1.1.0", "v1.2.0", True), # leading v tolerated
])
def test_between_bounds_inclusive_on_target_only(current, candidate, new, expected):
    assert _between(current, new, candidate) is expected


def test_between_rejects_non_semver():
    """Non-semver pairs return False — we won't lie about ordering."""
    assert _between("calver-2026.05.01", "calver-2026.05.15", "calver-2026.06.01") is False


def test_strip_noise_removes_contributor_blocks():
    body = """\
## Highlights
- shipped feature X

### Contributors
Thanks to @alice and @bob.

## Breaking changes
- removed --legacy flag
"""
    out = _strip_noise(body)
    assert "shipped feature X" in out
    assert "removed --legacy flag" in out
    assert "@alice" not in out
    assert "@bob" not in out


def test_strip_noise_preserves_full_changelog_link_in_isolation():
    """We only kill the FULL CHANGELOG SECTION, not random mentions."""
    body = """\
- changes etc

**Full Changelog**: https://github.com/owner/repo/compare/v1...v2
"""
    out = _strip_noise(body)
    # Inline link survives because there's no `# Full Changelog` HEADER above it
    assert "Full Changelog" in out


def test_strip_noise_empty_returns_empty():
    assert _strip_noise("") == ""


# ─── build_diff: end-to-end with mocked list_releases ──────────────────


async def test_build_diff_concatenates_releases_strictly_between(monkeypatch):
    async def fake_list(repo, per_page=50):
        return [
            {"tag_name": "v2.0.0", "body": "## 2.0.0\n- removed legacy API", "prerelease": False, "draft": False},
            {"tag_name": "v1.5.0", "body": "## 1.5.0\n- added feature X", "prerelease": False, "draft": False},
            {"tag_name": "v1.2.0", "body": "## 1.2.0\n- patches", "prerelease": False, "draft": False},
            {"tag_name": "v1.0.0", "body": "## 1.0.0\n- initial", "prerelease": False, "draft": False},
        ]
    monkeypatch.setattr(rd_mod, "list_releases", fake_list)

    diff = await build_diff("owner/repo", current_version="1.0.0", new_version="2.0.0")
    assert diff is not None
    assert diff.from_version == "1.0.0"
    assert diff.to_version == "2.0.0"
    assert diff.releases_walked == 3       # 1.2.0, 1.5.0, 2.0.0
    assert "removed legacy API" in diff.body
    assert "added feature X" in diff.body
    assert "1.2.0" in " ".join(diff.versions_included)
    # 1.0.0 is the user's current — must NOT appear in versions_included
    assert "1.0.0" not in diff.versions_included


async def test_build_diff_skips_drafts_and_prereleases(monkeypatch):
    async def fake_list(repo, per_page=50):
        return [
            {"tag_name": "v2.0.0-rc1", "body": "rc", "prerelease": True, "draft": False},
            {"tag_name": "v1.5.0", "body": "stable", "prerelease": False, "draft": False},
            {"tag_name": "v1.0.0", "body": "initial", "prerelease": False, "draft": False},
        ]
    monkeypatch.setattr(rd_mod, "list_releases", fake_list)
    diff = await build_diff("owner/repo", current_version="1.0.0", new_version="2.0.0")
    assert diff is not None
    assert diff.versions_included == ["v1.5.0"]
    assert "rc" not in diff.body


async def test_build_diff_returns_none_when_no_intermediate_releases(monkeypatch):
    """Single-step upgrade — caller falls back to existing release_notes."""
    async def fake_list(repo, per_page=50):
        return [
            {"tag_name": "v1.0.0", "body": "initial", "prerelease": False, "draft": False},
        ]
    monkeypatch.setattr(rd_mod, "list_releases", fake_list)
    diff = await build_diff("owner/repo", current_version="1.0.0", new_version="1.0.1")
    assert diff is None


async def test_build_diff_truncates_at_max_chars(monkeypatch):
    huge = "A" * 50000
    async def fake_list(repo, per_page=50):
        return [
            {"tag_name": "v1.5.0", "body": huge, "prerelease": False, "draft": False},
            {"tag_name": "v1.0.0", "body": "x", "prerelease": False, "draft": False},
        ]
    monkeypatch.setattr(rd_mod, "list_releases", fake_list)
    diff = await build_diff("owner/repo", "1.0.0", "1.5.0", max_chars=2000)
    assert diff is not None
    assert diff.truncated is True
    assert len(diff.body) <= 2100  # 2000 budget + a sliver for the header literal


async def test_build_diff_caps_releases_walked(monkeypatch):
    async def fake_list(repo, per_page=50):
        # 20 intermediate releases — we should walk at most max_releases=5.
        return [
            {"tag_name": f"v1.{i}.0", "body": f"{i}", "prerelease": False, "draft": False}
            for i in range(20, 0, -1)
        ] + [{"tag_name": "v1.0.0", "body": "initial", "prerelease": False, "draft": False}]
    monkeypatch.setattr(rd_mod, "list_releases", fake_list)
    diff = await build_diff("owner/repo", "1.0.0", "1.20.0", max_releases=5)
    assert diff is not None
    assert diff.releases_walked == 5


async def test_build_diff_returns_none_for_unparseable_versions():
    diff = await build_diff("owner/repo", current_version="abc", new_version="def")
    assert diff is None


async def test_build_diff_returns_none_when_versions_blank():
    diff = await build_diff("owner/repo", current_version="", new_version="2.0.0")
    assert diff is None


async def test_build_diff_survives_list_releases_error(monkeypatch):
    async def boom(repo, per_page=50):
        raise RuntimeError("rate limited")
    monkeypatch.setattr(rd_mod, "list_releases", boom)
    diff = await build_diff("owner/repo", "1.0.0", "2.0.0")
    assert diff is None


def test_version_diff_to_context_includes_body():
    d = VersionDiff(
        from_version="1.0.0",
        to_version="2.0.0",
        releases_walked=3,
        body="body content",
        versions_included=["v1.5.0", "v2.0.0"],
        truncated=False,
    )
    ctx = d.to_context()
    assert ctx["body"] == "body content"
    assert ctx["versions_included"] == ["v1.5.0", "v2.0.0"]
    assert ctx["truncated"] is False


def test_default_max_chars_is_under_prompt_budget():
    """Guard: the diff body must not blow the 15K release_notes budget."""
    assert DEFAULT_MAX_CHARS < 15000
