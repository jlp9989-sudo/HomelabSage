"""On-demand URL analysis — paste a link, get the same analysis the engine
would produce on a scheduled scan.

v1 supports GitHub + Codeberg repo URLs. The full URL classifier (Docker
Hub, news articles, HuggingFace model cards) is roadmap'd separately;
each sub-case is its own engineering chunk and they share only this
top-level dispatch entrypoint.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from .config import Config, get_active_llm_config
from .github import classify_repo_health, latest_release, repo_metadata
from .llm import LLMClient
from .models import Analysis, AnalyzedUpdate, Update
from .notes import NotesProvider

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
