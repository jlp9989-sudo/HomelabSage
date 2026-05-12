"""HTTP Basic Auth middleware.

Single-user, single-realm, constant-time comparison. `/healthz` stays open so
Docker / Uptime Kuma / Healthchecks probes don't need credentials.
"""

from __future__ import annotations

import base64
import secrets

from fastapi import FastAPI, Request, Response

from ..config import WebAuthConfig


def attach_basic_auth(app: FastAPI, cfg: WebAuthConfig) -> None:
    """Install a Basic Auth middleware on `app`.

    Caller decides when to invoke (typically guarded by
    `if cfg.enabled and cfg.password`).
    """
    expected_user = cfg.username.encode()
    expected_pass = cfg.password.encode()

    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        # Health endpoint must stay open for Docker / Kuma probes.
        if request.url.path == "/healthz":
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
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
