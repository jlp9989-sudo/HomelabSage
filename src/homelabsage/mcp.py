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
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import __version__
from .config import Config
from .db import Database
from .diagnostics import diagnose_containers, summarise
from .models import UpdateStatus

log = logging.getLogger(__name__)


# ─── tool implementations ─────────────────────────────────────────────────


def _tool_health(_cfg: Config, _db: Database, _params: dict) -> dict:
    """Probe: returns version + the names of every enabled plugin/output.

    Most-used tool by agents — they call it first to find out whether
    HomelabSage is reachable and what surface area is configured.
    """
    enabled_sources = [
        name for name, attr in (
            ("docker", _cfg.sources.docker),
            ("homeassistant", _cfg.sources.homeassistant),
            ("fedora", _cfg.sources.fedora),
            ("github_watched", _cfg.sources.github_watched),
        ) if attr.enabled
    ]
    enabled_outputs = [
        name for name, attr in (
            ("notion", _cfg.outputs.notion),
            ("telegram", _cfg.outputs.telegram),
            ("discord", _cfg.outputs.discord),
            ("ntfy", _cfg.outputs.ntfy),
            ("gotify", _cfg.outputs.gotify),
        ) if attr.enabled
    ]
    return {
        "version": __version__,
        "enabled_sources": enabled_sources,
        "enabled_outputs": enabled_outputs,
        "parity_gate_enabled": _cfg.parity_gate.enabled,
        "digest_enabled": _cfg.digest.enabled,
    }


def _tool_list_updates(_cfg: Config, db: Database, params: dict) -> dict:
    """List analyzed updates (most-recent first).

    Params:
      status: optional UpdateStatus filter — "new" | "analyzed" |
              "applied" | "dismissed" | "failed".
      source: optional plugin id filter ("docker", "github_watched", …).
      limit:  default 50, max 500.
    """
    status_raw = params.get("status")
    source = params.get("source")
    limit = min(int(params.get("limit") or 50), 500)
    status = UpdateStatus(status_raw) if status_raw else None

    items = db.list(status=status, source=source, limit=limit)
    return {
        "count": len(items),
        "items": [_summarise_update(it) for it in items],
    }


def _tool_get_update(_cfg: Config, db: Database, params: dict) -> dict:
    """Return the full record for one update (including the LLM analysis)."""
    update_id = params.get("update_id")
    if not update_id:
        raise ValueError("update_id is required")
    item = db.get(update_id)
    if item is None:
        raise ValueError(f"no update with id={update_id!r}")
    return _full_update(item)


def _tool_set_update_status(_cfg: Config, db: Database, params: dict) -> dict:
    """Mark an update as applied or dismissed.

    The agent uses this after presenting the verdict to the user — the
    user says "ok apply" or "skip this one", the agent records the
    outcome so the next scan doesn't re-surface it.
    """
    update_id = params.get("update_id")
    status_raw = params.get("status")
    if not update_id or not status_raw:
        raise ValueError("update_id and status are required")
    status = UpdateStatus(status_raw)
    if db.get(update_id) is None:
        raise ValueError(f"no update with id={update_id!r}")
    db.set_status(update_id, status)
    return {"ok": True, "update_id": update_id, "status": status.value}


def _tool_list_diagnostics(cfg: Config, _db: Database, _params: dict) -> dict:
    """`What I see` — per-container verdict (same as `/api/diagnostics`).

    Cheap (no outbound HTTP). Useful for the agent to answer "what
    containers are even tracked?" before listing updates.
    """
    rows = diagnose_containers(cfg.sources.docker)
    return {
        "counts": summarise(rows),
        "rows": [d.to_dict() for d in rows],
    }


def _tool_list_watched(_cfg: Config, db: Database, params: dict) -> dict:
    """List opt-in GitHub/Codeberg repos the watched-repos plugin scans."""
    active_only = bool(params.get("active_only", True))
    rows = db.list_watched_repos(active_only=active_only)
    return {"count": len(rows), "items": rows}


