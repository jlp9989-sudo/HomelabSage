"""Tests for the Fedora dnf plugin.

Focus on the pure parsers — the SSH transport is mocked at the
plugin level so we never need a live host (or paramiko's network
machinery) in CI.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from homelabsage.config import FedoraSourceConfig
from homelabsage.plugins.fedora import (
    FedoraPlugin,
    build_userspace_summary,
    parse_dnf_check_update,
    split_critical,
)

SAMPLE_OUTPUT = """
Last metadata expiration check: 0:05:13 ago on Mon 18 May 2026.

kernel.x86_64                        6.19.13-200.fc43               updates
kernel-core.x86_64                   6.19.13-200.fc43               updates
linux-firmware.noarch                20260515-1.fc43                updates
mesa-libGL.x86_64                    25.0.4-1.fc43                  updates
vulkan-loader.x86_64                 1.4.310-1.fc43                 updates
glibc.x86_64                         2.41-3.fc43                    updates

bash.x86_64                          5.2.32-3.fc43                  updates
curl.x86_64                          8.12.0-1.fc43                  updates
nano.x86_64                          8.4-1.fc43                     updates
python3-requests.noarch              2.32.4-1.fc43                  updates

Obsoleting Packages
old-package.x86_64                   0.0.0-1.fc43                   updates
    replaces                         old-package-something.fc42
"""


# ─── parse_dnf_check_update ───────────────────────────────────────────────


def test_parse_extracts_three_columns():
    rows = parse_dnf_check_update(SAMPLE_OUTPUT)
    names = [r["name"] for r in rows]
    # All real packages are there, in order.
    assert names[:3] == ["kernel", "kernel-core", "linux-firmware"]
    # Banner / blank lines are ignored.
    assert all("Last metadata" not in n for n in names)


def test_parse_stops_at_obsoleting_section():
    rows = parse_dnf_check_update(SAMPLE_OUTPUT)
    assert all(r["name"] != "old-package" for r in rows)


def test_parse_handles_arch_and_version_correctly():
    rows = parse_dnf_check_update(SAMPLE_OUTPUT)
    kernel = next(r for r in rows if r["name"] == "kernel")
    assert kernel == {
        "name": "kernel",
        "arch": "x86_64",
        "version": "6.19.13-200.fc43",
        "repo": "updates",
    }


def test_parse_empty_when_no_updates():
    assert parse_dnf_check_update("") == []
    assert parse_dnf_check_update("Last metadata expiration check: …\n") == []


def test_parse_ignores_indented_continuation_lines():
    """Some dnf builds wrap long version strings; continuation lines start
    with whitespace and shouldn't be mistaken for new packages."""
    raw = "kernel.x86_64    6.19.13-200.fc43    updates\n    continuation noise\n"
    rows = parse_dnf_check_update(raw)
    assert [r["name"] for r in rows] == ["kernel"]


# ─── split_critical ───────────────────────────────────────────────────────


def test_split_critical_groups_kernel_firmware_mesa():
    rows = parse_dnf_check_update(SAMPLE_OUTPUT)
    cfg = FedoraSourceConfig()
    crit, others = split_critical(rows, cfg.critical_packages)
    crit_names = [r["name"] for r in crit]
    assert "kernel" in crit_names
    assert "kernel-core" in crit_names
    assert "linux-firmware" in crit_names
    assert "mesa-libGL" in crit_names
    assert "vulkan-loader" in crit_names
    assert "glibc" in crit_names
    other_names = [r["name"] for r in others]
    assert "bash" in other_names
    assert "curl" in other_names
    assert "kernel" not in other_names


def test_split_critical_empty_patterns_dumps_everything_in_others():
    rows = parse_dnf_check_update(SAMPLE_OUTPUT)
    crit, others = split_critical(rows, [])
    assert crit == []
    assert others == rows


def test_split_critical_custom_pattern():
    rows = parse_dnf_check_update(SAMPLE_OUTPUT)
    crit, others = split_critical(rows, [r"^bash$"])
    assert [r["name"] for r in crit] == ["bash"]
    assert "kernel" in [r["name"] for r in others]


# ─── build_userspace_summary ──────────────────────────────────────────────


def test_summary_lists_packages_with_repo():
    rows = parse_dnf_check_update(SAMPLE_OUTPUT)
    _, others = split_critical(rows, FedoraSourceConfig().critical_packages)
    summary = build_userspace_summary(others)
    assert "bash.x86_64" in summary
    assert "8.12.0-1.fc43" in summary
    assert "from updates" in summary


def test_summary_truncates_with_count_indicator():
    rows = [
        {"name": f"pkg{i}", "arch": "x86_64", "version": "1.0", "repo": "updates"}
        for i in range(40)
    ]
    summary = build_userspace_summary(rows, max_listed=10)
    assert summary.count("\n") <= 10  # 10 bullets + "and N more"
    assert "and 30 more" in summary


def test_summary_empty_for_no_others():
    assert build_userspace_summary([]) == ""


# ─── FedoraPlugin.scan ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scan_disabled_returns_empty():
    plugin = FedoraPlugin(FedoraSourceConfig(enabled=False))
    assert await plugin.scan() == []


@pytest.mark.asyncio
async def test_scan_missing_credentials_returns_empty():
    plugin = FedoraPlugin(
        FedoraSourceConfig(enabled=True, host="", user="", ssh_key_path="")
    )
    assert await plugin.scan() == []


@pytest.mark.asyncio
async def test_scan_emits_critical_individuals_plus_one_aggregate(tmp_path):
    # Create a dummy key file so the path check passes; the SSH call is mocked.
    key = tmp_path / "id_ed25519"
    key.write_text("fake")
    plugin = FedoraPlugin(
        FedoraSourceConfig(
            enabled=True,
            host="halo",
            user="javi",
            ssh_key_path=str(key),
        )
    )
    with patch.object(plugin, "_run_check_update", return_value=SAMPLE_OUTPUT):
        updates = await plugin.scan()

    subjects = [u.subject for u in updates]
    # All critical packages from the fixture are present individually.
    for name in ("kernel", "kernel-core", "linux-firmware", "mesa-libGL", "vulkan-loader", "glibc"):
        assert name in subjects
    # And there's exactly one aggregate at the end.
    assert subjects.count("fedora-userspace") == 1
    agg = next(u for u in updates if u.subject == "fedora-userspace")
    assert "packages pending" in agg.new_version
    assert agg.release_notes  # non-empty summary body


@pytest.mark.asyncio
async def test_scan_no_aggregate_when_disabled(tmp_path):
    key = tmp_path / "id_ed25519"
    key.write_text("fake")
    plugin = FedoraPlugin(
        FedoraSourceConfig(
            enabled=True,
            host="halo",
            user="javi",
            ssh_key_path=str(key),
            aggregate_others=False,
        )
    )
    with patch.object(plugin, "_run_check_update", return_value=SAMPLE_OUTPUT):
        updates = await plugin.scan()
    subjects = [u.subject for u in updates]
    assert "fedora-userspace" not in subjects


@pytest.mark.asyncio
async def test_scan_swallows_ssh_errors(tmp_path):
    key = tmp_path / "id_ed25519"
    key.write_text("fake")
    plugin = FedoraPlugin(
        FedoraSourceConfig(enabled=True, host="halo", user="javi", ssh_key_path=str(key))
    )
    with patch.object(plugin, "_run_check_update", side_effect=ConnectionError("net is down")):
        # A failure on one source must not crash the whole run; engine swallows
        # plugin exceptions, but the plugin itself also returns [] defensively.
        assert await plugin.scan() == []
