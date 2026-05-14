"""Best-effort context enrichers for the curator.

Three small fetchers, each independent and failure-tolerant:

  - `fetch_github_readme(repo)`     — README.md from raw.githubusercontent.
  - `fetch_docker_hub_description(image)` — full_description from Docker Hub.
  - `fetch_container_logs(container)` — last N lines via the docker SDK.

The curator calls them once per container before composing its prompts.
Each returns None when there's nothing to add — the prompt template then
renders an "(none)" placeholder instead of an empty section header.

Why a separate module: keeps the network code out of the curator core and
makes it trivially mockable in tests (one function per source).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)


# Hosts that aren't Docker Hub. Don't even probe them — Docker Hub's API
# would return 404s and we'd pay the timeout. Add new hosts here as needed.
_NON_DOCKER_HUB_HOSTS = (
    "ghcr.io",
    "lscr.io",
    "quay.io",
    "registry.gitlab.com",
    "gcr.io",
    "public.ecr.aws",
    "docker.elastic.co",
    "mcr.microsoft.com",
)


# README candidate branches in order — most repos default to main now, but
# enough still use master that the fallback pays for itself.
_README_BRANCHES = ("main", "master")
_README_FILES = ("README.md", "README", "readme.md", "Readme.md")


@dataclass
class Enrichment:
    """Container of optional context strings injected into the prompt.

    None means the source either failed or was disabled. Strings are
    already truncated to the caller-provided budget.
    """

    readme: str | None = None
    docker_hub: str | None = None
    logs: str | None = None


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 16] + "\n…[truncated]…"


async def fetch_github_readme(
    repo: str | None,
    *,
    max_chars: int = 8000,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """Return the upstream README as plaintext, or None if unavailable.

    Tries the typical default-branch + filename combinations in order.
    Stops on the first 200; returns None if every combination 404s or
    the network fails. The text is NOT parsed — we hand the raw Markdown
    to the LLM, which handles it natively.
    """
    if not repo or "/" not in repo:
        return None
    owner, _, name = repo.partition("/")
    if not owner or not name:
        return None

    async def _try(c: httpx.AsyncClient) -> str | None:
        for branch in _README_BRANCHES:
            for filename in _README_FILES:
                url = (
                    f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{filename}"
                )
                try:
                    r = await c.get(url, timeout=8.0, follow_redirects=True)
                except Exception as e:
                    log.debug("readme fetch %s failed: %s", url, e)
                    continue
                if r.status_code == 200 and r.text.strip():
                    return _truncate(r.text, max_chars)
        return None

    if client is not None:
        return await _try(client)
    async with httpx.AsyncClient() as fresh:
        return await _try(fresh)


def _is_docker_hub_image(image: str) -> bool:
    """True if `image` is hosted on hub.docker.com (the public registry).

    Heuristic: Docker Hub references never carry an explicit host or
    use the implicit `docker.io/`. Anything with `ghcr.io/`, `lscr.io/`
    etc. is a different registry.
    """
    if not image:
        return False
    # Strip any tag/digest suffix
    head = image.split("@", 1)[0].split(":", 1)[0]
    if "/" not in head:
        return True  # `nginx`, `redis` → official library, hub.docker.com
    host = head.split("/", 1)[0]
    # A "host" with a dot or colon is a real registry hostname; otherwise
    # it's a Docker Hub namespace like `bellamy` in `bellamy/wallos`.
    if "." not in host and ":" not in host:
        return True
    return host.lower() in {"docker.io", "index.docker.io"} or not any(
        host.lower().endswith(h) for h in _NON_DOCKER_HUB_HOSTS
    ) and host.lower() not in _NON_DOCKER_HUB_HOSTS


def _docker_hub_repo_path(image: str) -> str | None:
    """Convert an image reference to the `<namespace>/<repo>` path used
    by Docker Hub's API. Returns None when the image doesn't live there."""
    if not _is_docker_hub_image(image):
        return None
    head = image.split("@", 1)[0].split(":", 1)[0]
    # Strip any leading `docker.io/` / `index.docker.io/`
    for prefix in ("docker.io/", "index.docker.io/"):
        if head.startswith(prefix):
            head = head[len(prefix) :]
            break
    parts = head.split("/")
    if len(parts) == 1:
        return f"library/{parts[0]}"  # official images live under `library/`
    if len(parts) == 2:
        return f"{parts[0]}/{parts[1]}"
    # 3+ segments mean a registry-prefixed path we already rejected above
    return None


async def fetch_docker_hub_description(
    image: str | None,
    *,
    max_chars: int = 6000,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """Return the `full_description` field from Docker Hub, if it lives there.

    Unauthenticated — Docker Hub allows ~100 pulls/6h per IP on this
    endpoint, more than enough for a daily curate.
    """
    if not image:
        return None
    path = _docker_hub_repo_path(image)
    if path is None:
        return None
    url = f"https://hub.docker.com/v2/repositories/{path}/"

    async def _try(c: httpx.AsyncClient) -> str | None:
        try:
            r = await c.get(url, timeout=8.0, follow_redirects=True)
        except Exception as e:
            log.debug("docker hub fetch %s failed: %s", url, e)
            return None
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        desc = (data.get("full_description") or "").strip()
        if not desc:
            return None
        return _truncate(desc, max_chars)

    if client is not None:
        return await _try(client)
    async with httpx.AsyncClient() as fresh:
        return await _try(fresh)


def fetch_container_logs(
    container,
    *,
    tail: int = 30,
    max_chars: int = 3000,
) -> str | None:
    """Return the last `tail` log lines as a single string, or None.

    Synchronous — the docker SDK's `logs()` call is already blocking,
    and run time is dominated by the LLM call afterwards.

    Errors are swallowed: this is best-effort enrichment, not a critical
    path. Containers that produce binary output (rare) get the bytes
    decoded with `errors='replace'` so we never crash the curate.
    """
    if container is None or tail <= 0:
        return None
    try:
        raw = container.logs(tail=tail, stdout=True, stderr=True, timestamps=False)
    except Exception as e:
        log.debug("docker logs fetch failed for %r: %s", getattr(container, "name", "?"), e)
        return None
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    text = text.strip()
    if not text:
        return None
    return _truncate(text, max_chars)
