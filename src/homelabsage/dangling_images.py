"""Dangling-image detector.

A `<none>:<none>` image sits in `docker image ls -f dangling=true`
because it was orphaned by a pull (newer tag overwrote the
reference). Each one eats disk and never garbage-collects on its
own — `docker image prune` is the manual fix.

This module is a pure function over the docker SDK's `images.list()`:
walk each image, find the ones with no `RepoTags` (the canonical
"dangling" marker — `RepoTags == []` or all entries are
`<none>:<none>`), and report them. Caller does the SDK call so the
unit tests don't need docker.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DanglingImage:
    """One image with no usable repo:tag handle."""

    id_short: str           # first 12 chars of the sha256
    size_bytes: int
    created: str | None     # ISO 8601 when known

    def to_context(self) -> dict:
        return {
            "id_short": self.id_short,
            "size_bytes": self.size_bytes,
            "size_mib": round(self.size_bytes / (1024 ** 2), 1),
            "created": self.created,
        }


def _is_dangling(repo_tags: list | None) -> bool:
    """Return True iff the image has no usable repo:tag."""
    if not repo_tags:
        return True
    # `RepoTags == ["<none>:<none>"]` shows up after a layer-cache
    # orphan — treat as dangling too.
    return all(
        isinstance(t, str) and (t == "<none>:<none>" or not t)
        for t in repo_tags
    )


def find_dangling(images: list) -> list[DanglingImage]:
    """Pure walk over a list of docker SDK Image objects.

    Each image is expected to expose `id` (sha256:...) and `attrs`
    with `RepoTags`, `Size`, `Created`. Missing attrs degrade
    gracefully — we never raise on a partial image record.
    """
    out: list[DanglingImage] = []
    for img in images:
        attrs = getattr(img, "attrs", None) or {}
        if not _is_dangling(attrs.get("RepoTags")):
            continue
        raw_id = getattr(img, "id", None) or attrs.get("Id") or ""
        # `sha256:abc…` → trim prefix for the short form
        if isinstance(raw_id, str) and raw_id.startswith("sha256:"):
            raw_id = raw_id.split(":", 1)[1]
        id_short = (raw_id or "")[:12]
        out.append(DanglingImage(
            id_short=id_short,
            size_bytes=int(attrs.get("Size") or 0),
            created=str(attrs.get("Created")) if attrs.get("Created") else None,
        ))
    return out


__all__ = ["DanglingImage", "find_dangling"]
