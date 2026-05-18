"""Tiny GitHub API helper — list releases, fetch release body, classify repo health.

Uses the public REST API. No auth required for low-volume use; if rate-limited,
pass GITHUB_TOKEN in the environment (read here, not from config — pure GH access,
no homelab data exposed).
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

GITHUB_API = "https://api.github.com"
CODEBERG_API = "https://codeberg.org/api/v1"


def _resolve_api(repo: str) -> tuple[str, str]:
    """Split a repo identifier into `(api_base, owner/repo)`.

    Bare `owner/repo` → GitHub. A `codeberg.org/owner/repo` prefix routes to
    Codeberg's Gitea/Forgejo-compatible API (same JSON shape for releases
    and repo metadata — `tag_name`, `body`, `archived`, `updated_at`).

    Adding a new host is a single elif branch: the response handlers down
    the line never need to know which host they're talking to.
    """
    if repo.startswith("codeberg.org/"):
        return CODEBERG_API, repo.removeprefix("codeberg.org/")
    return GITHUB_API, repo

# Repo health buckets. See `classify_repo_health` for the thresholds and why
# they were chosen.
RepoHealth = Literal["alive", "stale", "abandoned"]

# Default thresholds. Module-level constants (not Criteria-style dataclass) so
# the values are also documented in the CHANGELOG and easy to spot.
STALE_DAYS = 180        # >6 months no push → "stale": dev has slowed
ABANDONED_DAYS = 365    # >1 year no push  → "abandoned": likely dead for security purposes


def _headers(api_base: str = GITHUB_API) -> dict[str, str]:
    """Build the request headers for whichever forge host we're talking to.

    The `GITHUB_TOKEN` env var is *only* sent to api.github.com. Sending a
    GitHub PAT to Codeberg returns 401 ("invalid token") because the bearer
    schema is the same but the token namespace is not — and we'd rather make
    an anonymous request than blow it on the wrong host.
    """
    h = {"Accept": "application/vnd.github+json"}
    if api_base == GITHUB_API:
        h["X-GitHub-Api-Version"] = "2022-11-28"
        if token := os.environ.get("GITHUB_TOKEN"):
            h["Authorization"] = f"Bearer {token}"
    return h


async def list_releases(repo: str, per_page: int = 30) -> list[dict[str, Any]]:
    """List releases (most recent first). Empty list on 404 or transport error."""
    api_base, slug = _resolve_api(repo)
    url = f"{api_base}/repos/{slug}/releases"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers=_headers(api_base), params={"per_page": per_page})
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


async def repo_metadata(
    repo: str, *, client: httpx.AsyncClient | None = None
) -> dict[str, Any] | None:
    """Fetch the repository's metadata block.

    Returns the raw JSON from `GET /repos/{owner}/{name}` (so callers can pull
    `pushed_at`, `archived`, `open_issues_count`, `default_branch`, etc.),
    or None on any HTTP/transport error.
    """
    api_base, slug = _resolve_api(repo)
    url = f"{api_base}/repos/{slug}"
    owns = client is None
    if owns:
        client = httpx.AsyncClient(timeout=20)
    assert client is not None
    try:
        r = await client.get(url, headers=_headers(api_base))
        if r.status_code != 200:
            return None
        return r.json()
    except httpx.HTTPError:
        return None
    finally:
        if owns:
            await client.aclose()


# Permissive ISO8601: accept Z or ±HH:MM, with or without microseconds.
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


def _parse_github_ts(text: str | None) -> datetime | None:
    if not text or not _ISO_RE.match(text):
        return None
    s = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def classify_repo_health(
    metadata: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    stale_days: int = STALE_DAYS,
    abandoned_days: int = ABANDONED_DAYS,
) -> dict[str, Any]:
    """Bucket a repo into alive/stale/abandoned + return signals used.

    Decision rules (in order — first match wins):

      1. `archived: true` on the metadata → ABANDONED. GitHub's own signal,
         and the most reliable indicator a repo is no longer maintained.
      2. `pushed_at` older than `abandoned_days` (default 365) → ABANDONED.
         A full year without a commit is unambiguously dead for software:
         even quiet projects ship security patches once a year.
      3. `pushed_at` older than `stale_days` (default 180) → STALE.
         6 months without a commit means active development has stopped;
         the project may still work but updates are slowing.
      4. Otherwise → ALIVE.

    Why 180 / 365 and not 90 / 180? Many genuinely-maintained homelab tools
    (single-author hobby projects) go quiet for 3-4 months between releases.
    Flagging those as "stale" at 90 days would produce too many false
    positives. 180 days catches the projects that have actually slowed; 365
    catches the ones whose original author has moved on. Both numbers are
    configurable per-call so we can tighten them if the noise level is wrong.

    Returns a dict suitable for inclusion in `Update.context`:

        {
          "status": "alive" | "stale" | "abandoned",
          "pushed_at": "2025-..." or None,
          "days_since_push": int or None,
          "archived": bool,
          "open_issues": int or None,
          "reason": str,        # short human label of which rule fired
        }

    None metadata → status "unknown" + empty signals; the caller can decide
    whether to ignore the field or surface it as missing data.
    """
    if metadata is None:
        return {
            "status": "unknown",
            "pushed_at": None,
            "days_since_push": None,
            "archived": False,
            "open_issues": None,
            "reason": "metadata unavailable",
        }

    archived = bool(metadata.get("archived"))
    open_issues = metadata.get("open_issues_count")
    pushed_raw = metadata.get("pushed_at") or metadata.get("updated_at")
    pushed = _parse_github_ts(pushed_raw)
    now = now or datetime.now(UTC)

    days_since_push: int | None = None
    if pushed is not None:
        days_since_push = max(0, int((now - pushed).total_seconds() // 86400))

    status: RepoHealth
    reason: str
    if archived:
        status, reason = "abandoned", "github archived"
    elif days_since_push is not None and days_since_push >= abandoned_days:
        status, reason = "abandoned", f"no push in {days_since_push}d"
    elif days_since_push is not None and days_since_push >= stale_days:
        status, reason = "stale", f"no push in {days_since_push}d"
    else:
        status, reason = "alive", "recent activity"

    return {
        "status": status,
        "pushed_at": pushed_raw,
        "days_since_push": days_since_push,
        "archived": archived,
        "open_issues": open_issues,
        "reason": reason,
    }
