"""On-demand URL analysis — paste a link, get the same analysis the engine
would produce on a scheduled scan.

Supported URL shapes:
  - `https://github.com/owner/repo`               (+ /tree/, /releases, …)
  - `https://codeberg.org/owner/repo`             (Forgejo-compatible)
  - `https://hub.docker.com/r/owner/image`        (Docker Hub user/org image)
  - `https://hub.docker.com/_/library_image`      (Docker Hub library image)
  - `https://huggingface.co/owner/model`          (model card + fit check)
  - any other `http(s)://` URL → treated as a news/blog/changelog article
    and extracted with `trafilatura` (falls back to a naive HTML strip
    when the library isn't installed).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
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


# ─── HuggingFace model card analyser ───────────────────────────────────


_HF_PATH_RE = re.compile(r"^/([\w.\-]+)/([\w.\-]+?)(?:/.*)?$")


def parse_huggingface_url(url: str) -> str | None:
    """Map an HF model URL to `owner/model`. None for non-HF URLs.

    Examples
    --------
    >>> parse_huggingface_url("https://huggingface.co/Qwen/Qwen3.6-35B")
    'Qwen/Qwen3.6-35B'
    >>> parse_huggingface_url("https://huggingface.co/Qwen/Qwen3.6-35B/tree/main")
    'Qwen/Qwen3.6-35B'
    >>> parse_huggingface_url("https://github.com/foo/bar")  # not HF
    None
    """
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    host = (parsed.netloc or "").lower().removeprefix("www.")
    if host != "huggingface.co":
        return None
    m = _HF_PATH_RE.match(parsed.path)
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}"


async def _fetch_hf_model_meta(slug: str) -> dict | None:
    """`huggingface.co/api/models/<slug>` — public model metadata."""
    api = f"https://huggingface.co/api/models/{slug}"
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            r = await client.get(api, headers={"Accept": "application/json"})
            if r.status_code != 200:
                return None
            return r.json()
    except httpx.HTTPError as e:
        log.debug("HF model meta %s failed: %s", slug, e)
        return None


# Crude VRAM/RAM estimator: parameters * bytes_per_param. Quantised
# models are detected via filename suffixes (Q4_K_M, Q5_K_S, fp8, mxfp4,
# nf4, etc) and the bytes-per-param drops accordingly. Pure heuristic;
# the prompt mentions "approximate" in the verdict.
_QUANT_BYTES: dict[str, float] = {
    "fp32": 4.0, "f32": 4.0,
    "bf16": 2.0, "fp16": 2.0, "f16": 2.0, "half": 2.0,
    "fp8": 1.0, "mxfp4": 0.6,
    "int8": 1.0, "q8": 1.0,
    "int4": 0.5, "q4": 0.5, "q5": 0.65, "q3": 0.4, "q2": 0.3,
    "nf4": 0.5, "awq": 0.5, "gptq": 0.5,
}


def _bytes_per_param(filenames: list[str], reported_quant: str | None) -> float:
    """Best-guess bytes-per-param from siblings + reported quantisation.

    Prefers the explicit `quant` field on the HF API when available,
    falls back to scanning sibling filenames for known suffixes, defaults
    to bf16 (2.0).
    """
    if reported_quant:
        key = reported_quant.lower()
        for q, b in _QUANT_BYTES.items():
            if q in key:
                return b
    blob = " ".join(filenames).lower()
    for q, b in sorted(_QUANT_BYTES.items(), key=lambda kv: -len(kv[0])):
        if q in blob:
            return b
    return 2.0  # bf16/fp16 default


def estimate_vram_gib(params_billion: float, bytes_per_param: float) -> float:
    """params_billion * bytes_per_param / 1024^3 — plus 15% KV-cache headroom."""
    raw_gib = params_billion * 1e9 * bytes_per_param / (1024**3)
    return round(raw_gib * 1.15, 1)


def _extract_params_billion(meta: dict) -> float | None:
    """HF doesn't always report params. Try `safetensors.parameters`,
    fall back to size in tags / config."""
    safe = (meta.get("safetensors") or {}).get("parameters")
    if isinstance(safe, dict):
        total = sum(int(v) for v in safe.values() if isinstance(v, int))
        if total > 0:
            return round(total / 1e9, 2)
    # Some model cards encode it as a top-level integer
    direct = meta.get("config", {}).get("num_parameters") if isinstance(meta.get("config"), dict) else None
    if isinstance(direct, int) and direct > 0:
        return round(direct / 1e9, 2)
    return None


def _read_system_vram_gib(notes_dir: str | None) -> float | None:
    """Read `system.md` (written by the curator) for a VRAM line.

    Format we look for: `- GPU: ... (NN GiB VRAM)` — the curator's
    system probe writes this. None when notes_dir is missing or the
    file doesn't carry the pattern.
    """
    if not notes_dir:
        return None
    path = Path(notes_dir) / "system.md"
    if not path.exists():
        return None
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*GiB\s*VRAM", body, re.IGNORECASE)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


async def analyse_huggingface_url(
    cfg: Config,
    url: str,
    *,
    current_version: str = "",
) -> AnalyzedUpdate | None:
    """Pull HF model card, estimate VRAM, cross with system.md if available."""
    slug = parse_huggingface_url(url)
    if slug is None:
        return None
    meta = await _fetch_hf_model_meta(slug)
    if meta is None:
        return None
    siblings = [
        s.get("rfilename") or s.get("filename") or ""
        for s in (meta.get("siblings") or []) if isinstance(s, dict)
    ]
    quant_str = meta.get("safetensors", {}).get("total") if isinstance(meta.get("safetensors"), dict) else None
    bytes_per_param = _bytes_per_param(siblings, str(quant_str) if quant_str else None)
    params_b = _extract_params_billion(meta)
    vram_estimate_gib: float | None = None
    if params_b:
        vram_estimate_gib = estimate_vram_gib(params_b, bytes_per_param)

    system_vram = _read_system_vram_gib(cfg.notes.notes_dir or None)
    fit_verdict = "unknown"
    if vram_estimate_gib is not None and system_vram is not None:
        if vram_estimate_gib > system_vram:
            fit_verdict = "wont_fit"
        elif vram_estimate_gib > system_vram * 0.9:
            fit_verdict = "tight"
        else:
            fit_verdict = "fits"

    ctx: dict = {
        "hf_slug": slug,
        "params_billion": params_b,
        "bytes_per_param": bytes_per_param,
        "vram_estimate_gib": vram_estimate_gib,
        "system_vram_gib": system_vram,
        "fit_verdict": fit_verdict,
        "tags": meta.get("tags") or [],
        "library_name": meta.get("library_name"),
        "_note_keywords": [slug, slug.split("/")[-1]],
    }
    summary = (
        f"HuggingFace model `{slug}`. "
        f"Params: {params_b or 'unknown'}B. "
        f"VRAM estimate: {vram_estimate_gib or '?'} GiB "
        f"({fit_verdict} vs reported system VRAM {system_vram or 'unknown'} GiB)."
    )
    update = Update(
        source="analyse_url",
        subject=f"hf:{slug}",
        current_version=current_version or "(unknown)",
        new_version="(model card)",
        release_url=url,
        release_notes=summary,
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


# ─── news / blog / changelog URL analyser ──────────────────────────────


# Anchored on URLs that survive a real fetch. We treat anything http(s) as
# potentially analysable; the LLM downgrades on noise. Excluded: localhost,
# raw IPs, file:// — those are out of scope for "I read an article about X".
_LIKELY_ARTICLE_HOSTS_BLOCKLIST = {"localhost", "127.0.0.1", "0.0.0.0"}


def looks_like_article_url(url: str) -> bool:
    """Liberal guard. Reject clearly-non-article shapes; accept the rest."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.netloc or "").lower().split(":")[0].removeprefix("www.")
    return not (not host or host in _LIKELY_ARTICLE_HOSTS_BLOCKLIST)


