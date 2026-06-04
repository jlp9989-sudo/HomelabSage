"""HTTP Basic Auth + Bearer token middleware.

Two auth modes share the same middleware:
  - **Basic**: `Authorization: Basic base64(user:pass)`
  - **Bearer**: `Authorization: Bearer <api_key>` matched against
    `cfg.api_keys` (additive list)

Auth-bypass paths live as exact strings AND prefix tuples in a single
shared `AUTH_BYPASS_*` constant so a typo can't accidentally open a
path. `tests/test_auth_bypass.py` pins the exact set.
"""

from __future__ import annotations

import base64
import secrets

from fastapi import FastAPI, Request, Response

from ..config import WebAuthConfig

# Exact-match auth-bypass paths. `/healthz` for Docker / Kuma probes,
# `/api/stack-health` + `/metrics` for dashboard scrapers.
AUTH_BYPASS_EXACT: frozenset[str] = frozenset({
    "/healthz",
    "/api/stack-health",
    "/api/doctor",
    "/metrics",
    # GitHub webhooks don't send Authorization headers. We bypass
    # Basic Auth here BUT only because the endpoint itself enforces
    # `GITHUB_RELEASE_WEBHOOK_SECRET` HMAC-SHA256 — see
    # `routes_updates.py::api_inbox_github_release` which refuses the
    # request when the env var is unset.
    "/api/webhook/github-release",
})

# Prefix-bypass paths. `/widget/*` for Homepage / Homarr scrapers.
AUTH_BYPASS_PREFIX: tuple[str, ...] = (
    "/widget/",
)


def _is_bypassed(path: str) -> bool:
    if path in AUTH_BYPASS_EXACT:
        return True
    return any(path.startswith(p) for p in AUTH_BYPASS_PREFIX)


def attach_basic_auth(app: FastAPI, cfg: WebAuthConfig) -> None:
    """Install the Basic Auth + Bearer middleware on `app`.

    Caller decides when to invoke (typically guarded by
    `if cfg.enabled and cfg.password`).
    """
    expected_user = cfg.username.encode()
    expected_pass = cfg.password.encode()
    api_keys = [k.encode() for k in (cfg.api_keys or []) if k]

    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        path = request.url.path
        if _is_bypassed(path):
            return await call_next(request)
        header = request.headers.get("authorization", "")
        # Bearer first — most agents will use it.
        if header.startswith("Bearer "):
            presented = header[7:].strip().encode()
            for key in api_keys:
                if secrets.compare_digest(presented, key):
                    return await call_next(request)
        elif header.startswith("Basic "):
            try:
                creds = base64.b64decode(header[6:]).decode()
                user, _, pwd = creds.partition(":")
                if (
                    secrets.compare_digest(user.encode(), expected_user)
                    and secrets.compare_digest(pwd.encode(), expected_pass)
                ):
                    return await call_next(request)
            except Exception:
                pass
        return Response(
            status_code=401,
            content="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="HomelabSage"'},
        )
