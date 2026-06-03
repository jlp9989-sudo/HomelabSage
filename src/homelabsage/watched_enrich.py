"""Watched-repo enrichment — pull README excerpt + topics for the analyzer.

The docker plugin gets compose context, container labels, env vars, and
docker inspect for free. The watched-repos plugin sees none of that —
it only knows owner/repo + tag + release body. That's enough for the
analyzer when the release notes are thorough, but for repos with
sparse release notes (one-liners, version-bump auto-PRs) the verdict
gets shallow.

This module attaches the bits of GitHub metadata the analyzer can
actually use:

  - `topics`  — the repo's `topics` array, often the only "what does
                this do?" signal on a script-style repo without a README.
  - `readme`  — first ~3 KB of the README, normalised to text.
                Mirrors the docker plugin's `fetch_readme` behaviour.

Best-effort: a 404 / rate-limit / decode error returns empty values
and the analyzer falls back to whatever else it has.
"""

from __future__ import annotations

import base64
import binascii
import logging
from dataclasses import dataclass

import httpx

from .github import _headers, _resolve_api

log = logging.getLogger(__name__)


# Soft cap: enough for a project's lede + first section, small enough to
# not blow the analyzer's prompt budget when the user is watching many
# repos. The docker plugin uses the same constant for symmetry.
_README_BUDGET = 3000


@dataclass
class RepoEnrichment:
    """What we attach to `Update.context.watched_enrich`."""

    topics: list[str]
    readme_excerpt: str
    homepage: str | None

    def to_context(self) -> dict:
        return {
            "topics": self.topics,
            "readme_excerpt": self.readme_excerpt,
            "homepage": self.homepage,
        }


async def _fetch_json(url: str, *, headers: dict, timeout: float = 15.0) -> dict | list | None:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, headers=headers)
            if r.status_code != 200:
                return None
            return r.json()
    except httpx.HTTPError as e:
        log.debug("watched_enrich fetch %s failed: %s", url, e)
        return None


def _decode_readme_body(payload: dict) -> str:
    """GitHub `/readme` returns `{content: <base64>, encoding: 'base64'}`.

    Codeberg/Gitea returns plain text under `content` with `encoding:
    'base64'` too in their compat API, so the same decode path works.
    """
    if not isinstance(payload, dict):
        return ""
    content = payload.get("content")
    enc = (payload.get("encoding") or "").lower()
    if not isinstance(content, str):
        return ""
    if enc == "base64":
        try:
            return base64.b64decode(content).decode("utf-8", errors="replace")
        except (ValueError, binascii.Error):
            return ""
    return content


async def enrich(repo: str, *, budget: int = _README_BUDGET) -> RepoEnrichment:
    """Fetch topics + README excerpt for a watched repo.

    Never raises — every failure path returns the default empty values
    so the caller doesn't have to guard the assignment.
    """
    api_base, slug = _resolve_api(repo)
    headers = _headers(api_base)
    meta_url = f"{api_base}/repos/{slug}"
    readme_url = f"{api_base}/repos/{slug}/readme"

    meta = await _fetch_json(meta_url, headers=headers)
    readme_payload = await _fetch_json(readme_url, headers=headers)

    topics: list[str] = []
    homepage: str | None = None
    if isinstance(meta, dict):
        raw_topics = meta.get("topics")
        if isinstance(raw_topics, list):
            topics = [str(t) for t in raw_topics][:20]
        if isinstance(meta.get("homepage"), str) and meta["homepage"]:
            homepage = meta["homepage"]

    readme = ""
    if isinstance(readme_payload, dict):
        readme = _decode_readme_body(readme_payload)[:budget]

    return RepoEnrichment(topics=topics, readme_excerpt=readme, homepage=homepage)


__all__ = ["RepoEnrichment", "enrich"]