def _strip_html_fallback(html: str) -> str:
    """Naive HTML stripper for when `trafilatura` isn't installed.

    Removes `<script>` + `<style>` blocks entirely, then collapses all
    tags to spaces, then squeezes whitespace. The output is noisier than
    trafilatura's but lets the analyzer still see paragraph-level text.
    """
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                  flags=re.DOTALL | re.IGNORECASE)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body)
    return body.strip()


async def _fetch_article(url: str, *, timeout: float = 20.0) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 HomelabSage/analyse-url",
            })
            if r.status_code != 200:
                return None
            return r.text
    except httpx.HTTPError as e:
        log.debug("article fetch %s failed: %s", url, e)
        return None


def _extract_article_text(html: str, *, url: str) -> str:
    """Try trafilatura first; fall back to the naive strip."""
    try:
        import trafilatura  # type: ignore[import-not-found]
    except ImportError:
        return _strip_html_fallback(html)
    try:
        extracted = trafilatura.extract(
            html, url=url, include_comments=False, include_tables=False,
        )
    except Exception as e:
        log.debug("trafilatura.extract failed: %s", e)
        extracted = None
    return (extracted or _strip_html_fallback(html)).strip()


async def analyse_article_url(
    cfg: Config,
    url: str,
    *,
    current_version: str = "",
) -> AnalyzedUpdate | None:
    """Treat an arbitrary URL as a news/blog/changelog article.

    Pulls + extracts main content, then runs the analyzer prompt with the
    user's notes injected. Returns None when the page can't be fetched or
    extraction produced an empty body — we'd rather say "nothing to
    analyse" than feed the LLM a blob of nav HTML.
    """
    if not looks_like_article_url(url):
        return None
    html = await _fetch_article(url)
    if not html:
        return None
    text = _extract_article_text(html, url=url)
    # Reject excessively-thin extractions — those almost always indicate
    # a JS-rendered page, a paywall, or a cookie wall.
    if len(text) < 200:
        log.info("article extraction too short (%d chars) — skipping", len(text))
        return None

    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().removeprefix("www.")
    subject = f"article:{host}"
    keywords = [host] + [t for t in re.split(r"\W+", text[:300]) if len(t) > 3][:8]

    update = Update(
        source="analyse_url",
        subject=subject,
        current_version=current_version or "(unknown)",
        new_version="(article)",
        release_url=url,
        # Cap at 8 KB — the analyzer never needs the entire article,
        # the first few KB carry the lead and the bullets.
        release_notes=text[:8000],
        context={"_note_keywords": keywords, "article_host": host},
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
    notes_ctx = notes.context_for(subject, keywords=keywords)
    analyzed.analysis = await llm.analyze(update, notes=notes_ctx)
    return analyzed


# ─── unified entry point ────────────────────────────────────────────────


async def analyse_url(
    cfg: Config,
    url: str,
    *,
    current_version: str = "",
) -> AnalyzedUpdate | None:
    """Dispatch by URL shape — repo → Hub → article.

    Returns None when the URL doesn't match any supported shape OR when
    the article path returns no usable content (paywall, JS-only page).
    The CLI `homelabsage analyse <url>` calls this single function so
    the user doesn't have to know which path their URL goes through.
    """
    if parse_repo_url(url) is not None:
        return await analyse_repo_url(cfg, url, current_version=current_version)
    if parse_dockerhub_url(url) is not None:
        return await analyse_dockerhub_url(cfg, url, current_version=current_version)
    if parse_huggingface_url(url) is not None:
        return await analyse_huggingface_url(cfg, url, current_version=current_version)
    if looks_like_article_url(url):
        return await analyse_article_url(cfg, url, current_version=current_version)
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