def _tool_list_pending_dispatches(_cfg: Config, db: Database, _params: dict) -> dict:
    """Push notifications queued during a parity-gate window.

    Returns the same data the engine drains on the next ungated scan.
    Agents use this to surface "you'll get N pings the moment parity
    clears" warnings to the user.
    """
    rows = db.list_pending_dispatches()
    return {"count": len(rows), "items": rows}


# Tool registry — name → (description, params_schema, impl_fn).
# The schema follows JSON Schema draft-7 the way MCP clients expect.
TOOLS: dict[str, dict[str, Any]] = {
    "health": {
        "description": "Probe HomelabSage. Returns version and the names of every enabled source/output.",
        "params_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "impl": _tool_health,
    },
    "list_updates": {
        "description": "List analyzed updates, most-recent first. Filter by status or source.",
        "params_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["new", "analyzed", "applied", "dismissed", "failed"]},
                "source": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "additionalProperties": False,
        },
        "impl": _tool_list_updates,
    },
    "get_update": {
        "description": "Return full detail for one update_id (Update + Analysis + context).",
        "params_schema": {
            "type": "object",
            "properties": {"update_id": {"type": "string"}},
            "required": ["update_id"],
            "additionalProperties": False,
        },
        "impl": _tool_get_update,
    },
    "set_update_status": {
        "description": "Mark an update as applied, dismissed, or failed. Idempotent.",
        "params_schema": {
            "type": "object",
            "properties": {
                "update_id": {"type": "string"},
                "status": {"type": "string", "enum": ["new", "analyzed", "applied", "dismissed", "failed"]},
            },
            "required": ["update_id", "status"],
            "additionalProperties": False,
        },
        "impl": _tool_set_update_status,
    },
    "list_diagnostics": {
        "description": "Per-container verdict (tracked / floating_tag / no_repo / no_version / skipped_by_rule).",
        "params_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "impl": _tool_list_diagnostics,
    },
    "list_watched_repos": {
        "description": "List opt-in GitHub/Codeberg repos the watched-repos plugin scans.",
        "params_schema": {
            "type": "object",
            "properties": {"active_only": {"type": "boolean", "default": True}},
            "additionalProperties": False,
        },
        "impl": _tool_list_watched,
    },
    "list_pending_dispatches": {
        "description": "Push notifications queued by the parity-gate (waiting for the gate to clear).",
        "params_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "impl": _tool_list_pending_dispatches,
    },
}


# ─── helpers ──────────────────────────────────────────────────────────────


def _summarise_update(item) -> dict:
    """Compact one AnalyzedUpdate for list endpoints (no release_notes / context)."""
    u = item.update
    a = item.analysis
    return {
        "id": item.id,
        "source": u.source,
        "subject": u.subject,
        "current_version": u.current_version,
        "new_version": u.new_version,
        "release_url": u.release_url,
        "severity": a.severity.value if a else None,
        "summary": a.summary if a else None,
        "action_required": a.action_required if a else None,
        "status": item.status.value,
        "detected_at": item.detected_at.isoformat(),
    }


def _full_update(item) -> dict:
    """Detailed version — includes context block and full Analysis."""
    base = _summarise_update(item)
    u = item.update
    a = item.analysis
    base["release_notes"] = (u.release_notes or "")[:8000]
    base["context"] = u.context
    if a:
        base["analysis"] = {
            "severity": a.severity.value,
            "summary": a.summary,
            "breaking_changes": a.breaking_changes,
            "config_obsolete": a.config_obsolete,
            "new_features_relevant": a.new_features_relevant,
            "action_required": a.action_required,
            "recommended_action": a.recommended_action,
        }
    return base


# ─── JSON-RPC dispatcher ──────────────────────────────────────────────────


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
            return _jsonrpc_error(req_id, -32602, str(e))
        except Exception as e:
            log.exception("MCP tool %s crashed", tool_name)
            return _jsonrpc_error(req_id, -32603, f"tool {tool_name!r} failed: {e}")
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
        return JSONResponse(dispatch(cfg, db, body))
