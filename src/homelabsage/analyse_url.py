"""On-demand URL analysis — paste a link, get the same analysis the engine
would produce on a scheduled scan.

Supported URL shapes:
  - `https://github.com/owner/repo`               (+ /tree/, /releases, …)
  - `https://codeberg.org/owner/repo`             (Forgejo-compatible)
  - `https://hub.docker.com/r/owner/image`        (Docker Hub user/org image)
  - `https://hub.docker.com/_/library_image`      (Docker Hub library image)

News articles + HuggingFace model cards remain roadmap'd separately; each
sub-case is its own engineering chunk and they share only this dispatcher.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import httpx

from .config import Config, get_active_llm_config
from .github import classify_repo_health, latest_release, repo_metadata
from .images import find_alternatives
from .llm import LLMClient
from .models import Analysis, AnalyzedUpdate, Update
from .notes import NotesProvider
from .registries import dockerhub_tag_info

log = logging.getLogger(__name__)


# Anchor to host plus owner/repo (the path may have a trailing /tree/branch
# segment which we strip). Accept .git suffix and trailing slashes.
_REPO_PATH_RE = re.compile(r"^/([\w.\-]+)/([\w.\-]+?)(?:\.git)?/?$")


def parse_repo_url(url: str) -> str | None:
    """Map a URL to the `owner/repo` (or `codeberg.org/owner/repo`) form used
    by `homelabsage.github`. Returns None when the URL isn't a supported
    forge URL.

    Examples
    --------
    >>> parse_repo_url("https://github.com/openclaw/openclaw")
    'openclaw/openclaw'
    >>> parse_repo_url("https://codeberg.org/forgejo/forgejo")
    'codeberg.org/forgejo/forgejo'
    >>> parse_repo_url("https://github.com/openclaw/openclaw/tree/main")
    'openclaw/openclaw'
    >>> parse_repo_url("https://example.com/foo")
    None
    """
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    # Strip optional `/tree/branch`, `/releases`, `/commits/...` tails so
    # `parse_repo_url('https://github.com/owner/repo/releases/tag/v1')`
    # still works.
    path = parsed.path
    for marker in ("/tree/", "/releases", "/commits", "/blob/", "/issues",
                   "/pull/", "/wiki"):
        idx = path.find(marker)
        if idx != -1:
            path = path[:idx]
            break
    if not path.endswith("/"):
        path += "/"
    path = path.rstrip("/") + "/"
    # Final pass: strip the trailing slash so the regex anchor matches.
    path_for_re = path.rstrip("/") + "/" if path != "/" else path
    m = _REPO_PATH_RE.match(path_for_re.rstrip("/"))
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if host == "github.com":
        return f"{owner}/{repo}"
    if host == "codeberg.org":
        return f"codeberg.org/{owner}/{repo}"
    return None


# ─── Docker Hub URL parser + analyser ───────────────────────────────────


# `hub.docker.com/r/owner/image` for namespaced images;
# `hub.docker.com/_/image` for the official "library/" namespace.
_HUB_NAMESPACED_RE = re.compile(r"^/r/([\w.\-]+)/([\w.\-]+?)/?$")
_HUB_LIBRARY_RE = re.compile(r"^/_/([\w.\-]+)/?$")


def parse_dockerhub_url(url: str) -> str | None:
    """Map a Docker Hub URL to the `owner/image` slug `find_alternatives` expects.

    Examples
    --------
    >>> parse_dockerhub_url("https://hub.docker.com/r/linuxserver/plex")
    'linuxserver/plex'
    >>> parse_dockerhub_url("https://hub.docker.com/_/nginx")
    'library/nginx'
    >>> parse_dockerhub_url("https://hub.docker.com/r/linuxserver/plex/tags")
    'linuxserver/plex'
    >>> parse_dockerhub_url("https://github.com/foo/bar")  # not Hub
    None
    """
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    host = (parsed.netloc or "").lower().removeprefix("www.")
    if host != "hub.docker.com":
        return None
    # Strip the navigation tail Docker Hub appends — /tags, /general, etc.
    path = parsed.path
    for marker in ("/tags", "/general", "/builds", "/dockerfile"):
        idx = path.find(marker)
        if idx != -1:
            path = path[:idx]
            break
    if (m := _HUB_NAMESPACED_RE.match(path)) is not None:
        return f"{m.group(1)}/{m.group(2)}"
    if (m := _HUB_LIBRARY_RE.match(path)) is not None:
        return f"library/{m.group(1)}"
    return None


async def _dockerhub_repo_meta(slug: str) -> dict | None:
    """Fetch `hub.docker.com/v2/repositories/{slug}` for description + stats."""
    url = f"https://hub.docker.com/v2/repositories/{slug}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return None
            return r.json()
    except httpx.HTTPError as e:
        log.debug("dockerhub repo meta %s failed: %s", slug, e)
        return None


async def analyse_dockerhub_url(
    cfg: Config,
    url: str,
    *,
    current_version: str = "",
) -> AnalyzedUpdate | None:
    """Treat a Docker Hub URL as a one-shot Update.

    The "new_version" we point the LLM at is the registry's *latest* tag
    (whatever it currently serves). We also attach `find_alternatives`
    context so the prompt can suggest a better-maintained sibling.
    """
    slug = parse_dockerhub_url(url)
    if slug is None:
        return None

    meta = await _dockerhub_repo_meta(slug)
    description = (meta or {}).get("description") or ""
    full_description = (meta or {}).get("full_description") or ""

    info = await dockerhub_tag_info(slug, "latest")
    new_tag = info.tag if info else "latest"
    new_version = new_tag

    ctx: dict = {
        "image": f"{slug}:{new_tag}",
        "registry": "docker.io",
        "registry_slug": slug,
        "_note_keywords": [slug, slug.split("/")[-1]],
    }
    if info and info.pushed_at:
        ctx["remote_pushed_at"] = info.pushed_at.isoformat()
    if description:
        ctx["dockerhub_description"] = description

    # Best-effort alternative lookup — same logic the docker plugin uses.
    try:
        alts = await find_alternatives(f"{slug}:{new_tag}", description=description)
        if alts.candidates:
            ctx["alternatives"] = [a.to_context() for a in alts.candidates]
    except Exception as e:
        log.debug("find_alternatives failed for %s: %s", slug, e)

    update = Update(
        source="analyse_url",
        subject=slug,
        current_version=current_version or "(unknown)",
        new_version=new_version,
        release_url=f"https://hub.docker.com/r/{slug}/tags",
        release_notes=(full_description or "")[:8000],
        context=ctx,
    )

    analyzed = AnalyzedUpdate(update=update)
    llm = LLMClient(get_active_llm_config(cfg))
    if not llm.is_enabled():
        return analyzed
    notes = NotesProvider(
        notes_dir=cfg.notes.notes_dir or None,
        extra_docs=cfg.notes.extra_docs,
        max_chars=cfg.notes.max_chars,
    )
    notes_ctx = notes.context_for(slug, keywords=[slug, slug.split("/")[-1]])
    analyzed.analysis = await llm.analyze(update, notes=notes_ctx)
    return analyzed


# ─── unified entry point ────────────────────────────────────────────────


async def analyse_url(
    cfg: Config,
    url: str,
    *,
    current_version: str = "",
) -> AnalyzedUpdate | None:
    """Dispatch by URL shape — repo URL → analyse_repo_url, Hub URL → Hub variant.

    Returns None when the URL doesn't match any supported shape. The CLI
    `homelabsage analyse <url>` calls this single function so the user
    doesn't have to know which path their URL goes through.
    """
    if parse_repo_url(url) is not None:
        return await analyse_repo_url(cfg, url, current_version=current_version)
    if parse_dockerhub_url(url) is not None:
        return await analyse_dockerhub_url(cfg, url, current_version=current_version)
    return None


async def analyse_repo_url(
    cfg: Config,
    url: str,
    *,
    current_version: str = "",
) -> AnalyzedUpdate | None:
    """Treat a repo URL as a one-shot Update and run the analyzer on it.

    `current_version` is whatever the user runs locally (or empty if they
    don't know). Empty currents still produce a useful analysis — the LLM
    treats them as "new install" and skips breaking-change reasoning.
    """
    repo = parse_repo_url(url)
    if repo is None:
        return None
    release = await latest_release(repo)
    if not release:
        log.warning("no release found for %s", repo)
        # Synthesise an Update with no release — the analyzer can still
        # comment on repo_health.
        release = {"tag_name": "", "html_url": url, "body": ""}

    ctx: dict = {"repo": repo, "_note_keywords": [repo, repo.split("/")[-1]]}
    try:
        meta = await repo_metadata(repo)
        health = classify_repo_health(meta)
        if health.get("status") != "unknown":
            ctx["repo_health"] = health
    except Exception as e:
        log.debug("repo_metadata failed for %s: %s", repo, e)

    update = Update(
        source="analyse_url",
        subject=repo,
        current_version=current_version or "(unknown)",
        new_version=(release.get("tag_name") or "(none)").lstrip("v"),
        release_url=release.get("html_url") or url,
        release_notes=release.get("body") or "",
        context=ctx,
    )

    analyzed = AnalyzedUpdate(update=update)

    llm = LLMClient(get_active_llm_config(cfg))
    if not llm.is_enabled():
        # Without an LLM there's no analysis to produce, but the user still
        # gets the structured Update back.
        return analyzed

    notes = NotesProvider(
        notes_dir=cfg.notes.notes_dir or None,
        extra_docs=cfg.notes.extra_docs,
        max_chars=cfg.notes.max_chars,
    )
    notes_ctx = notes.context_for(repo, keywords=[repo, repo.split("/")[-1]])
    analysis: Analysis | None = await llm.analyze(update, notes=notes_ctx)
    analyzed.analysis = analysis
    return analyzed
