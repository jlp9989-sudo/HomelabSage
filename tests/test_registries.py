"""Tests for the OCI registry helper (Docker Hub digest comparison)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from homelabsage.registries import (
    FloatingTagInfo,
    dockerhub_tag_info,
    local_digest_for,
    parse_image_ref,
)

# ─── parse_image_ref ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ref, expected_host, expected_slug, expected_tag, hub",
    [
        # Official Docker Hub library image, bare.
        ("nginx", "docker.io", "library/nginx", "latest", True),
        ("redis:7", "docker.io", "library/redis", "7", True),
        # Docker Hub user repo with explicit tag.
        ("bellamy/wallos:latest", "docker.io", "bellamy/wallos", "latest", True),
        # Docker Hub image without an explicit tag → defaults to latest.
        ("linuxserver/plex", "docker.io", "linuxserver/plex", "latest", True),
        # Non-Hub host with port-less FQDN — recognised as separate registry.
        ("ghcr.io/jlp9989-sudo/homelabsage:0.2.0", "ghcr.io", "jlp9989-sudo/homelabsage", "0.2.0", False),
        ("lscr.io/linuxserver/plex:latest", "lscr.io", "linuxserver/plex", "latest", False),
        ("quay.io/jetstack/cert-manager-controller:v1.16.1", "quay.io", "jetstack/cert-manager-controller", "v1.16.1", False),
    ],
)
def test_parse_image_ref_recognises_common_shapes(
    ref, expected_host, expected_slug, expected_tag, hub
):
    parsed = parse_image_ref(ref)
    assert parsed is not None
    assert parsed.host == expected_host
    assert parsed.slug == expected_slug
    assert parsed.tag == expected_tag
    assert parsed.is_docker_hub is hub


def test_parse_image_ref_strips_digest_pin():
    parsed = parse_image_ref("bellamy/wallos@sha256:abc123def456")
    assert parsed is not None
    assert parsed.host == "docker.io"
    assert parsed.slug == "bellamy/wallos"
    assert parsed.tag == "latest"


def test_parse_image_ref_returns_none_for_empty():
    assert parse_image_ref("") is None


# ─── dockerhub_tag_info ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dockerhub_tag_info_returns_digest_and_timestamp():
    """A real-shaped Hub response with `digest` at the top level."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {
        "digest": "sha256:topleveldigest",
        "last_updated": "2026-05-18T09:30:00.123456Z",
    }
    client = AsyncMock()
    client.get = AsyncMock(return_value=mock_response)

    info = await dockerhub_tag_info("bellamy/wallos", "latest", client=client)

    assert info is not None
    assert info.digest == "sha256:topleveldigest"
    assert info.pushed_at == datetime.fromisoformat("2026-05-18T09:30:00.123456+00:00")
    assert info.tag == "latest"


