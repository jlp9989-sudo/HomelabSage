"""Tests for v0.8.4: dangling-images detector + MCP."""

from __future__ import annotations

from types import SimpleNamespace

from homelabsage.config import Config
from homelabsage.dangling_images import (
    DanglingImage,
    _is_dangling,
    find_dangling,
)
from homelabsage.db import Database


def _img(*, image_id: str, repo_tags: list | None, size: int, created: str | None = None):
    return SimpleNamespace(
        id=image_id,
        attrs={
            "Id": image_id,
            "RepoTags": repo_tags,
            "Size": size,
            "Created": created,
        },
    )


# ─── _is_dangling unit ────────────────────────────────────────────


def test_is_dangling_when_empty_repo_tags():
    assert _is_dangling([]) is True
    assert _is_dangling(None) is True


def test_is_dangling_when_none_tag():
    assert _is_dangling(["<none>:<none>"]) is True


def test_is_dangling_false_for_real_tag():
    assert _is_dangling(["mealie:1.2.3"]) is False


# ─── find_dangling ────────────────────────────────────────────────


def test_find_dangling_picks_only_orphans():
    imgs = [
        _img(image_id="sha256:abc1234567890abcd", repo_tags=None, size=200 * 1024 ** 2),
        _img(image_id="sha256:def1234567890abcd", repo_tags=["mealie:1.2"], size=500 * 1024 ** 2),
        _img(image_id="sha256:ghi1234567890abcd", repo_tags=["<none>:<none>"], size=80 * 1024 ** 2),
    ]
    out = find_dangling(imgs)
    assert {f.id_short for f in out} == {"abc123456789", "ghi123456789"}


def test_find_dangling_trims_sha256_prefix():
    imgs = [_img(image_id="sha256:0123456789abc0123", repo_tags=None, size=0)]
    out = find_dangling(imgs)
    assert out[0].id_short == "0123456789ab"


def test_find_dangling_handles_missing_attrs():
    bad = SimpleNamespace(id="sha256:zzz", attrs=None)
    # Must not raise; treated as dangling (no repo_tags).
    out = find_dangling([bad])
    assert len(out) == 1


def test_dangling_image_to_context_shape():
    d = DanglingImage(id_short="abc12", size_bytes=10 * 1024 ** 2, created="2026-06-04T00:00:00Z")
    ctx = d.to_context()
    assert ctx["id_short"] == "abc12"
    assert ctx["size_mib"] == 10.0
    assert ctx["created"] == "2026-06-04T00:00:00Z"


# ─── MCP dangling_images ─────────────────────────────────────────


def test_mcp_dangling_images_handles_no_docker(monkeypatch):
    """When docker import / from_env fails, the tool returns ok=false."""
    import builtins

    from homelabsage.mcp import TOOLS
    original_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == "docker":
            raise ImportError("no docker")
        return original_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", boom)
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["dangling_images"]["impl"]
    out = impl(cfg, db, {})
    assert out["ok"] is False
    assert out["items"] == []


def test_mcp_dangling_images_returns_findings(monkeypatch):
    """When docker SDK works, we walk and report."""
    from homelabsage.mcp import TOOLS

    class _Images:
        def list(self, **kw):
            return [
                _img(image_id="sha256:abc1234567890abcd", repo_tags=None,
                     size=10 * 1024 ** 2),
            ]

    class _Client:
        def __init__(self): self.images = _Images()

    import sys
    fake_docker = SimpleNamespace(from_env=lambda: _Client())
    monkeypatch.setitem(sys.modules, "docker", fake_docker)
    cfg = Config()
    db = Database(":memory:")
    impl = TOOLS["dangling_images"]["impl"]
    out = impl(cfg, db, {})
    assert out["ok"] is True
    assert out["count"] == 1
    assert out["total_mib"] == 10.0
