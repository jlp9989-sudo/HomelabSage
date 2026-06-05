"""Liveness probe — always 200, never auth-gated.

Kept in its own file so the Basic Auth middleware can hard-allow `/healthz`
by path without needing to know which route group owns it.
"""

from __future__ import annotations

from fastapi import FastAPI

from .. import __version__


def register_health_routes(app: FastAPI) -> None:
    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/version")
    async def api_version() -> dict:
        """Version + feature flag map for downstream consumers.

        Mirrors the MCP `version` tool. Auth-bypassed so Homepage /
        Kuma probes can read it directly. The feature flag map lets
        a dashboard widget conditionally render new fields based on
        whether the server supports them.
        """
        return {
            "version": __version__,
            "features": {
                "doctor": True,
                "audit_history": True,
                "snooze": True,
                "audit_alerts": True,
            },
        }
