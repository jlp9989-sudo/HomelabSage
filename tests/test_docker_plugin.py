"""Unit tests for the Docker plugin's pure functions.

We don't talk to the Docker daemon here; we exercise the pure functions
that decide whether a tag looks like a version, whether one version is
newer than another, and whether a stopped container counts as orphan.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from homelabsage.config import DockerSourceConfig
from homelabsage.plugins.docker import (
    _SEMVER_RE,
    DockerPlugin,
    _orphan_days,
    _parse_docker_timestamp,
)


def _fake_container(
    *,
    name: str = "svc",
    labels: dict[str, str] | None = None,
    tags: list[str] | None = None,
    image_in_attrs: str = "",
) -> SimpleNamespace:
    """Minimal Container stand-in for resolve_repo tests.

    Only exposes the surface `_find_github_repo` reads — name, image.labels,
    image.tags, and Config.Image from attrs.
    """
    return SimpleNamespace(
        name=name,
        image=SimpleNamespace(labels=labels or {}, tags=tags or []),
        attrs={"Config": {"Image": image_in_attrs}},
    )


def test_semver_re_accepts_clean_semver():
    for v in ["1.2.3", "v0.10.0", "10.20.30", "2.5", "v3.0"]:
        assert _SEMVER_RE.match(v), v


def test_semver_re_rejects_variant_tags():
    for bad in ["openvino", "latest", "main", "edge", "stable",
                "cuda", "ubuntu-22.04-full", "alpine", "release-1.30.0",
                "rocm", ""]:
        assert _SEMVER_RE.match(bad) is None, bad


def test_is_newer_returns_true_for_higher_version():
    assert DockerPlugin._is_newer("1.0.0", "1.0.1") is True
    assert DockerPlugin._is_newer("1.0.0", "2.0.0") is True
    assert DockerPlugin._is_newer("v1.2.3", "1.2.4") is True


def test_is_newer_returns_false_for_same_or_older():
    assert DockerPlugin._is_newer("1.2.3", "1.2.3") is False
    assert DockerPlugin._is_newer("2.0.0", "1.9.9") is False


def test_is_newer_refuses_to_compare_non_semver():
    # Previous behaviour fell back to string `!=` and produced false positives
    # like 'openvino' != '2.7.5' → True. The fix returns False instead.
    assert DockerPlugin._is_newer("openvino", "2.7.5") is False
    assert DockerPlugin._is_newer("latest", "2.7.5") is False
    assert DockerPlugin._is_newer("main", "1.0.0") is False


# ─── timestamp parsing ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw, expected_year, expected_offset_hours",
    [
        # Plain RFC3339 with Z
        ("2025-04-12T10:33:45Z", 2025, 0),
        # With microseconds + Z (Docker's most common shape)
        ("2025-04-12T10:33:45.123456Z", 2025, 0),
        # With nanoseconds — Python only handles 6 digits, plugin must truncate
        ("2025-04-12T10:33:45.123456789Z", 2025, 0),
        # Explicit offset (less common from Docker but legal RFC3339)
        ("2025-04-12T10:33:45+02:00", 2025, 2),
        # Nanos + non-Z offset
        ("2025-04-12T10:33:45.987654321+02:00", 2025, 2),
    ],
)
def test_parse_docker_timestamp_accepts_real_shapes(raw, expected_year, expected_offset_hours):
    dt = _parse_docker_timestamp(raw)
    assert dt is not None
    assert dt.year == expected_year
    assert dt.utcoffset() is not None
    assert dt.utcoffset().total_seconds() == expected_offset_hours * 3600


def test_parse_docker_timestamp_returns_none_for_sentinel():
    # Docker uses 0001-01-01T00:00:00Z when the field is unset.
    assert _parse_docker_timestamp("0001-01-01T00:00:00Z") is None


def test_parse_docker_timestamp_returns_none_for_empty():
    assert _parse_docker_timestamp("") is None
    assert _parse_docker_timestamp("   ") is None


def test_parse_docker_timestamp_returns_none_for_garbage():
    assert _parse_docker_timestamp("not a timestamp") is None


# ─── orphan detection ────────────────────────────────────────────────────

def _ts(days_ago: int) -> str:
    """Helper: build a Docker-shaped timestamp <days_ago> days before now."""
    when = datetime.now(UTC) - timedelta(days=days_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def test_orphan_days_returns_none_for_running_container():
    assert _orphan_days("running", _ts(60)) is None


def test_orphan_days_returns_none_for_created_container():
    # Containers that never started don't count as orphan — they're a different
    # kind of problem (the user is mid-setup).
    assert _orphan_days("created", _ts(60)) is None


def test_orphan_days_returns_zero_when_just_exited():
    # `now=now` for determinism — explicit clock.
    now = datetime.now(UTC)
    just = (now - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    assert _orphan_days("exited", just, now=now) == 0


def test_orphan_days_returns_int_days_for_long_stopped():
    now = datetime.now(UTC)
    days = _orphan_days("exited", _ts(45), now=now)
    # Allow 44/45/46 depending on intraday cutoff
    assert days is not None and 44 <= days <= 46


def test_orphan_days_returns_none_for_exited_without_finished_at():
    # Edge case: status=exited but the FinishedAt slot is empty / sentinel.
    assert _orphan_days("exited", "") is None
    assert _orphan_days("exited", "0001-01-01T00:00:00Z") is None


# ─── resolve_repo from OCI labels ─────────────────────────────────────────


def _plugin(overrides: dict[str, str] | None = None) -> DockerPlugin:
    return DockerPlugin(DockerSourceConfig(overrides=overrides or {}))


def test_resolve_repo_reads_github_source_label():
    c = _fake_container(
        labels={"org.opencontainers.image.source": "https://github.com/linuxserver/docker-plex"}
    )
    assert _plugin().resolve_repo(c) == "linuxserver/docker-plex"


def test_resolve_repo_reads_codeberg_source_label():
    # Forgejo ships with this exact label. Before this support the detector
    # fell through and users had to add a fake `Forgejo: go-gitea/gitea`
    # override that pointed at the wrong project.
    c = _fake_container(
        name="Forgejo",
        labels={"org.opencontainers.image.source": "https://codeberg.org/forgejo/forgejo"},
    )
    assert _plugin().resolve_repo(c) == "codeberg.org/forgejo/forgejo"


def test_resolve_repo_override_wins_over_label():
    c = _fake_container(
        name="myservice",
        labels={"org.opencontainers.image.source": "https://github.com/upstream/repo"},
    )
    assert _plugin({"myservice": "fork/repo"}).resolve_repo(c) == "fork/repo"


def test_resolve_repo_falls_back_to_ghcr_heuristic():
    c = _fake_container(tags=["ghcr.io/jlp9989-sudo/homelabsage:0.2.0"])
    assert _plugin().resolve_repo(c) == "jlp9989-sudo/homelabsage"


def test_resolve_repo_returns_none_when_unknown():
    c = _fake_container(tags=["someorg/random-image:latest"])
    assert _plugin().resolve_repo(c) is None
