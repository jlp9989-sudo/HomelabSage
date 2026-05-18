"""Tiny client for OCI registries — only what the floating-tag detector needs.

`floating tag` = a tag that doesn't encode a version (e.g. `latest`, `main`,
`edge`, `stable`, `nightly`). Semver-only matching skips these silently,
so containers running `image:latest` go un-tracked forever. To close that
gap we compare the local image digest against the registry's current
digest for the same tag: if they differ, an update is available — without
needing a release number.

Scope:
  * Docker Hub (anonymous) — covers 70%+ of typical homelab images. The
    `hub.docker.com` JSON API exposes `digest` and `last_updated` per tag
    with no auth, which is what we use here.
  * Other hosts (ghcr.io, lscr.io, quay.io, mcr…) — out of scope for this
    pass; they need auth tokens with different shapes per registry. The
    public helper returns None for them so the caller falls through.

The helper is `httpx.AsyncClient`-aware (callers can pass their own client
for connection pooling) but defaults to a fresh per-call client so simple
unit tests don't need a fixture.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)


# Anything that ISN'T Docker Hub. We don't try to query these because
# their auth model is different — adding them is a separate, future PR.
_NON_DOCKER_HUB_HOSTS = (
    "ghcr.io",
    "lscr.io",
    "quay.io",
    "registry.gitlab.com",
    "gcr.io",
    "public.ecr.aws",
    "docker.elastic.co",
    "mcr.microsoft.com",
    "cr.fluentbit.io",
    "registry.k8s.io",
    "codeberg.org",
)


@dataclass(frozen=True)
class ImageRef:
    """Parsed `host/owner/name:tag` reference.

    `host` is the canonical registry host (`docker.io` for Hub, even when
    the original ref omitted it). `slug` is `owner/name` (with `library/`
    prepended for official images so the Hub API takes it).
    `tag` is the tag string verbatim.
    """

    host: str
    slug: str
    tag: str

    @property
    def is_docker_hub(self) -> bool:
        return self.host == "docker.io"


def parse_image_ref(image_ref: str) -> ImageRef | None:
    """Split a Docker image reference into `(host, slug, tag)`.

    Returns None when the input is empty or visibly malformed. Defaults
    to Docker Hub when the host part is missing, matching the runtime's
    own behaviour.
    """
    if not image_ref:
        return None

    ref = image_ref.split("@", 1)[0]  # ignore @sha256:... digest pin
    name_part, sep, tag = ref.partition(":")
    if not sep or not tag:
        tag = "latest"
        name_part = ref

    if "/" in name_part:
        head, _, rest = name_part.partition("/")
        if "." in head or ":" in head or head == "localhost":
            host = head.lower()
            slug = rest
        else:
            host = "docker.io"
            slug = name_part
    else:
        # Bare `nginx`, `redis` etc — Hub official library namespace.
        host = "docker.io"
        slug = f"library/{name_part}"

    # Hub user image like `bellamy/wallos` already has its `/`; if a
    # non-Hub host left us with a bare repo, treat the host as part of
    # the slug to avoid an empty owner.
    if "/" not in slug and host == "docker.io":
        slug = f"library/{slug}"

    return ImageRef(host=host, slug=slug, tag=tag)


def _is_docker_hub_host(host: str) -> bool:
    host = host.lower()
    if host in {"docker.io", "index.docker.io", "registry-1.docker.io"}:
        return True
    return host not in _NON_DOCKER_HUB_HOSTS and "." not in host


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class FloatingTagInfo:
    """What we learned about a floating tag on a remote registry.

    `digest` is the manifest list / index digest for that tag — what
    `docker pull` would resolve to right now. `pushed_at` is the
    registry-reported timestamp of the most recent push that produced it.
    `tag` echoes the queried tag so the caller doesn't have to track it.
    """

    digest: str
    pushed_at: datetime | None
    tag: str
    raw: dict[str, Any] | None = None


async def dockerhub_tag_info(
    slug: str, tag: str, *, client: httpx.AsyncClient | None = None
) -> FloatingTagInfo | None:
    """Read `last_updated` + digest of a tag from Docker Hub, anonymously.

    Returns None on 404 (typo, private repo), transport error, or a
    response that doesn't carry the fields we need. Never raises.
    """
    url = f"https://hub.docker.com/v2/repositories/{slug}/tags/{tag}"
    owns = client is None
    if owns:
        client = httpx.AsyncClient(timeout=15)
    assert client is not None
    try:
        r = await client.get(url)
        if r.status_code != 200:
            log.debug("dockerhub tag %s:%s → HTTP %s", slug, tag, r.status_code)
            return None
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.debug("dockerhub tag %s:%s failed: %s", slug, tag, e)
        return None
    finally:
        if owns:
            await client.aclose()

    digest = data.get("digest") or ""
    if not digest:
        # Multi-arch images: digest is on `images[].digest`. Take the first
        # entry as a fingerprint — Hub guarantees the same `last_updated`
        # is bumped whenever ANY of the platform-specific blobs change, so
        # cross-arch comparisons stay coherent for our "did it move?" use.
        images = data.get("images") or []
        if images and isinstance(images, list):
            digest = images[0].get("digest") or ""
    if not digest:
        return None

    return FloatingTagInfo(
        digest=digest,
        pushed_at=_parse_iso(data.get("last_updated")),
        tag=tag,
        raw=data,
    )


def local_digest_for(image_ref: str, image_attrs: dict[str, Any] | None) -> str | None:
    """Extract the digest of the image the container is running.

    `image_attrs` is the `Image.attrs` dict from the Docker SDK (i.e.
    `client.images.get(...).attrs`), not the container's attrs — the
    container's `Image` field is just the SHA string. RepoDigests look
    like `lscr.io/linuxserver/plex@sha256:abc…`; we strip the prefix and
    return the bare `sha256:…` so it can be compared with the registry's
    value. Returns None when no RepoDigests are present (image was
    loaded locally, not pulled).
    """
    if not image_attrs:
        return None
    repo_digests: list[str] = image_attrs.get("RepoDigests") or []
    if not repo_digests:
        return None
    # Prefer the entry whose prefix matches the running image's repo.
    name_prefix = image_ref.split(":", 1)[0].split("@", 1)[0]
    for rd in repo_digests:
        if rd.startswith(name_prefix + "@"):
            _, _, digest = rd.partition("@")
            return digest or None
    # Fallback: first entry.
    _, _, digest = repo_digests[0].partition("@")
    return digest or None
