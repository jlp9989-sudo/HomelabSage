"""Tests for the image-size growth detector (bloatware signal)."""

from __future__ import annotations

from homelabsage import image_size as imgsize_mod
from homelabsage.image_size import ImageSizeGrowth, detect_growth
from homelabsage.registries import FloatingTagInfo


async def test_detect_growth_triggered_when_ratio_meets_threshold(monkeypatch):
    async def fake_info(slug, tag, *, client=None):
        return FloatingTagInfo(
            digest="sha256:abc", pushed_at=None, tag=tag,
            total_size_bytes=400 * 1024 * 1024,  # 400 MiB
        )

    monkeypatch.setattr(imgsize_mod, "dockerhub_tag_info", fake_info)
    growth = await detect_growth(
        "nginx:1.20", current_size_bytes=100 * 1024 * 1024, new_tag="1.25",
        threshold_ratio=2.0,
    )
    assert growth is not None
    assert growth.triggered is True
    assert growth.ratio == 4.0


async def test_detect_growth_below_threshold_not_triggered(monkeypatch):
    async def fake_info(slug, tag, *, client=None):
        return FloatingTagInfo(
            digest="x", pushed_at=None, tag=tag,
            total_size_bytes=150 * 1024 * 1024,
        )

    monkeypatch.setattr(imgsize_mod, "dockerhub_tag_info", fake_info)
    growth = await detect_growth(
        "nginx:1.20", current_size_bytes=100 * 1024 * 1024, new_tag="1.25",
        threshold_ratio=2.0,
    )
    assert growth is not None
    assert growth.triggered is False
    assert growth.ratio == 1.5


async def test_detect_growth_none_for_non_dockerhub(monkeypatch):
    """GHCR / quay / private registries: skip (no tag-info endpoint we cover)."""
    growth = await detect_growth(
        "ghcr.io/owner/img:1.0",
        current_size_bytes=100 * 1024 * 1024,
        new_tag="2.0",
    )
    assert growth is None


async def test_detect_growth_none_when_registry_lacks_size(monkeypatch):
    async def fake_info(slug, tag, *, client=None):
        return FloatingTagInfo(
            digest="x", pushed_at=None, tag=tag, total_size_bytes=0,
        )

    monkeypatch.setattr(imgsize_mod, "dockerhub_tag_info", fake_info)
    growth = await detect_growth("nginx:1.20", 100, "1.25")
    assert growth is None


async def test_detect_growth_none_when_local_size_unknown():
    """current_size_bytes=0 means we have nothing to compare against."""
    growth = await detect_growth("nginx:1.20", 0, "1.25")
    assert growth is None


async def test_detect_growth_survives_registry_failure(monkeypatch):
    async def boom(slug, tag, *, client=None):
        raise RuntimeError("dockerhub rate-limited")

    monkeypatch.setattr(imgsize_mod, "dockerhub_tag_info", boom)
    # Detector must swallow the error so the scan loop continues.
    growth = await detect_growth("nginx:1.20", 100 * 1024 * 1024, "1.25")
    assert growth is None


def test_image_size_growth_to_context_rounds_human_readable():
    g = ImageSizeGrowth(
        current_bytes=80 * 1024 * 1024,
        new_bytes=210 * 1024 * 1024,
        ratio=210 / 80,
        triggered=True,
    )
    ctx = g.to_context()
    assert ctx["current_mib"] == 80.0
    assert ctx["new_mib"] == 210.0
    assert ctx["ratio"] == 2.62
    assert ctx["triggered"] is True