@pytest.mark.asyncio
async def test_dockerhub_tag_info_falls_back_to_images_entry():
    """Multi-arch images carry the digest under `images[*].digest`."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {
        "digest": "",
        "last_updated": "2026-04-01T00:00:00Z",
        "images": [{"digest": "sha256:archonedigest", "architecture": "amd64"}],
    }
    client = AsyncMock()
    client.get = AsyncMock(return_value=mock_response)

    info = await dockerhub_tag_info("linuxserver/plex", "latest", client=client)
    assert info is not None
    assert info.digest == "sha256:archonedigest"


@pytest.mark.asyncio
async def test_dockerhub_tag_info_returns_none_for_404():
    mock_response = AsyncMock()
    mock_response.status_code = 404
    client = AsyncMock()
    client.get = AsyncMock(return_value=mock_response)

    info = await dockerhub_tag_info("does/not-exist", "latest", client=client)
    assert info is None


@pytest.mark.asyncio
async def test_dockerhub_tag_info_swallows_http_error():
    import httpx

    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.HTTPError("net down"))
    info = await dockerhub_tag_info("bellamy/wallos", "latest", client=client)
    assert info is None


@pytest.mark.asyncio
async def test_dockerhub_tag_info_returns_none_when_no_digest_anywhere():
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {"last_updated": "2026-04-01T00:00:00Z"}
    client = AsyncMock()
    client.get = AsyncMock(return_value=mock_response)

    info = await dockerhub_tag_info("ghost/repo", "latest", client=client)
    assert info is None


# ─── local_digest_for ─────────────────────────────────────────────────────


def test_local_digest_extracts_matching_repo():
    image_attrs = {
        "RepoDigests": [
            "bellamy/wallos@sha256:topdigest",
            "other/repo@sha256:notthis",
        ],
    }
    assert local_digest_for("bellamy/wallos:latest", image_attrs) == "sha256:topdigest"


def test_local_digest_falls_back_to_first_when_no_prefix_match():
    image_attrs = {"RepoDigests": ["other/repo@sha256:onlyone"]}
    assert local_digest_for("foo/bar:latest", image_attrs) == "sha256:onlyone"


def test_local_digest_returns_none_when_no_repo_digests():
    assert local_digest_for("foo/bar:latest", {"RepoDigests": []}) is None


def test_local_digest_returns_none_for_empty_attrs():
    assert local_digest_for("foo/bar:latest", None) is None
    assert local_digest_for("foo/bar:latest", {}) is None


# ─── integration with the plugin branch ───────────────────────────────────


@pytest.mark.asyncio
async def test_floating_tag_update_emits_when_digests_differ(tmp_path):
    """Hand-roll a FakeContainer and verify the plugin emits one Update
    with the floating-tag shape (no semver, registry-style versions)."""
    from types import SimpleNamespace

    from homelabsage.config import DockerSourceConfig
    from homelabsage.plugins.docker import DockerPlugin

    c = SimpleNamespace(
        name="wallos",
        image=SimpleNamespace(
            labels={},
            tags=["bellamy/wallos:latest"],
            attrs={
                "RepoDigests": ["bellamy/wallos@sha256:localoldlocal"],
                "Created": "2026-01-01T00:00:00Z",
            },
        ),
        attrs={"Config": {"Image": "bellamy/wallos:latest", "Labels": {}}},
    )
    plugin = DockerPlugin(DockerSourceConfig(track_floating_tags=True))
    remote = FloatingTagInfo(
        digest="sha256:remotenewremote",
        pushed_at=datetime.fromisoformat("2026-05-18T09:00:00+00:00"),
        tag="latest",
    )
    with patch("homelabsage.plugins.docker.dockerhub_tag_info", AsyncMock(return_value=remote)):
        update = await plugin._floating_tag_update(c)

    assert update is not None
    assert update.subject == "wallos"
    assert update.current_version.startswith("local @ localoldloca")
    assert update.new_version.startswith("registry @ remotenew")
    assert update.context["registry"] == "docker.io"
    assert update.context["registry_slug"] == "bellamy/wallos"
    assert update.context["tag"] == "latest"


@pytest.mark.asyncio
async def test_floating_tag_skipped_when_digests_match(tmp_path):
    from types import SimpleNamespace

    from homelabsage.config import DockerSourceConfig
    from homelabsage.plugins.docker import DockerPlugin

    c = SimpleNamespace(
        name="wallos",
        image=SimpleNamespace(
            labels={},
            tags=["bellamy/wallos:latest"],
            attrs={"RepoDigests": ["bellamy/wallos@sha256:samedigest"]},
        ),
        attrs={"Config": {"Image": "bellamy/wallos:latest", "Labels": {}}},
    )
    plugin = DockerPlugin(DockerSourceConfig(track_floating_tags=True))
    remote = FloatingTagInfo(digest="sha256:samedigest", pushed_at=None, tag="latest")
    with patch("homelabsage.plugins.docker.dockerhub_tag_info", AsyncMock(return_value=remote)):
        assert await plugin._floating_tag_update(c) is None


@pytest.mark.asyncio
async def test_floating_tag_skipped_for_ghcr_image():
    """Only Docker Hub is queried in this pass; non-Hub hosts fall through."""
    from types import SimpleNamespace

    from homelabsage.config import DockerSourceConfig
    from homelabsage.plugins.docker import DockerPlugin

    c = SimpleNamespace(
        name="homelabsage",
        image=SimpleNamespace(
            labels={},
            tags=["ghcr.io/jlp9989-sudo/homelabsage:latest"],
            attrs={"RepoDigests": ["ghcr.io/jlp9989-sudo/homelabsage@sha256:x"]},
        ),
        attrs={
            "Config": {
                "Image": "ghcr.io/jlp9989-sudo/homelabsage:latest",
                "Labels": {},
            },
        },
    )
    plugin = DockerPlugin(DockerSourceConfig(track_floating_tags=True))
    assert await plugin._floating_tag_update(c) is None
