"""Web UI configuration (port, auth)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class WebAuthConfig(BaseModel):
    """HTTP Basic Auth for the web UI.

    Disabled by default so first-time users see the UI. Enable in any
    install that's bound to a non-loopback address. The `/healthz`
    endpoint is always unauthenticated (Docker healthcheck).
    """

    enabled: bool = False
    username: str = "admin"
    password: str = ""


class WebConfig(BaseModel):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8000
    auth: WebAuthConfig = Field(default_factory=WebAuthConfig)
