"""LLM-backend health probe.

When the configured LLM endpoint is down (Halo asleep, Ollama
restarted, Groq's API in a degraded state), a HomelabSage scan still
fires every plugin, pulls every registry, and then dies on the
analyser call — wasting the API budget and the registry rate-limit.

This module is a small pre-flight: probe the endpoint with a cheap
GET (`/v1/models` for OpenAI-compat, `/health` for known providers,
falling back to a HEAD on the base URL). On failure the engine can
log + skip the scan rather than barrel ahead.

The probe is best-effort and SHORT — 3s default timeout. We never
want this gate to add minutes to a normal scan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)


@dataclass
class LLMHealth:
    """Probe verdict — `__bool__` returns `ok`."""

    ok: bool
    endpoint: str
    reason: str
    status_code: int | None = None

    def __bool__(self) -> bool:
        return self.ok


def _candidate_urls(endpoint: str) -> list[str]:
    """Order of probe URLs to try, cheapest first.

    For an OpenAI-compat endpoint, `/v1/models` is the canonical
    cheap health-check (returns 200 with a tiny JSON list). Some
    self-hosted servers expose `/health` or `/healthz` directly on
    the root. We try `/v1/models` first, then fall back.
    """
    base = endpoint.rstrip("/")
    if not base:
        return []
    # Strip a trailing /v1 or /chat/completions so we can probe siblings.
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")].rstrip("/")
    out: list[str] = []
    if "/v1" in base or "/v2" in base:
        out.append(base + "/models")
    else:
        out.append(base + "/v1/models")
    # Common probe endpoints (llama-server, vLLM, Ollama)
    # Compute scheme://host:port without any path for these.
    p = urlparse(base if "://" in base else "http://" + base)
    root = f"{p.scheme}://{p.netloc}"
    out.append(root + "/health")
    out.append(root + "/healthz")
    return out


def probe(
    endpoint: str,
    *,
    api_key: str = "",
    timeout: float = 3.0,
) -> LLMHealth:
    """Probe the LLM endpoint. Never raises.

    Empty endpoint → `ok=True` with reason "no endpoint configured"
    (treat as healthy: the engine handles missing config elsewhere
    and skipping the scan for that reason would mask the real issue).
    """
    if not endpoint or not endpoint.strip():
        return LLMHealth(ok=True, endpoint="", reason="no endpoint configured")
    urls = _candidate_urls(endpoint.strip())
    if not urls:
        return LLMHealth(
            ok=False, endpoint=endpoint, reason="couldn't derive probe URL",
        )
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    last_reason = ""
    last_status: int | None = None
    try:
        with httpx.Client(timeout=timeout, headers=headers) as client:
            for u in urls:
                try:
                    r = client.get(u)
                except httpx.HTTPError as e:
                    last_reason = f"{type(e).__name__}: {e}"
                    continue
                last_status = r.status_code
                # 200 / 204 → healthy; 401/403 also healthy (endpoint
                # is up, auth is the user's problem).
                if r.status_code in (200, 204, 401, 403):
                    return LLMHealth(
                        ok=True, endpoint=endpoint,
                        reason=f"{u} → {r.status_code}",
                        status_code=r.status_code,
                    )
                last_reason = f"{u} → {r.status_code}"
    except (httpx.HTTPError, OSError) as e:
        last_reason = f"client error: {type(e).__name__}: {e}"
    return LLMHealth(
        ok=False, endpoint=endpoint,
        reason=last_reason or "unreachable",
        status_code=last_status,
    )


__all__ = ["LLMHealth", "probe"]
