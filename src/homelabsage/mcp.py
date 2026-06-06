"""Model Context Protocol (MCP) endpoint.

Exposes HomelabSage state as MCP tools so the user's AI assistant
(Claude Code, Cursor, ChatGPT desktop) can read pending updates,
diagnostics, watched repos, and apply/dismiss decisions without
scraping the web UI.

Wire format: JSON-RPC 2.0 over HTTP (single POST endpoint at
`/mcp`). We deliberately implement the minimal subset of the MCP
HTTP transport rather than depend on the `mcp` PyPI package — that
package is heavy and its API churns rapidly. The two RPC methods
clients actually use are:

  - `tools/list`   → returns the list of tool descriptors
  - `tools/call`   → invokes a tool by name with JSON params

Both are JSON-RPC 2.0 calls. Streaming / notifications are out of
scope: every HomelabSage operation is synchronous and returns in
under a second.

Security: this endpoint is behind the same HTTP Basic Auth as the
rest of the web UI. There is no anonymous MCP access; if you want
to expose MCP to a local agent without auth, run HomelabSage
without `web.auth.enabled` (the standard "trust your LAN" stance).

Structure since v0.11.1:
  - `mcp.py` (this file) — JSON-RPC envelope + dispatch + route
    registration. ~150 LOC.
  - `mcp_tools.py` — 50 `_tool_*` impls + the JSON-Schema TOOLS
    descriptor + helpers used only by impls. ~1700 LOC, mechanical
    to navigate because tools cluster by section comments and the
    descriptor lives right next to the impl table.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import __version__
from .config import Config
from .db import Database
from .mcp_tools import TOOLS
from .outputs._errlog import safe_error

log = logging.getLogger(__name__)


# ─── JSON-RPC 2.0 envelope ────────────────────────────────────────────────


def _jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _jsonrpc_result(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def dispatch(cfg: Config, db: Database, body: dict) -> dict:
    """Single-request JSON-RPC dispatch. Pure function, fully unit-testable."""
    req_id = body.get("id")
    if body.get("jsonrpc") != "2.0":
        return _jsonrpc_error(req_id, -32600, "jsonrpc must be '2.0'")
    method = body.get("method")
    params = body.get("params") or {}

    if method == "tools/list":
        return _jsonrpc_result(req_id, {
            "tools": [
                {
                    "name": name,
                    "description": spec["description"],
                    "inputSchema": spec["params_schema"],
                }
                for name, spec in TOOLS.items()
            ],
        })

    if method == "tools/call":
        tool_name = params.get("name")
        tool_args = params.get("arguments") or {}
        spec = TOOLS.get(tool_name or "")
        if spec is None:
            return _jsonrpc_error(req_id, -32601, f"unknown tool: {tool_name!r}")
        impl: Callable[[Config, Database, dict], Any] = spec["impl"]
        try:
            result = impl(cfg, db, tool_args)
        except ValueError as e:
            # ValueError is the conventional "bad argument" path — its
            # message is part of the tool contract (e.g. "limit must be
            # 1..500"), surface it. ValueError messages never carry
            # secrets in our codebase by audit.
            return _jsonrpc_error(req_id, -32602, str(e))
        except Exception as e:
            # C5: never echo `str(e)` to the client. Exception messages
            # routinely carry secrets (sqlite paths, HTTP URLs with
            # tokens, SSH host strings, container env vars). Log the
            # full trace server-side, return only the type to the caller.
            log.exception("MCP tool %s crashed", tool_name)
            return _jsonrpc_error(
                req_id, -32603,
                f"tool {tool_name!r} failed ({safe_error(e)}) — see server logs",
            )
        # MCP convention: wrap structured results in `content[]` with text/json
        # so generic clients can render them uniformly.
        return _jsonrpc_result(req_id, {
            "content": [{"type": "json", "data": result}],
            "structuredContent": result,
        })

    return _jsonrpc_error(req_id, -32601, f"unknown method: {method!r}")


# ─── route registration ───────────────────────────────────────────────────


def register_mcp_routes(app: FastAPI, cfg: Config, db: Database) -> None:
    """Mount `POST /mcp` and `GET /mcp/info` on the FastAPI app.

    `/mcp/info` is a tiny GET endpoint that returns the tool list as
    plain JSON (no JSON-RPC envelope). Useful for humans inspecting
    the server with `curl`; agents always use POST /mcp.
    """

    @app.get("/mcp/info")
    async def mcp_info() -> JSONResponse:
        return JSONResponse({
            "name": "homelabsage",
            "version": __version__,
            "protocol": "mcp/json-rpc-2.0",
            "tools": list(TOOLS.keys()),
        })

    @app.post("/mcp")
    async def mcp_post(body: dict) -> JSONResponse:
        if not isinstance(body, dict):
            raise HTTPException(400, "MCP body must be a JSON object")
        # C2: many tools do blocking I/O (docker SDK images.list, TLS
        # probe loops, compose-graph walks, sync sqlite). Calling
        # `dispatch` directly on the event loop froze the entire web
        # UI + scheduler for the duration of a slow tool. Offload to
        # a worker thread so a hung tool only ties up one thread.
        result = await asyncio.to_thread(dispatch, cfg, db, body)
        return JSONResponse(result)
