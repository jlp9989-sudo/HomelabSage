"""Liveness probe — always 200, never auth-gated.

Kept in its own file so the Basic Auth middleware can hard-allow `/healthz`
by path without needing to know which route group owns it.
"""

from __future__ import annotations

from fastapi import FastAPI


def register_health_routes(app: FastAPI) -> None:
    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}
