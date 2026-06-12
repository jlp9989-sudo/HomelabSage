"""Audit tools: report, history, diff, categories, mute list, prune."""

from __future__ import annotations

import logging

from ..config import Config
from ..db import Database

log = logging.getLogger(__name__)


def _tool_audit(cfg: Config, db: Database, _params: dict) -> dict:
    """Run the proactive auditor and return the rendered report.

    Same shape as `/api/audit`: counts by severity/category +
    every finding with its source signal cited. Useful for agents
    that want to surface "is my homelab healthy?" at a glance.
    """
    from ..audit import build_report
    report = build_report(cfg, db)
    return report.to_json()


def _tool_audit_mute_add(_cfg: Config, db: Database, params: dict) -> dict:
    """Add or refresh an audit-finding mute by fingerprint."""
    for field in ("category", "source_kind", "source_ref"):
        v = params.get(field)
        if not isinstance(v, str) or not v.strip():
            return {"ok": False, "error": f"{field} is required"}
    expires = params.get("expires_at")
    if expires is not None and not isinstance(expires, str):
        return {"ok": False, "error": "expires_at must be a string or null"}
    if expires:
        from .._time import parse_iso
        try:
            parse_iso(expires)
        except ValueError:
            return {"ok": False, "error": "expires_at must be ISO 8601"}
    reason = params.get("reason")
    db.add_audit_mute(
        category=params["category"],
        source_kind=params["source_kind"],
        source_ref=params["source_ref"],
        expires_at=expires or None,
        reason=reason if isinstance(reason, str) else None,
    )
    return {"ok": True, "expires_at": expires or None}


def _tool_audit_mute_list(_cfg: Config, db: Database, params: dict) -> dict:
    """List audit-finding mutes (active by default)."""
    include_expired = bool(params.get("include_expired") or False)
    rows = db.list_audit_mutes(include_expired=include_expired)
    return {"count": len(rows), "items": rows}


def _tool_audit_mute_remove(_cfg: Config, db: Database, params: dict) -> dict:
    """Remove one mute by fingerprint."""
    for field in ("category", "source_kind", "source_ref"):
        v = params.get(field)
        if not isinstance(v, str) or not v.strip():
            return {"ok": False, "error": f"{field} is required"}
    ok = db.remove_audit_mute(
        category=params["category"],
        source_kind=params["source_kind"],
        source_ref=params["source_ref"],
    )
    return {"ok": ok}


def _tool_audit_mute_purge_expired(_cfg: Config, db: Database, _params: dict) -> dict:
    """Drop every audit-mute row whose `expires_at` is in the past."""
    n = db.purge_expired_audit_mutes()
    return {"ok": True, "purged": n}


def _tool_audit_categories(cfg: Config, db: Database, _params: dict) -> dict:
    """Return just the category histogram from the current audit.

    Lighter payload than `audit` for dashboards that only need
    `{compose_lint: 3, cve: 1}` to colour a stripe — the full
    findings list is megabytes on a busy homelab.
    """
    from ..audit import build_report
    report = build_report(cfg, db)
    return {
        "counts_by_category": report.counts_by_category,
        "counts_by_severity": report.counts_by_severity,
        "total": len(report.findings),
        "healthy": report.healthy,
    }


def _tool_audit_prune(cfg: Config, _db: Database, params: dict) -> dict:
    """Truncate audit_history.jsonl to the `keep_last` newest rows."""
    from ..audit_history import prune
    notes_dir = cfg.curator.output_dir or cfg.notes.notes_dir
    if not notes_dir:
        return {"ok": False, "dropped": 0, "reason": "no notes_dir configured"}
    keep = max(0, int(params.get("keep_last") or 100))
    dropped = prune(notes_dir, keep_last=keep)
    return {"ok": True, "dropped": dropped, "keep_last": keep}


