"""Tests for tag-pattern intelligence."""

from __future__ import annotations

import pytest

from homelabsage.tag_patterns import (
    classify_tag,
    infer_scheme,
    matches_scheme,
)

# ─── classify_tag ──────────────────────────────────────────────────


@pytest.mark.parametrize("tag,scheme", [
    # semver
    ("1.2.3", "semver"),
    ("v1.2.3", "semver"),
    ("1.2.3-rc1", "semver"),
    ("1.0", "semver"),
    # calver — 4-digit year prefix is canonical
    ("2026.06.01", "calver"),
    ("2026-06-01", "calver"),
    # LSIO-style build suffix
    ("1.4.0-ls123", "build_suffix"),
    ("4.5.0-alpine", "build_suffix"),
    ("3.0.0-bookworm", "build_suffix"),
    # digest
    ("sha-abc123def", "digest"),
    ("sha_abc123def456", "digest"),
    # floating
    ("latest", "floating"),
    ("main", "floating"),
    ("edge", "floating"),
    ("nightly", "floating"),
    ("release-1.x", "floating"),
    ("branch-2.x", "floating"),
    # unknown
    ("cuda12", "unknown"),
    ("openvino", "unknown"),
    ("special-build", "unknown"),
    ("", "unknown"),
])
def test_classify_tag_against_known_shapes(tag, scheme):
    assert classify_tag(tag) == scheme


# ─── infer_scheme ──────────────────────────────────────────────────


def test_infer_scheme_picks_dominant_semver():
    report = infer_scheme(["1.0.0", "1.1.0", "1.2.0", "latest"])
    assert report.dominant == "semver"
    assert report.confidence == 1.0   # 3/3 trackable
    assert report.floating_tags == ["latest"]
    assert report.total_tags == 4


def test_infer_scheme_picks_calver_for_date_dominant_lists():
    report = infer_scheme(["2026.06.01", "2026.05.15", "2026.04.02"])
    assert report.dominant == "calver"
    assert report.confidence == 1.0


def test_infer_scheme_picks_build_suffix_for_lsio_style():
    report = infer_scheme([
        "4.5.0-ls101",
        "4.5.0-ls102",
        "4.5.0-ls103",
        "latest",
    ])
    assert report.dominant == "build_suffix"


def test_infer_scheme_handles_mixed_scheme_lists():
    """Whichever scheme dominates wins."""
    report = infer_scheme([
        "1.0.0", "1.1.0", "1.2.0",     # 3 semver
        "2026.06.01",                    # 1 calver
        "latest",                        # 1 floating
    ])
    assert report.dominant == "semver"
    assert report.confidence == 0.75   # 3/4 trackable


def test_infer_scheme_floating_only_returns_floating():
    """Image that only publishes `latest` / `main` has no trackable scheme."""
    report = infer_scheme(["latest", "main", "edge"])
    assert report.dominant == "floating"
    assert len(report.floating_tags) == 3


def test_infer_scheme_empty_list_returns_unknown_low_confidence():
    report = infer_scheme([])
    assert report.dominant == "unknown"
    assert report.confidence == 0.0
    assert report.counts == {}


def test_infer_scheme_unknown_dominant_when_nothing_classifies():
    report = infer_scheme(["cuda12", "openvino", "weird"])
    assert report.dominant == "unknown"
    assert report.confidence == 1.0    # 3/3 unknown


# ─── matches_scheme ────────────────────────────────────────────────


def test_matches_scheme_returns_true_when_aligned():
    assert matches_scheme("1.0.0", "semver") is True
    assert matches_scheme("2026.06.01", "calver") is True


def test_matches_scheme_returns_false_when_misaligned():
    assert matches_scheme("cuda12", "semver") is False
    assert matches_scheme("1.0.0", "calver") is False
