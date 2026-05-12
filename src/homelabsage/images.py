"""Alternative-image detector.

Given a container image (e.g. `ghcr.io/imagegenius/immich:openvino-1.155`), find
other images that:

  * cover the same primary purpose (heuristic: short-name match or description
    overlap), AND
  * are significantly more popular than the current one (≥10× the pull count),
    AND
  * are actively maintained (last push within the last 90 days), AND
  * publish stable semver-shaped tags (so they're upgradeable, not "latest"-only).

Sources of candidates:

  1. Docker Hub repository search — broad, noisy. The cheap entry point. Each
     hit is enriched with a per-repo lookup when `last_updated` is missing
     (the search endpoint frequently omits it).
  2. LinuxServer.io direct probe — `hub.docker.com/v2/repositories/linuxserver/
     <short_name>/`. LSIO publishes every image to that namespace, and the
     standalone catalog (`fleet.linuxserver.io/api/v1/images`) was discontinued
     mid-2024. The direct Hub path is the only stable handle.

A planned third source (GitHub code search for `FROM <image>`) is out of scope
for this iteration; we'd add it only if the first two produce too few hits.

This module is pure async + httpx; it never writes to disk and never crosses
into the LLM. The caller hands the resulting list to the analyzer as one more
piece of `Update.context`, so the model decides whether to surface it — same
honesty discipline as the rest of the pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

log = logging.getLogger(__name__)


# Threshold defaults. Each one is justified in the docstring of `Criteria` so a
# future reader can see why these numbers were chosen rather than guess.
@dataclass(frozen=True)
class Criteria:
    """Tunables for `find_alternatives`. All defaults are documented inline.

    * `min_pulls_ratio = 10.0` — alternatives must have at least 10× the current
      image's pull count. A 2-3× delta is noise (forks of similar size); 10×
      means "much-more-adopted" which is the only signal worth surfacing
      unsolicited.
    * `min_absolute_pulls = 1_000_000` — fallback when the current image's pull
      count is unknown (e.g. GHCR, private registry). One million pulls is the
      lower bound at which Docker Hub reliably distinguishes "people use this"
      from "vanity repo". Below it, false-positive rate climbs fast.
    * `recent_push_days = 90` — kept in sync with the `STALE_DAYS` threshold in
      `homelabsage.github` (abandonware radar). If we wouldn't trust the
      *current* repo at >90d, we shouldn't propose alternatives at >90d either.
    * `min_name_similarity = 0.6` — Jaccard over tokenised short names. 0.6
      catches `immich-server` vs `immich` (≈0.66) but rejects unrelated names
      that just share one stop-word. 0.6 was chosen by walking a sample of
      Hub search results manually; below 0.5 false-positives swarm.
    * `min_description_jaccard = 0.4` — fallback similarity when names diverge
      (`linuxserver/jellyfin` vs `jellyfin/jellyfin`). Strict enough to require
      multiple shared content words, not just "self-hosted".
    """

    min_pulls_ratio: float = 10.0
    min_absolute_pulls: int = 1_000_000
    recent_push_days: int = 90
    min_name_similarity: float = 0.6
    min_description_jaccard: float = 0.4
    max_results: int = 5


# Stop-words filtered out before computing Jaccard similarity. Kept tight — we
# want signal-bearing tokens, not noise. Generic homelab vocabulary ("server",
# "container", "docker", "self-hosted") would match everything if left in.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "for", "with", "in", "on", "to",
        "is", "are", "be", "by", "from", "as", "at",
        "server", "client", "container", "docker", "image", "official",
        # `self-hosted` splits on the hyphen, so list each half explicitly.
        "self", "hosted", "selfhosted",
        "open", "source", "opensource",
        "free", "lightweight", "fast", "simple", "modern",
    }
)


# Re-use the docker plugin's semver definition. We keep this regex local rather
# than importing from `plugins.docker` to avoid a backward dependency between
# `images` (helper) and `plugins` (consumer).
_SEMVER_RE = re.compile(r"^v?\d+(?:\.\d+){1,3}")


@dataclass
class Alternative:
    """One candidate alternative image, normalised across sources."""

    image: str                 # canonical `owner/name` for Docker Hub, or full ref for others
    pulls: int                 # last known pull count
    last_pushed: datetime      # last image push / catalog update
    source: str                # "docker-hub" | "lsio"
    short_description: str = ""
    github_url: str | None = None
    sample_tag: str | None = None  # most-recent semver-shaped tag if any

    def to_context(self) -> dict[str, Any]:
        """Shape that goes into Update.context. Keep keys short — token cost."""
        return {
            "image": self.image,
            "pulls": self.pulls,
            "last_pushed": self.last_pushed.isoformat(),
            "source": self.source,
            "description": self.short_description[:200],
            "github_url": self.github_url,
            "tag": self.sample_tag,
        }


# ─── name / version helpers ────────────────────────────────────────────────

def _short_name(image_ref: str) -> str:
    """Extract a comparable short name from any image ref.

    The grammar is `[REGISTRY[:PORT]/][NAMESPACE/]NAME[:TAG]`, so we must split
    on `/` first (to skip the optional `registry.example.com:5000/` prefix)
    and only then strip the tag from the final segment.

    >>> _short_name("ghcr.io/imagegenius/immich:openvino-1.155")
    'immich'
    >>> _short_name("immich-app/immich-server:v1.155")
    'immich-server'
    >>> _short_name("jellyfin/jellyfin")
    'jellyfin'
    >>> _short_name("registry.example.com:5000/team/whatever:1.0")
    'whatever'
    """
    last_segment = image_ref.split("/")[-1]
    return last_segment.split(":", 1)[0].lower()


def _tokenise(text: str) -> set[str]:
    """Lowercase, split on non-alphanum, drop stopwords + single chars."""
    if not text:
        return set()
    parts = re.split(r"[^a-zA-Z0-9]+", text.lower())
    return {p for p in parts if len(p) > 1 and p not in _STOPWORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    """Symmetric set-overlap ratio. 1.0 = identical, 0.0 = disjoint."""
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def _has_recent_semver_tag(tags: list[str]) -> str | None:
    """Return the first tag that looks like semver, or None.

    A `latest`-only image is excluded: we can't propose "switch to this" if
    the user can't pin a version they'll keep getting updates for.
    """
    for t in tags:
        if _SEMVER_RE.match(t):
            return t
    return None


# ─── source clients ────────────────────────────────────────────────────────

DOCKER_HUB_SEARCH = "https://hub.docker.com/v2/search/repositories/"
DOCKER_HUB_REPO = "https://hub.docker.com/v2/repositories/"
# Enrichment fan-out cap. Hub's search endpoint omits `last_updated` on most
# rows; we fall back to the per-repo endpoint for the top-N to fill it in.
# Five is a defensible budget: enough to reach `Criteria.max_results` survivors
# in normal cases, low enough that a 5x latency multiplier doesn't blow up.
_ENRICH_TOP_N = 5


async def _query_docker_hub(
    term: str, *, client: httpx.AsyncClient, page_size: int = 25
) -> list[dict[str, Any]]:
    """Search Docker Hub repositories by free-text query."""
    try:
        r = await client.get(
            DOCKER_HUB_SEARCH,
            params={"query": term, "page_size": page_size},
            timeout=20,
        )
        if r.status_code != 200:
            return []
        return list(r.json().get("results") or [])
    except httpx.HTTPError as e:
        log.debug("Docker Hub search failed for %r: %s", term, e)
        return []


async def _query_docker_hub_pulls(
    repo: str, *, client: httpx.AsyncClient
) -> int | None:
    """Fetch the pull count for a specific `owner/name` Hub repo, or None."""
    try:
        r = await client.get(f"{DOCKER_HUB_REPO}{repo}/", timeout=15)
        if r.status_code != 200:
            return None
        return int(r.json().get("pull_count") or 0)
    except (httpx.HTTPError, ValueError, TypeError):
        return None


async def _query_docker_hub_tags(
    repo: str, *, client: httpx.AsyncClient, page_size: int = 25
) -> list[str]:
    """Return tag names for a Hub repo, newest first."""
    try:
        r = await client.get(
            f"{DOCKER_HUB_REPO}{repo}/tags/",
            params={"page_size": page_size},
            timeout=15,
        )
        if r.status_code != 200:
            return []
        return [t.get("name", "") for t in (r.json().get("results") or [])]
    except httpx.HTTPError:
        return []


async def _query_docker_hub_repo(
    repo: str, *, client: httpx.AsyncClient
) -> dict[str, Any] | None:
    """Fetch a single Hub repo's metadata (the same shape used by `_query_docker_hub_pulls`).

    Used to fill in `last_updated` when the search endpoint omits it.
    """
    try:
        r = await client.get(f"{DOCKER_HUB_REPO}{repo}/", timeout=15)
        if r.status_code != 200:
            return None
        return r.json()
    except httpx.HTTPError:
        return None


async def _query_lsio_direct(
    short_name: str, *, client: httpx.AsyncClient
) -> dict[str, Any] | None:
    """Probe `hub.docker.com/v2/repositories/linuxserver/<short_name>/`.

    LSIO publishes every image to that namespace. Two-line replacement for the
    `fleet.linuxserver.io/api/v1/images` catalog, which was retired mid-2024.
    Returns the raw Hub repo metadata or None.
    """
    return await _query_docker_hub_repo(f"linuxserver/{short_name}", client=client)


# ─── shape normalisation ───────────────────────────────────────────────────

def _parse_dt(text: str | None) -> datetime | None:
    """Parse a Docker Hub / LSIO ISO timestamp, tolerating shapes."""
    if not text:
        return None
    s = text.replace("Z", "+00:00")
    # Truncate fractional seconds beyond microseconds (Hub sometimes returns 7 digits).
    m = re.match(r"^(.*\.\d{1,6})\d*(\+\d{2}:\d{2}|-\d{2}:\d{2})$", s)
    if m:
        s = m.group(1) + m.group(2)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _hub_search_row_to_partial(row: dict[str, Any]) -> dict[str, Any] | None:
    """Pull `(name, pulls, short_description)` out of a Hub search row.

    Does NOT build an `Alternative` — search rows usually have no
    `last_updated`, so we defer that to enrichment.
    """
    name = row.get("repo_name") or row.get("name") or ""
    if not name:
        return None
    if "/" not in name:
        # Implicit "library/" namespace (Docker official images).
        name = f"library/{name}"
    return {
        "name": name,
        "pulls": int(row.get("pull_count") or 0),
        "short_description": str(row.get("short_description") or ""),
        "last_updated": row.get("last_updated"),
    }


def _hub_repo_to_alt(
    name: str, repo: dict[str, Any], *, source: str = "docker-hub"
) -> Alternative | None:
    """Convert a per-repo Hub metadata blob (the shape returned by /v2/repositories/<name>/)
    into an Alternative. Returns None if `last_updated` is unparseable.
    """
    pushed = _parse_dt(repo.get("last_updated"))
    if pushed is None:
        return None
    return Alternative(
        image=name,
        pulls=int(repo.get("pull_count") or 0),
        last_pushed=pushed,
        source=source,
        short_description=str(repo.get("description") or repo.get("short_description") or ""),
    )


# ─── filter ────────────────────────────────────────────────────────────────

def _filter_candidate(
    candidate: Alternative,
    *,
    current_image: str,
    current_short: str,
    current_short_tokens: set[str],
    current_pulls: int | None,
    current_description: str,
    crit: Criteria,
    now: datetime,
) -> bool:
    """Return True if `candidate` deserves to be surfaced to the LLM."""
    # 1) Don't propose the user's own image back at them.
    if candidate.image.lower() == current_image.lower():
        return False
    if _short_name(candidate.image) == current_short and candidate.image != current_image:
        # Same short name in a different namespace is the textbook "alternative
        # image" case (linuxserver/jellyfin vs jellyfin/jellyfin). Skip the
        # similarity check; jump straight to maintenance + pulls gates.
        name_match = True
    else:
        # Otherwise require a name-similarity floor OR a description-Jaccard floor.
        cand_tokens = _tokenise(_short_name(candidate.image))
        name_match = _jaccard(cand_tokens, current_short_tokens) >= crit.min_name_similarity
        if not name_match:
            cand_desc = _tokenise(candidate.short_description)
            user_desc = _tokenise(current_description)
            if _jaccard(cand_desc, user_desc) < crit.min_description_jaccard:
                return False

    # 2) Maintenance gate.
    if now - candidate.last_pushed > timedelta(days=crit.recent_push_days):
        return False

    # 3) Popularity gate.
    if current_pulls is not None and current_pulls > 0:
        if candidate.pulls < current_pulls * crit.min_pulls_ratio:
            return False
    elif candidate.pulls < crit.min_absolute_pulls:
        return False

    # All gates passed. (`name_match` is only False when both name AND
    # description similarity failed — we already returned False above in that
    # case, so reaching here means at least one similarity check passed.)
    return True


# ─── orchestrator ──────────────────────────────────────────────────────────

@dataclass
class FindAlternativesResult:
    """Bundled output so the caller can log which sources fired."""

    candidates: list[Alternative] = field(default_factory=list)
    sources_consulted: list[str] = field(default_factory=list)
    current_pulls: int | None = None


async def find_alternatives(
    image: str,
    *,
    description: str = "",
    criteria: Criteria | None = None,
    client: httpx.AsyncClient | None = None,
) -> FindAlternativesResult:
    """Return up to `criteria.max_results` images that pass all gates.

    Network errors are swallowed per-source — the orchestrator returns
    whatever survived. Empty results are normal and safe.
    """
    crit = criteria or Criteria()
    short = _short_name(image)
    short_tokens = _tokenise(short)
    now = datetime.now(UTC)

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient()
    assert client is not None
    try:
        # Look up the current image's pull count first so the ratio gate has a
        # reference. Only works if the image lives on Docker Hub.
        current_pulls: int | None = None
        if image.startswith(("docker.io/", "registry-1.docker.io/")) or (
            "/" in image and "." not in image.split("/")[0]
        ):
            # `owner/name` (no domain) = Docker Hub by convention.
            current_pulls = await _query_docker_hub_pulls(
                image.split(":", 1)[0].replace("docker.io/", "").replace(
                    "registry-1.docker.io/", ""
                ),
                client=client,
            )

        # Parallel source queries.
        hub_task = _query_docker_hub(short, client=client)
        lsio_task = _query_lsio_direct(short, client=client)
        hub_rows, lsio_repo = await asyncio.gather(hub_task, lsio_task)

        # Stage 1: collect partial info from Hub search (no last_updated yet).
        partials: list[dict[str, Any]] = []
        for row in hub_rows:
            if p := _hub_search_row_to_partial(row):
                partials.append(p)
        # Sort by pulls so the per-repo enrichment fan-out targets the most
        # promising candidates.
        partials.sort(key=lambda p: p["pulls"], reverse=True)

        # Stage 2: build Alternatives. For each partial, if last_updated is
        # present we can build directly; otherwise fall back to a per-repo
        # lookup, capped at _ENRICH_TOP_N to bound HTTP fan-out.
        candidates_raw: list[Alternative] = []
        enrichment_budget = _ENRICH_TOP_N
        for p in partials:
            if _parse_dt(p["last_updated"]) is not None:
                alt = _hub_repo_to_alt(p["name"], {
                    "pull_count": p["pulls"],
                    "last_updated": p["last_updated"],
                    "short_description": p["short_description"],
                })
                if alt:
                    candidates_raw.append(alt)
                continue
            if enrichment_budget <= 0:
                continue
            enrichment_budget -= 1
            repo = await _query_docker_hub_repo(p["name"], client=client)
            if not repo:
                continue
            alt = _hub_repo_to_alt(p["name"], repo)
            if alt:
                candidates_raw.append(alt)

        # LSIO direct hit (if the image exists under linuxserver/<short>).
        if lsio_repo is not None:
            lsio_alt = _hub_repo_to_alt(
                f"linuxserver/{short}", lsio_repo, source="lsio"
            )
            if lsio_alt:
                candidates_raw.append(lsio_alt)

        # Sort again by pulls now that we have the full set.
        candidates_raw.sort(key=lambda a: a.pulls, reverse=True)

        kept: list[Alternative] = []
        for cand in candidates_raw:
            if _filter_candidate(
                cand,
                current_image=image.split(":", 1)[0],
                current_short=short,
                current_short_tokens=short_tokens,
                current_pulls=current_pulls,
                current_description=description,
                crit=crit,
                now=now,
            ):
                kept.append(cand)
                if len(kept) >= crit.max_results:
                    break

        # Best-effort: enrich top kept candidates with a sample semver tag, so
        # the LLM can cite something stable. Bounded fan-out: at most
        # max_results extra requests.
        for cand in kept:
            tags = await _query_docker_hub_tags(cand.image, client=client)
            cand.sample_tag = _has_recent_semver_tag(tags)

        return FindAlternativesResult(
            candidates=kept,
            sources_consulted=["docker-hub", "lsio"],
            current_pulls=current_pulls,
        )
    finally:
        if owns_client:
            await client.aclose()