def _tool_audit_history(cfg: Config, _db: Database, params: dict) -> dict:
    """Paginated list of past audit snapshots (newest first).

    Returns compact summaries — never the full findings list, to
    keep MCP frames small. For the full diff, use `audit_diff`.
    """
    from ..audit_history import list_history
    notes_dir = cfg.curator.output_dir or cfg.notes.notes_dir
    if not notes_dir:
        return {"count": 0, "items": [], "reason": "no notes_dir configured"}
    limit = max(1, min(int(params.get("limit") or 50), 200))
    offset = max(0, int(params.get("offset") or 0))
    rows = list_history(notes_dir, limit=limit, offset=offset)
    return {"count": len(rows), "items": rows}


def _tool_audit_diff(cfg: Config, db: Database, _params: dict) -> dict:
    """Compute new/resolved findings vs the latest persisted snapshot."""
    from ..audit import build_report
    from ..audit_history import diff_against_latest
    notes_dir = cfg.curator.output_dir or cfg.notes.notes_dir
    if not notes_dir:
        return {
            "previous_snapshot": None, "new": [], "resolved": [],
            "reason": "no notes_dir configured",
        }
    report = build_report(cfg, db)
    return diff_against_latest(notes_dir, report.to_json())



TOOLS: dict[str, dict] = {
    "audit": {
        "description": (
            "Run the proactive auditor and return the report (counts + every "
            "finding with its source signal cited). Same data as `/api/audit`."
        ),
        "params_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "impl": _tool_audit,
    },
    "audit_mute_add": {
        "description": (
            "Mute an audit finding by `(category, source_kind, "
            "source_ref)` fingerprint. Optional ISO 8601 "
            "`expires_at` (empty/null = permanent). Idempotent: "
            "re-adding refreshes the row."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "source_kind": {"type": "string"},
                "source_ref": {"type": "string"},
                "expires_at": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                },
                "reason": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                },
            },
            "required": ["category", "source_kind", "source_ref"],
            "additionalProperties": False,
        },
        "impl": _tool_audit_mute_add,
    },
    "audit_mute_list": {
        "description": (
            "List audit-finding mutes (active by default; pass "
            "include_expired=true for full history)."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "include_expired": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "impl": _tool_audit_mute_list,
    },
    "audit_mute_remove": {
        "description": (
            "Remove one audit-finding mute by fingerprint. Returns "
            "`{ok: bool}` — false when no row matched."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "source_kind": {"type": "string"},
                "source_ref": {"type": "string"},
            },
            "required": ["category", "source_kind", "source_ref"],
            "additionalProperties": False,
        },
        "impl": _tool_audit_mute_remove,
    },
    "audit_mute_purge_expired": {
        "description": (
            "Drop every audit-mute row whose `expires_at` is in the "
            "past. Returns `{ok, purged}`."
        ),
        "params_schema": {
            "type": "object", "properties": {},
            "additionalProperties": False,
        },
        "impl": _tool_audit_mute_purge_expired,
    },
    "audit_categories": {
        "description": (
            "Compact audit roll-up: `{counts_by_category, "
            "counts_by_severity, total, healthy}`. No findings list."
        ),
        "params_schema": {
            "type": "object", "properties": {},
            "additionalProperties": False,
        },
        "impl": _tool_audit_categories,
    },
    "audit_prune": {
        "description": (
            "Truncate audit_history.jsonl to the `keep_last` newest "
            "rows (default 100). Atomic rewrite. Returns the number "
            "of rows dropped."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "keep_last": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": False,
        },
        "impl": _tool_audit_prune,
    },
    "audit_history": {
        "description": (
            "Paginated list of past audit snapshots, newest first. "
            "Returns compact per-snapshot summaries (severity/"
            "category counts + finding_count), never the full "
            "findings list."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "offset": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": False,
        },
        "impl": _tool_audit_history,
    },
    "audit_diff": {
        "description": (
            "Diff the current audit findings against the latest "
            "persisted snapshot. Returns `{previous_snapshot, new, "
            "resolved}` where new/resolved are keyed by "
            "(category, source_kind, source_ref)."
        ),
        "params_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "impl": _tool_audit_diff,
    },
}
