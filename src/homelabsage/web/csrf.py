"""Origin/Referer-based CSRF mitigation.

Browsers attach Basic Auth credentials automatically to every request that
targets the matching realm. Without protection, a logged-in user who visits
an attacker-controlled page can have their browser silently POST to our
state-changing endpoints (settings PATCH, notes save, scheduler trigger).

The smallest-scope mitigation is to verify the request's intent against the
`Origin` (or, as a fallback, `Referer`) header. Modern browsers always
attach `Origin` to POST/PATCH/DELETE — it cannot be set by JavaScript on
cross-site requests. So:

  - If the request is GET/HEAD/OPTIONS → allow. These are nullipotent by
    convention; we never change state on them.
  - If `Origin` is present → its scheme://host[:port] must match the
    request's effective host. Otherwise reject with 403.
  - If `Origin` is absent but `Referer` is present → same check against
    the Referer's host. (Some old browsers / privacy proxies strip Origin.)
  - If both are absent → allow. Typical of curl / programmatic clients;
    those already cleared Basic Auth, and CSRF requires a browser to
    automate the request.

This is the same protection mechanism used by Django's CSRF_USE_SESSIONS=
False mode and FastAPI-best-practices guides. It does NOT replace a proper
token-based CSRF system, but it closes the well-known browser-automated
attack vector against the current Basic-Auth setup, which is the realistic
threat for a homelab tool.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import FastAPI, Request, Response

log = logging.getLogger(__name__)

# Methods that may not change state. We trust them implicitly.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _request_origin(request: Request) -> str:
    """Reconstruct the request's effective `scheme://host[:port]`.

    Honours `X-Forwarded-Proto` when set by a trusted reverse proxy — a
    typical homelab deployment puts HomelabSage behind Caddy/Traefik/nginx
    that terminates TLS, so the inner request.url.scheme is `http` even
    though the public URL is `https`. We trust X-Forwarded-Proto because
    if the proxy is compromised, the attacker has already won.
    """
    host = request.headers.get("host", "")
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{scheme}://{host}" if host else ""


def _header_origin(header_value: str) -> str:
    """Normalise an Origin or Referer header to `scheme://host[:port]`."""
    if not header_value:
        return ""
    parsed = urlparse(header_value)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def attach_csrf_guard(app: FastAPI) -> None:
    """Install the Origin/Referer-check middleware on `app`.

    Idempotent — calling twice adds two layers (harmless but pointless).
    Caller should attach this ONCE in `create_app`, before any other
    middleware that might wrap responses.
    """

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        if request.method in _SAFE_METHODS:
            return await call_next(request)
        # Health endpoint must stay open for unauthenticated probes that
        # may use HEAD/GET — but POST is conceivable for some healthchecks,
        # so we whitelist by path too.
        if request.url.path == "/healthz":
            return await call_next(request)

        expected = _request_origin(request)
        origin = _header_origin(request.headers.get("origin", ""))
        referer = _header_origin(request.headers.get("referer", ""))

        if origin:
            if origin != expected:
                log.warning(
                    "csrf: Origin mismatch (expected %r, got %r) on %s %s",
                    expected, origin, request.method, request.url.path,
                )
                return Response(status_code=403, content="cross-origin request rejected")
            return await call_next(request)
        if referer:
            if referer != expected:
                log.warning(
                    "csrf: Referer mismatch (expected %r, got %r) on %s %s",
                    expected, referer, request.method, request.url.path,
                )
                return Response(status_code=403, content="cross-origin request rejected")
            return await call_next(request)

        # No Origin AND no Referer: typical of curl / API clients. Basic Auth
        # already gated entry; a browser-driven CSRF would carry one of these.
        return await call_next(request)
