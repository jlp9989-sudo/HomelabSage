"""'What I see' diagnostics — runtime inventory of how each container looks
to the scan engine.

Used by the `/diagnostics` web page (and the eponymous CLI subcommand).
Reports one row per running/stopped container with a verdict:

  - `tracked`         — repo resolved, semver available, would scan normally
  - `floating_tag`    — no semver but image is on Docker Hub → digest comparison
  - `no_repo`         — could not resolve a GitHub/Codeberg repo → skip
  - `no_version`      — repo found but no parseable version → skip
  - `skipped_by_rule` — matches a `skip:` regex in config
  - `override_used`   — repo came from `overrides:` mapping

The probe does NOT make outbound HTTP calls (no GitHub API hits) — it only
reads docker state and the user's config. Cheap enough to run on every page
load.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .config import DockerSourceConfig
from .plugins.docker import (
    _GHCR_RE,
    DockerPlugin,
    _looks_like_semver,
)

log = logging.getLogger(__name__)


@dataclass
class ContainerDiagnostic:
    """One row in the diagnostics table."""

    name: str
    status: str           # docker container status (running / exited / paused)
    image: str
    image_short: str
    verdict: str          # see module docstring
    repo: str | None = None
    current_version: str | None = None
    skip_pattern: str | None = None  # populated when verdict == skipped_by_rule
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "image": self.image,
            "image_short": self.image_short,
            "verdict": self.verdict,
            "repo": self.repo,
            "current_version": self.current_version,
            "skip_pattern": self.skip_pattern,
            "notes": self.notes,
        }


def diagnose_containers(cfg: DockerSourceConfig) -> list[ContainerDiagnostic]:
    """Walk every container the docker plugin would see and report verdict.

    Returns an empty list when the docker daemon is unreachable (the UI
    already handles that case via a banner). Never raises.
    """
    plugin = DockerPlugin(cfg)
    try:
        client = plugin._client()
        containers = client.containers.list(all=True)
    except Exception as e:
        log.warning("diagnostics: docker daemon unreachable: %s", e)
        return []

    skip_res = [(p, re.compile(p)) for p in cfg.skip]
    results: list[ContainerDiagnostic] = []
    for c in containers:
        status = (c.attrs.get("State", {}) or {}).get("Status", "")
        image_tag = (c.image.tags[0] if c.image.tags else "") or (
            c.attrs.get("Config", {}).get("Image", "") or ""
        )
        image_short = image_tag.split("/")[-1].split(":")[0] if image_tag else "(none)"

        diag = ContainerDiagnostic(
            name=c.name,
            status=str(status),
            image=image_tag or "(no image tag)",
            image_short=image_short,
            verdict="tracked",
        )

        # 1. Skipped by config?
        matched = next(
            ((pat, regex) for pat, regex in skip_res if regex.search(c.name)),
            None,
        )
        if matched is not None:
            diag.verdict = "skipped_by_rule"
            diag.skip_pattern = matched[0]
            results.append(diag)
            continue

        # 2. Repo resolution path mirrors DockerPlugin._find_github_repo
        diag.repo = plugin.resolve_repo(c)
        if diag.repo is None:
            # Floating-tag detection: when there's no semver tag but the
            # image lives on Docker Hub, the plugin would still surface it
            # via the digest path. Reflect that here.
            current = plugin.extract_version(c)
            # Plugin's registries.parse_image_ref handles full DH detection;
            # we replicate the simpler check (not GHCR, has a tag) to avoid
            # importing it. The combined condition mirrors the plugin's
            # branching exactly.
            if (
                cfg.track_floating_tags
                and not _looks_like_semver(current)
                and image_tag
                and not _GHCR_RE.match(image_tag)
            ):
                diag.verdict = "floating_tag"
                diag.current_version = current
                diag.notes.append(
                    "no semver tag — would compare image digest with Docker Hub"
                )
                results.append(diag)
                continue
            diag.verdict = "no_repo"
            diag.notes.append(
                "could not resolve a GitHub/Codeberg repo — add to "
                "`sources.docker.overrides` to track manually"
            )
            results.append(diag)
            continue

        # 3. Override used?
        if c.name in cfg.overrides:
            diag.notes.append(f"repo from overrides ({cfg.overrides[c.name]})")

        # 4. Version extraction
        diag.current_version = plugin.extract_version(c)
        if not diag.current_version:
            diag.verdict = "no_version"
            diag.notes.append(
                "image tag is not a semver and OCI image.version label missing"
            )
            results.append(diag)
            continue

        # Default: tracked
        results.append(diag)
    return results


def summarise(diagnostics: list[ContainerDiagnostic]) -> dict[str, int]:
    """Roll up to per-verdict counts for the page header."""
    counts: dict[str, int] = {}
    for d in diagnostics:
        counts[d.verdict] = counts.get(d.verdict, 0) + 1
    return counts
