"""Watched repos plugin — analyse arbitrary GitHub/Codeberg repos the user
explicitly opts in to track.

Docker covers the running-container half of a homelab. This plugin covers
everything else the user runs in production but doesn't deploy as a container:
toolboxes (e.g. `kyuz0/amd-strix-halo-toolboxes`), one-shot scripts, dotfiles,
firmware bundles, custom integrations they pulled from a repo.

Scoping:
  - We intentionally do NOT auto-import a user's `starred` or `watching` list.
    Most starred repos are aspirational, not in-use. Noise floor is too high.
  - The user opts in per-repo via `homelabsage watched add owner/repo`.
  - State lives in a new `watched_repos` SQLite table — independent of the
    `updates` table so disabling/re-enabling a repo is a single column flip.

Update shape mirrors the docker plugin's contract: the analyzer sees the same
`repo_health` and release_notes context fields, so existing prompt rules fire
without changes.
"""

from __future__ import annotations

import logging
from typing import Any

from packaging.version import InvalidVersion, Version

from ..db import Database
from ..github import classify_repo_health, latest_release, repo_metadata
from ..models import Update
from . import Plugin

log = logging.getLogger(__name__)


class WatchedReposPlugin(Plugin):
    """Yields one Update per active watched repo with a newer release.

    Unlike DockerPlugin we don't compare against a *running* version — the
    user picks a `current_version` when they add the repo (or the plugin
    seeds it from the latest release on first scan). Subsequent scans
    surface any newer tag and let the analyzer evaluate it.
    """

    id = "github_watched"

    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _is_newer(current: str, candidate: str) -> bool:
        try:
            return Version(candidate.lstrip("v")) > Version(current.lstrip("v"))
        except InvalidVersion:
            # Fall back to a strict string-inequality on non-semver tags. This
            # is intentionally weaker than the docker plugin's path; some
            # repos use date-tags (`2026.06.01`) which `Version` rejects but
            # are still meaningfully comparable as strings.
            return bool(candidate) and current != candidate

    async def scan(self) -> list[Update]:
        watched = self.db.list_watched_repos(active_only=True)
        updates: list[Update] = []
        for w in watched:
            try:
                release = await latest_release(w["repo"])
            except Exception as e:
                log.warning("latest_release failed for %s: %s", w["repo"], e)
                continue
            if not release:
                continue
            new_version = (release.get("tag_name") or "").lstrip("v")
            if not new_version:
                continue

            current = w["current_version"] or ""
            # First scan after `watched add` with no version → seed the row so
            # the analyzer doesn't fire a phantom "0 → 1.2.3" update on every
            # add. The user can `watched set-version <id> <version>` if they
            # want the first scan to fire immediately.
            if not current:
                self.db.update_watched_repo_version(w["id"], new_version)
                continue
            if not self._is_newer(current, new_version):
                continue

            ctx: dict[str, Any] = {
                "repo": w["repo"],
                "watched_nickname": w["nickname"] or w["repo"],
                "_note_keywords": [
                    k for k in (w["repo"], w["repo"].split("/")[-1], w["nickname"]) if k
                ],
            }
            try:
                meta = await repo_metadata(w["repo"])
                health = classify_repo_health(meta)
                if health.get("status") != "unknown":
                    ctx["repo_health"] = health
            except Exception as e:
                log.debug("repo_metadata failed for %s: %s", w["repo"], e)

            # Best-effort README + topics so the analyzer sees what a
            # running-container plugin would get from docker inspect /
            # docker hub. One extra HTTP round-trip per Update.
            try:
                from ..watched_enrich import enrich
                ext = await enrich(w["repo"])
                if ext.topics or ext.readme_excerpt or ext.homepage:
                    ctx["watched_enrich"] = ext.to_context()
            except Exception as e:
                log.debug("watched_enrich failed for %s: %s", w["repo"], e)

            updates.append(Update(
                source=self.id,
                subject=w["nickname"] or w["repo"],
                current_version=current,
                new_version=new_version,
                release_url=release.get("html_url"),
                release_notes=release.get("body") or "",
                context=ctx,
            ))
        return updates
