"""Operational tools: diagnostics, watched repos, dispatch queue, csi, chronicle, system info."""

from __future__ import annotations

import logging

from .. import __version__
from ..config import Config
from ..db import Database
from ..diagnostics import diagnose_containers, summarise
from ..models import UpdateStatus
from ._shared import _run_coro

log = logging.getLogger(__name__)


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


def _tool_clear_pending_dispatches(_cfg: Config, db: Database, _params: dict) -> dict:
    """Drop every row from the pending push-dispatch queue."""
    cleared = db.clear_pending_dispatches()
    return {"ok": True, "cleared": cleared}


def _tool_analyze_url(cfg: Config, _db: Database, params: dict) -> dict:
    """Run the URL analyser on any supported URL shape.

    Same dispatch as `homelabsage analyse <url>`: GitHub/Codeberg →
    repo analyser; Docker Hub → hub analyser; HuggingFace → model
    card + VRAM fit; everything else → article analyser via
    trafilatura. Returns the analysed Update + analysis as JSON.
    """
    url = params.get("url")
    if not url:
        raise ValueError("url is required")
    from ..analyse_url import analyse_url
    result = _run_coro(analyse_url(
        cfg, str(url), current_version=str(params.get("current_version") or ""),
    ))
    if result is None:
        return {"matched": False, "reason": "no analyser path matched this URL"}
    return {"matched": True, "item": result.model_dump(mode="json")}


def _tool_csi(cfg: Config, _db: Database, params: dict) -> dict:
    """Post-mortem evidence for a container.

    Mirrors `homelabsage csi <name>`: pulls recent ERROR/WARN log lines
    + last-detected update + notes excerpt, optionally runs the LLM.
    `--evidence-only` is exposed as `llm: false` for air-gapped agents.
    """
    container_name = params.get("container_name")
    if not container_name:
        raise ValueError("container_name is required")
    use_llm = bool(params.get("llm", True))
    from ..csi import build_prompt, gather_evidence, run_csi
    if use_llm:
        report, evidence = _run_coro(run_csi(cfg, str(container_name)))
        return {
            "container_name": evidence.container_name,
            "log_lines": evidence.log_lines,
            "notes_excerpt": evidence.notes_excerpt,
            "log_since": (
                evidence.log_since.isoformat()
                if evidence.log_since else None
            ),
            "report_markdown": report,
        }
    # Evidence-only: never touch the LLM
    evidence = gather_evidence(cfg, str(container_name))
    return {
        "container_name": evidence.container_name,
        "log_lines": evidence.log_lines,
        "notes_excerpt": evidence.notes_excerpt,
        "log_since": (
            evidence.log_since.isoformat()
            if evidence.log_since else None
        ),
        "prompt_only": build_prompt(evidence),
    }


def _tool_list_heartbeats(_cfg: Config, db: Database, params: dict) -> dict:
    """Recent heartbeat history + 24h summary.

    Returns `{summary: {...}, recent: [...]}`. `limit` caps the
    recent list (default 50, max 500). Summary is the rolling 24h
    success/fail counts already computed by `db.heartbeat_summary`.
    """
    limit = max(1, min(int(params.get("limit") or 50), 500))
    summary = db.heartbeat_summary(hours=24)
    recent = db.list_recent_heartbeats(limit=limit)
    return {"summary": summary, "recent": recent}


def _tool_get_chronicle(_cfg: Config, db: Database, params: dict) -> dict:
    """Walk the last `days` of updates and return the narrative timeline.

    Returns `{period_start, period_end, counts_by_kind, entries}`.
    Same data as `homelabsage chronicle` CLI but JSON-shaped for
    agents. Each entry has `{when, subject, kind, headline, detail}`.
    Default 30 days; clamp 1..365.
    """
    from ..chronicle import build_chronicle
    days = max(1, min(int(params.get("days") or 30), 365))
    chronicle = build_chronicle(db, days=days)
    return {
        "period_start": chronicle.period_start.isoformat(),
        "period_end": chronicle.period_end.isoformat(),
        "counts_by_kind": chronicle.counts_by_kind(),
        "entries": [
            {
                "when": e.when.isoformat() if e.when else None,
                "subject": e.subject,
                "kind": e.kind,
                "headline": e.headline,
                "detail": e.detail,
            }
            for e in chronicle.entries
        ],
    }


