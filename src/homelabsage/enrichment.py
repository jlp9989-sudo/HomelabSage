"""Best-effort context enrichers for the curator.

Four small fetchers, each independent and failure-tolerant:

  - `fetch_github_readme(repo)`     — README.md from raw.githubusercontent.
  - `fetch_docker_hub_description(image)` — full_description from Docker Hub.
  - `fetch_container_logs(container)` — last N lines via the docker SDK.
  - `find_user_context(name, dirs)`  — grep user notes/memory for the name.

The curator calls them once per container before composing its prompts.
Each returns None when there's nothing to add — the prompt template then
renders an "(none)" placeholder instead of an empty section header.

Why a separate module: keeps the network code out of the curator core and
makes it trivially mockable in tests (one function per source).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

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
    user_context: str | None = None


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

    # Route by host prefix — same contract `github.py:_resolve_api` uses.
    # Bare `owner/repo` → GitHub raw; `codeberg.org/owner/repo` → Codeberg
    # raw (different URL shape: /{owner}/{repo}/raw/branch/{branch}/{file}).
    if repo.startswith("codeberg.org/"):
        slug = repo.removeprefix("codeberg.org/")

        def _url(owner: str, name: str, branch: str, filename: str) -> str:
            return f"https://codeberg.org/{owner}/{name}/raw/branch/{branch}/{filename}"
    else:
        slug = repo

        def _url(owner: str, name: str, branch: str, filename: str) -> str:
            return f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{filename}"

    owner, _, name = slug.partition("/")
    if not owner or not name:
        return None

    # Branches to probe. Start with the repo's actual default (forgejo's is
    # literally `forgejo`, not `main`) so we get a 200 on the first try when
    # the metadata lookup succeeds. Fall back to the historic main/master if
    # metadata is unavailable. _README_BRANCHES is appended last so a custom
    # default still benefits from the typical fallback chain.
    from .github import repo_metadata as _repo_metadata

    branches: list[str] = []
    try:
        meta = await _repo_metadata(repo)
        if meta and (default := meta.get("default_branch")):
            branches.append(default)
    except Exception:
        meta = None
    for b in _README_BRANCHES:
        if b not in branches:
            branches.append(b)

    async def _try(c: httpx.AsyncClient) -> str | None:
        for branch in branches:
            for filename in _README_FILES:
                url = _url(owner, name, branch, filename)
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


# Files we'll bother grepping. Markdown notes (the dominant case for
# both /opt/notes and /opt/claude-memory) plus plain-text fallbacks.
_USER_CONTEXT_GLOBS = ("*.md", "*.txt")

# How many extra lines around each match to include — gives the LLM a
# bit of surrounding context without dragging in whole pages.
_CONTEXT_LINES = 2


def _name_pattern(container_name: str) -> re.Pattern[str]:
    """Match the container_name as a whole token, case-insensitive.

    Allows the dash and dot delimiters typical in container names — the
    SQL identifier-style word boundary `\\b` doesn't cover them, so we
    build an explicit boundary class. Examples:
        FileBrowser-PNP, mealie-db, openclaw, ragflow-server.
    """
    boundary = r"(?:^|[^A-Za-z0-9_\-./])"
    return re.compile(
        boundary + re.escape(container_name) + boundary, re.IGNORECASE
    )


def find_user_context(
    container_name: str,
    search_dirs: list[str],
    *,
    max_chars: int = 4000,
    context_lines: int = _CONTEXT_LINES,
) -> str | None:
    """Grep the user's notes/memories for the container name and return
    surrounding snippets, or None if nothing matched.

    Each match becomes a block of `2*context_lines + 1` lines (centred
    on the matching line). Blocks are deduplicated within a file (a
    line that lives in two adjacent blocks is shown once). The output
    is `## file.md\\n<snippet>\\n…` per matching file.

    All access is read-only; symlinks are followed once via `Path.glob`
    (the bind-mount in production is `:ro` anyway). Errors per file are
    swallowed so one bad file doesn't drop the whole context.
    """
    if not container_name or not search_dirs:
        return None
    pattern = _name_pattern(container_name)
    out: list[str] = []
    budget = max_chars

    for dir_str in search_dirs:
        if budget <= 0:
            break
        root = Path(dir_str)
        if not root.is_dir():
            continue
        files: list[Path] = []
        for glob in _USER_CONTEXT_GLOBS:
            try:
                files.extend(sorted(root.rglob(glob)))
            except OSError:
                continue

        for fp in files:
            if budget <= 0:
                break
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = text.splitlines()
            hit_lines = {
                i for i, line in enumerate(lines) if pattern.search(line)
            }
            if not hit_lines:
                continue
            # Expand each hit into a window, then merge overlapping windows
            # so adjacent matches don't duplicate lines.
            windows: list[tuple[int, int]] = []
            for h in sorted(hit_lines):
                lo = max(0, h - context_lines)
                hi = min(len(lines) - 1, h + context_lines)
                if windows and lo <= windows[-1][1] + 1:
                    windows[-1] = (windows[-1][0], max(windows[-1][1], hi))
                else:
                    windows.append((lo, hi))

            # Render this file's snippets relative to root for readability
            try:
                rel = fp.relative_to(root)
            except ValueError:
                rel = fp
            block = [f"## {rel}"]
            for lo, hi in windows:
                block.append("\n".join(lines[lo : hi + 1]))
                block.append("…")
            block_text = "\n".join(block).rstrip("…\n").rstrip()
            chunk = block_text + "\n"
            if len(chunk) > budget:
                # Truncate the file block instead of dropping it whole —
                # partial context beats no context.
                chunk = chunk[: budget - 16] + "\n…[truncated]…\n"
            out.append(chunk)
            budget -= len(chunk)

    if not out:
        return None
    return "\n".join(out).rstrip()
