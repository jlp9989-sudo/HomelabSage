"""Tiny GitHub API helper — list releases, fetch release body.

Uses the public REST API. No auth required for low-volume use; if rate-limited,
pass GITHUB_TOKEN in the environment (read here, not from config — pure GH access,
no homelab data exposed).
"""

from __future__ import annotations

import os
from typing import Any

import httpx

GITHUB_API = "https://api.github.com"


def _headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token := os.environ.get("GITHUB_TOKEN"):
        h["Authorization"] = f"Bearer {token}"
    return h


async def list_releases(repo: str, per_page: int = 30) -> list[dict[str, Any]]:
    """List releases (most recent first). Empty list on 404 or transport error."""
    url = f"{GITHUB_API}/repos/{repo}/releases"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers=_headers(), params={"per_page": per_page})
            if r.status_code == 404:
                return []
            r.raise_for_status()
            return r.json()
    except httpx.HTTPError:
        return []


async def latest_release(repo: str, include_prereleases: bool = False) -> dict[str, Any] | None:
    """Return latest non-draft release for a repo, or None."""
    rels = await list_releases(repo, per_page=10)
    for r in rels:
        if r.get("draft"):
            continue
        if r.get("prerelease") and not include_prereleases:
            continue
        return r
    return None
