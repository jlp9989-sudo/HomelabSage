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
    api_keys: list[str] = Field(
        default_factory=list,
        description=(
            "Bearer-token alternative to Basic Auth. Each entry is a "
            "shared secret; the client sends `Authorization: Bearer "
            "<key>` and bypasses the Basic Auth challenge. Treat each "
            "entry like a password — rotation = edit + restart."
        ),
    )


class WebConfig(BaseModel):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8000
    auth: WebAuthConfig = Field(default_factory=WebAuthConfig)
    preflight_gate: bool = Field(
        False,
        description=(
            "Show a confirmation wall when applying an update whose "
            "analysis carries `breaking_changes`. Off by default to keep "
            "the one-click flow; on for installs where the user wants a "
            "circuit-breaker before destructive actions."
        ),
    )