def _tool_system_info(cfg: Config, db: Database, _params: dict) -> dict:
    """One-call rollup: version + update counts + audit/snooze/mute totals.

    Designed for an agent's first probe — "give me the shape of this
    install". Cheaper than `doctor` (no LLM/TLS/DNS probes) and richer
    than `status_health` (counts the user-facing surface too).
    """
    from ..audit import build_report

    counts = {s.value: 0 for s in UpdateStatus}
    for it in db.list(limit=500):
        counts[it.status.value] = counts.get(it.status.value, 0) + 1
    report = build_report(cfg, db)
    snoozed = (
        len(db.list_snoozed(limit=500))
       )
    mutes = (
        len(db.list_audit_mutes())
       )
    return {
        "version": __version__,
        "updates_by_status": counts,
        "audit_total": len(report.findings),
        "audit_counts_by_severity": report.counts_by_severity,
        "snoozed_total": snoozed,
        "audit_mutes_total": mutes,
        "pending_dispatches": len(db.list_pending_dispatches()),
    }


def _tool_version(_cfg: Config, _db: Database, _params: dict) -> dict:
    """Return the HomelabSage version + a brief feature flag map.

    Lets a downstream agent gate behaviour on minimum version
    (e.g. "this tool only works against ≥ 0.7.0"). `version_parts`
    is a `[major, minor, patch]` int list so the agent doesn't have
    to parse the string itself.
    """
    parts: list[int] = []
    for p in __version__.split("."):
        try:
            parts.append(int(p.split("-", 1)[0]))   # strip RC/dev suffix
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return {
        "version": __version__,
        "version_parts": parts[:3],
        "features": {
            "doctor": True,
            "audit_history": True,
            "snooze": True,
            "audit_alerts": True,
        },
    }


def _tool_health_check_results(_cfg: Config, db: Database, params: dict) -> dict:
    """Recent post-update health-check rows.

    Returns the failures by default (the actionable set); pass
    `include_ok=true` for the full history.
    """
    limit = int(params.get("limit") or 100)
    rows = db.list_recent_health_checks(limit=max(1, min(limit, 500)))
    if not params.get("include_ok"):
        rows = [r for r in rows if not r.get("ok")]
    return {"count": len(rows), "items": rows}



TOOLS: dict[str, dict] = {
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
    "clear_pending_dispatches": {
        "description": (
            "Drop every row from the pending push-dispatch queue. "
            "Operator panic-button for misconfigured outputs that "
            "queued thousands of items. Returns count cleared."
        ),
        "params_schema": {
            "type": "object", "properties": {},
            "additionalProperties": False,
        },
        "impl": _tool_clear_pending_dispatches,
    },
    "analyze_url": {
        "description": (
            "Run the URL analyser on any supported URL "
            "(GitHub/Codeberg, Docker Hub, HuggingFace, article). "
            "Returns the analysed Update + analysis."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "current_version": {"type": "string"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "impl": _tool_analyze_url,
    },
    "csi": {
        "description": (
            "Post-mortem evidence for a container — recent ERROR/WARN log "
            "lines, last-detected update, user notes excerpt. Set llm=false "
            "for the air-gapped evidence-only view."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "container_name": {"type": "string"},
                "llm": {"type": "boolean", "default": True},
            },
            "required": ["container_name"],
            "additionalProperties": False,
        },
        "impl": _tool_csi,
    },
    "list_heartbeats": {
        "description": (
            "Recent heartbeat history + 24h rolling summary. "
            "`{summary: {...}, recent: [...]}`. `limit` capped 500."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "additionalProperties": False,
        },
        "impl": _tool_list_heartbeats,
    },
    "get_chronicle": {
        "description": (
            "Walk the last `days` of updates and return the "
            "narrative timeline (applied / dismissed / breaking / "
            "severity_jump). Returns `{period_start, period_end, "
            "counts_by_kind, entries}`. Default 30 days; clamp 1..365."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 365},
            },
            "additionalProperties": False,
        },
        "impl": _tool_get_chronicle,
    },
    "system_info": {
        "description": (
            "One-call rollup: version + updates by status + audit "
            "totals + snooze + mute + pending-dispatch counts. The "
            "right shape for an agent's first probe of an install."
        ),
        "params_schema": {
            "type": "object", "properties": {},
            "additionalProperties": False,
        },
        "impl": _tool_system_info,
    },
    "version": {
        "description": (
            "Return HomelabSage version + a feature flag map for "
            "downstream agents to gate on min-version requirements."
        ),
        "params_schema": {
            "type": "object", "properties": {},
            "additionalProperties": False,
        },
        "impl": _tool_version,
    },
    "health_check_results": {
        "description": (
            "Recent post-update health-check rows. Defaults to failures only; "
            "set include_ok=true to get the full history."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "include_ok": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "impl": _tool_health_check_results,
    },
}
