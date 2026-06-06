"""MCP tool implementations + the TOOLS registry.

Split out of `mcp.py` in v0.11.1 to bring that file from 1828 LOC
down to ~150 (route registration + dispatch only). This module owns
all 50 `_tool_*` impls, the JSON-Schema descriptor literal `TOOLS`,
and the helpers that turn an `AnalyzedUpdate` into the dict shape
the impls return.

`mcp.py` only imports `TOOLS` from here; everything else stays
private. Keeping the impls + their descriptors in one file (and OUT
of the dispatch file) makes both files single-purpose and the dispatch
trivially readable.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

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


def _tool_set_update_status(cfg: Config, db: Database, params: dict) -> dict:
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
    item = db.get(update_id)
    if item is None:
        raise ValueError(f"no update with id={update_id!r}")
    db.set_status(update_id, status)
    if status == UpdateStatus.APPLIED and cfg.health_check.enabled and \
            item.update.source == "docker":
        from .health_check import queue_for_update
        queue_for_update(
            db, update_id=update_id,
            container_name=item.update.subject, cfg=cfg.health_check,
        )
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


def _tool_audit(cfg: Config, db: Database, _params: dict) -> dict:
    """Run the proactive auditor and return the rendered report.

    Same shape as `/api/audit`: counts by severity/category +
    every finding with its source signal cited. Useful for agents
    that want to surface "is my homelab healthy?" at a glance.
    """
    from .audit import build_report
    report = build_report(cfg, db)
    return report.to_json()


def _tool_rollback_recipe(cfg: Config, db: Database, params: dict) -> dict:
    """Return the rollback recipe (CLI + compose forms) for an update."""
    update_id = params.get("update_id")
    if not update_id:
        raise ValueError("update_id is required")
    item = db.get(update_id)
    if item is None:
        raise ValueError(f"no update with id={update_id!r}")
    from .rollback import build_recipe, render_markdown
    # Build the compose graph so the recipe surfaces the docker-compose
    # form + cascade warnings. Empty when no compose_scan_paths are
    # configured — the recipe still renders the CLI form.
    graph = None
    if cfg.sources.docker.compose_scan_paths:
        try:
            from .compose import build_graph as build_compose_graph
            graph = build_compose_graph(cfg.sources.docker.compose_scan_paths)
        except Exception as e:
            log.debug("rollback_recipe compose graph failed: %s", e)
    recipe = build_recipe(item, graph=graph)
    return {
        "container_name": recipe.container_name,
        "prior_image": recipe.prior_image,
        "new_image": recipe.new_image,
        "cli_steps": recipe.cli_steps,
        "compose_steps": recipe.compose_steps,
        "compose_file": str(recipe.compose_file) if recipe.compose_file else None,
        "cascade_warnings": recipe.cascade_warnings,
        "markdown": render_markdown(recipe),
    }


def _tool_history_csv(_cfg: Config, db: Database, params: dict) -> dict:
    """Dump the update history as CSV in the response body.

    For agents that want to ingest the audit log directly. Output is
    capped at `limit` rows (default 1000) so a busy DB doesn't blow
    the JSON-RPC envelope.
    """
    from .history import dump_to_string
    limit = int(params.get("limit") or 1000)
    limit = max(1, min(limit, 5000))
    body = dump_to_string(db, limit=limit)
    return {"csv": body, "rows": body.count("\n") - 1 if body else 0}


def _run_coro(coro):
    """Run a coroutine to completion from a sync context that may or
    may not already be inside an event loop.

    Since v0.10.1 the HTTP `/mcp` route awaits `asyncio.to_thread(dispatch,
    …)`, so the thread executing this helper does NOT have a running
    loop — the `asyncio.run` branch is the only one HTTP callers take.
    The thread-spawn fallback is defensive for direct importers that
    call `dispatch()` from inside their own async code (the standalone
    tool path) — keeping it lets us stay independent of who invokes us
    without rewriting the call sites.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Running loop present — schedule on a worker thread.
    result_holder: dict = {}

    def _worker():
        loop = asyncio.new_event_loop()
        try:
            result_holder["v"] = loop.run_until_complete(coro)
        except BaseException as e:  # propagate
            result_holder["err"] = e
        finally:
            loop.close()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join()
    if "err" in result_holder:
        raise result_holder["err"]
    return result_holder.get("v")


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
    from .analyse_url import analyse_url
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
    from .csi import build_prompt, gather_evidence, run_csi
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


def _tool_get_user_note(_cfg: Config, db: Database, params: dict) -> dict:
    """Read the free-text user_note attached to an update."""
    update_id = params.get("update_id")
    if not update_id:
        raise ValueError("update_id is required")
    if db.get(update_id) is None:
        raise ValueError(f"no update with id={update_id!r}")
    return {"update_id": update_id, "note": db.get_user_note(update_id) or ""}


def _tool_set_user_note(_cfg: Config, db: Database, params: dict) -> dict:
    """Attach (or clear) the free-text user_note for an update."""
    update_id = params.get("update_id")
    if not update_id:
        raise ValueError("update_id is required")
    note = params.get("note")
    if note is not None and not isinstance(note, str):
        raise ValueError("note must be a string or null")
    if not db.set_user_note(update_id, note or ""):
        raise ValueError(f"no update with id={update_id!r}")
    return {"ok": True, "update_id": update_id,
            "note": db.get_user_note(update_id) or ""}


def _tool_search_updates(_cfg: Config, db: Database, params: dict) -> dict:
    """Substring search across subject + summary + breaking_changes + user_note."""
    q = params.get("q")
    if not isinstance(q, str) or not q.strip():
        raise ValueError("q is required")
    limit = int(params.get("limit") or 50)
    items = db.search(q, limit=max(1, min(limit, 200)))
    return {
        "query": q, "count": len(items),
        "items": [_summarise_update(it) for it in items],
    }


def _tool_explain(_cfg: Config, db: Database, params: dict) -> dict:
    """Return the stored prompt + raw LLM response for an update.

    Same data the `/updates/<id>/explain` web page renders. Useful for
    an agent that wants to audit a verdict ("show me what the model
    actually said") without scraping HTML.
    """
    update_id = params.get("update_id")
    if not update_id:
        raise ValueError("update_id is required")
    if not hasattr(db, "get_explainer"):
        raise ValueError("explainers mixin not wired into this DB")
    row = db.get_explainer(update_id)
    if row is None:
        raise ValueError(f"no explainer for update_id={update_id!r}")
    return row


def _tool_set_star(_cfg: Config, db: Database, params: dict) -> dict:
    """Toggle the bookmark flag on an update."""
    update_id = params.get("update_id")
    starred = params.get("starred")
    if not update_id:
        raise ValueError("update_id is required")
    if not isinstance(starred, bool):
        raise ValueError("starred must be a boolean")
    if not db.set_starred(update_id, starred):
        raise ValueError(f"no update with id={update_id!r}")
    return {"ok": True, "update_id": update_id,
            "starred": db.is_starred(update_id)}


def _tool_list_starred(_cfg: Config, db: Database, _params: dict) -> dict:
    """List bookmarked updates."""
    items = db.list_starred(limit=200)
    return {"count": len(items),
            "items": [_summarise_update(it) for it in items]}


def _tool_doctor(cfg: Config, db: Database, params: dict) -> dict:
    """Run the bundled diagnostic (LLM / TLS / DNS / disk / compose / audit).

    Same data as `homelabsage doctor` CLI and `GET /api/doctor`,
    surfaced for agents. Set `skip_llm=true` for offline runs.
    """
    from .doctor import build_report as build_doctor_report
    skip_llm = bool(params.get("skip_llm") or False)
    return build_doctor_report(cfg, db, skip_llm=skip_llm)


def _tool_compose_graph_mermaid(cfg: Config, _db: Database, params: dict) -> dict:
    """Render the compose dependency graph as Mermaid.

    `paths` param overrides the configured scan paths. Returns
    `{mermaid: "...", service_count: N, edge_count: M}`.
    """
    from .cli.compose_graph import render_mermaid
    from .compose import build_graph
    paths = params.get("paths")
    if not isinstance(paths, list) or not paths:
        paths = list(cfg.sources.docker.compose_scan_paths or [])
    if not paths:
        return {
            "mermaid": "", "service_count": 0, "edge_count": 0,
            "reason": "no compose scan paths configured",
        }
    graph = build_graph([str(p) for p in paths])
    dependents = getattr(graph, "_dependents", {})
    body = render_mermaid(graph.services, dependents)
    edge_count = sum(len(s.depends_on) for s in graph.services.values())
    return {
        "mermaid": body,
        "service_count": len(graph.services),
        "edge_count": edge_count,
    }


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
        from ._time import parse_iso
        try:
            parse_iso(expires)
        except ValueError:
            return {"ok": False, "error": "expires_at must be ISO 8601"}
    reason = params.get("reason")
    if not hasattr(db, "add_audit_mute"):
        return {"ok": False, "error": "db missing helper"}
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
    if not hasattr(db, "list_audit_mutes"):
        return {"count": 0, "items": []}
    include_expired = bool(params.get("include_expired") or False)
    rows = db.list_audit_mutes(include_expired=include_expired)
    return {"count": len(rows), "items": rows}


def _tool_audit_mute_remove(_cfg: Config, db: Database, params: dict) -> dict:
    """Remove one mute by fingerprint."""
    for field in ("category", "source_kind", "source_ref"):
        v = params.get(field)
        if not isinstance(v, str) or not v.strip():
            return {"ok": False, "error": f"{field} is required"}
    if not hasattr(db, "remove_audit_mute"):
        return {"ok": False, "error": "db missing helper"}
    ok = db.remove_audit_mute(
        category=params["category"],
        source_kind=params["source_kind"],
        source_ref=params["source_ref"],
    )
    return {"ok": ok}


def _tool_audit_mute_purge_expired(_cfg: Config, db: Database, _params: dict) -> dict:
    """Drop every audit-mute row whose `expires_at` is in the past."""
    if not hasattr(db, "purge_expired_audit_mutes"):
        return {"ok": False, "purged": 0, "error": "db missing helper"}
    n = db.purge_expired_audit_mutes()
    return {"ok": True, "purged": n}


def _tool_audit_categories(cfg: Config, db: Database, _params: dict) -> dict:
    """Return just the category histogram from the current audit.

    Lighter payload than `audit` for dashboards that only need
    `{compose_lint: 3, cve: 1}` to colour a stripe — the full
    findings list is megabytes on a busy homelab.
    """
    from .audit import build_report
    report = build_report(cfg, db)
    return {
        "counts_by_category": report.counts_by_category,
        "counts_by_severity": report.counts_by_severity,
        "total": len(report.findings),
        "healthy": report.healthy,
    }


def _tool_audit_prune(cfg: Config, _db: Database, params: dict) -> dict:
    """Truncate audit_history.jsonl to the `keep_last` newest rows."""
    from .audit_history import prune
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
    from .audit_history import list_history
    notes_dir = cfg.curator.output_dir or cfg.notes.notes_dir
    if not notes_dir:
        return {"count": 0, "items": [], "reason": "no notes_dir configured"}
    limit = max(1, min(int(params.get("limit") or 50), 200))
    offset = max(0, int(params.get("offset") or 0))
    rows = list_history(notes_dir, limit=limit, offset=offset)
    return {"count": len(rows), "items": rows}


def _tool_audit_diff(cfg: Config, db: Database, _params: dict) -> dict:
    """Compute new/resolved findings vs the latest persisted snapshot."""
    from .audit import build_report
    from .audit_history import diff_against_latest
    notes_dir = cfg.curator.output_dir or cfg.notes.notes_dir
    if not notes_dir:
        return {
            "previous_snapshot": None, "new": [], "resolved": [],
            "reason": "no notes_dir configured",
        }
    report = build_report(cfg, db)
    return diff_against_latest(notes_dir, report.to_json())


def _tool_dangling_images(_cfg: Config, _db: Database, _params: dict) -> dict:
    """Walk the local image cache and report dangling `<none>:<none>` images.

    Reaches the docker daemon directly — when the SDK or daemon
    isn't available, returns `{ok: false, error: ...}` so a
    homelab without docker doesn't crash the agent.
    """
    try:
        import docker
        client = docker.from_env()
        images = client.images.list(all=True, filters={"dangling": True})
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "items": []}
    from .dangling_images import find_dangling
    findings = find_dangling(images)
    total_bytes = sum(f.size_bytes for f in findings)
    return {
        "ok": True,
        "count": len(findings),
        "total_bytes": total_bytes,
        "total_mib": round(total_bytes / (1024 ** 2), 1),
        "items": [f.to_context() for f in findings],
    }


def _tool_is_snoozed(_cfg: Config, db: Database, params: dict) -> dict:
    """Quick lookup: is this update currently snoozed?

    Returns `{snoozed: bool, snooze_until: str|null}`. `snoozed` is
    True only when the timestamp is in the future — past timestamps
    are treated as expired (matches engine + flush behaviour).
    """
    update_id = params.get("update_id")
    if not isinstance(update_id, str) or not update_id:
        return {"snoozed": False, "snooze_until": None,
                "error": "update_id is required"}
    if not hasattr(db, "get_snooze"):
        return {"snoozed": False, "snooze_until": None}
    raw = db.get_snooze(update_id)
    if not raw:
        return {"snoozed": False, "snooze_until": None}
    from ._time import parse_iso, utcnow
    try:
        until = parse_iso(raw)
    except ValueError:
        return {"snoozed": False, "snooze_until": raw,
                "error": "snooze_until unparseable"}
    if until is None:
        return {"snoozed": False, "snooze_until": raw}
    if until.tzinfo is None:
        from datetime import UTC
        until = until.replace(tzinfo=UTC)
    return {
        "snoozed": until > utcnow(),
        "snooze_until": raw,
    }


def _tool_list_snoozed(_cfg: Config, db: Database, params: dict) -> dict:
    """List currently-snoozed updates (snooze_until > now)."""
    if not hasattr(db, "list_snoozed"):
        return {"count": 0, "items": []}
    limit = max(1, min(int(params.get("limit") or 200), 500))
    rows = db.list_snoozed(limit=limit)
    return {"count": len(rows), "items": rows}


def _tool_clear_all_snoozes(_cfg: Config, db: Database, _params: dict) -> dict:
    """Clear `snooze_until` on every snoozed update."""
    if not hasattr(db, "clear_all_snoozes"):
        return {"ok": False, "cleared": 0, "error": "db missing helper"}
    cleared = db.clear_all_snoozes()
    return {"ok": True, "cleared": cleared}


def _tool_clear_pending_dispatches(_cfg: Config, db: Database, _params: dict) -> dict:
    """Drop every row from the pending push-dispatch queue."""
    if not hasattr(db, "clear_pending_dispatches"):
        return {"ok": False, "cleared": 0, "error": "db missing helper"}
    cleared = db.clear_pending_dispatches()
    return {"ok": True, "cleared": cleared}


def _tool_purge_old_updates(_cfg: Config, db: Database, params: dict) -> dict:
    """Drop APPLIED+DISMISSED updates older than `older_than_days`."""
    if not hasattr(db, "purge_old_updates"):
        return {"ok": False, "purged": 0, "error": "db missing helper"}
    days = max(1, int(params.get("older_than_days") or 90))
    dry = bool(params.get("dry_run") or False)
    n = db.purge_old_updates(older_than_days=days, dry_run=dry)
    return {"ok": True, "purged": n, "older_than_days": days, "dry_run": dry}


def _tool_scan_window_check(cfg: Config, _db: Database, _params: dict) -> dict:
    """Would a scan start right now, or is the scan-window gate blocking?

    Returns `{enabled, blocked, reason}`. Useful for agents that
    want to decide "trigger /run now" vs "wait until the window
    clears".
    """
    from .scan_window import is_scan_blocked
    if not cfg.scan_window.enabled:
        return {"enabled": False, "blocked": False, "reason": "disabled"}
    verdict = is_scan_blocked(
        enabled=True,
        window_spec=cfg.scan_window.window,
        timezone_name=cfg.scan_window.timezone,
    )
    return {
        "enabled": True,
        "blocked": bool(verdict),
        "reason": verdict.reason,
    }


def _tool_get_explainer(_cfg: Config, db: Database, params: dict) -> dict:
    """Return the cached LLM prompt + raw response for an update.

    Cheaper than `explain` (which re-runs the LLM) when the user
    asked "why?" within the same scan cycle. Returns
    `{ok, prompt, raw_response, model, created_at}` or
    `{ok: false, error}` when no cached explainer exists.
    """
    update_id = params.get("update_id")
    if not isinstance(update_id, str) or not update_id:
        return {"ok": False, "error": "update_id is required"}
    if not hasattr(db, "get_explainer"):
        return {"ok": False, "error": "db missing helper"}
    row = db.get_explainer(update_id)
    if row is None:
        return {"ok": False, "error": f"no explainer for {update_id}"}
    return {"ok": True, **row}


def _tool_list_heartbeats(_cfg: Config, db: Database, params: dict) -> dict:
    """Recent heartbeat history + 24h summary.

    Returns `{summary: {...}, recent: [...]}`. `limit` caps the
    recent list (default 50, max 500). Summary is the rolling 24h
    success/fail counts already computed by `db.heartbeat_summary`.
    """
    if not hasattr(db, "list_recent_heartbeats"):
        return {"summary": {}, "recent": []}
    limit = max(1, min(int(params.get("limit") or 50), 500))
    summary = db.heartbeat_summary(hours=24) if hasattr(db, "heartbeat_summary") else {}
    recent = db.list_recent_heartbeats(limit=limit)
    return {"summary": summary, "recent": recent}


def _tool_get_chronicle(_cfg: Config, db: Database, params: dict) -> dict:
    """Walk the last `days` of updates and return the narrative timeline.

    Returns `{period_start, period_end, counts_by_kind, entries}`.
    Same data as `homelabsage chronicle` CLI but JSON-shaped for
    agents. Each entry has `{when, subject, kind, headline, detail}`.
    Default 30 days; clamp 1..365.
    """
    from .chronicle import build_chronicle
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


def _tool_where_is(cfg: Config, _db: Database, params: dict) -> dict:
    """Locate a compose service / container by name.

    Returns hits with file + line + project + context excerpt. Empty
    `name` returns count=0. No compose scan paths configured →
    `reason` explains.
    """
    name = params.get("name")
    if not isinstance(name, str) or not name.strip():
        return {"count": 0, "items": [], "reason": "name is required"}
    roots = list(cfg.sources.docker.compose_scan_paths or [])
    if not roots:
        return {
            "count": 0, "items": [],
            "reason": "no compose scan paths configured",
        }
    from .compose import build_graph
    from .where_is import find as find_where
    graph = build_graph(roots)
    hits = find_where(graph, name)
    return {
        "count": len(hits),
        "items": [h.to_context() for h in hits],
    }


def _tool_update_diff(_cfg: Config, db: Database, params: dict) -> dict:
    """Side-by-side comparison of two analyzed updates by id.

    Returns `{a, b, diff: {severity_changed, breaking_changes_added,
    breaking_changes_removed, summary_a, summary_b}}`. Either id
    missing → `{ok: false, error}`. Useful for "did this rebase
    change the risk profile?" or post-mortem queries.
    """
    a_id = params.get("a")
    b_id = params.get("b")
    if not isinstance(a_id, str) or not a_id:
        return {"ok": False, "error": "a is required"}
    if not isinstance(b_id, str) or not b_id:
        return {"ok": False, "error": "b is required"}
    a = db.get(a_id)
    b = db.get(b_id)
    if a is None:
        return {"ok": False, "error": f"no update with id={a_id}"}
    if b is None:
        return {"ok": False, "error": f"no update with id={b_id}"}

    def _shape(it):
        return {
            "id": it.id,
            "subject": it.update.subject,
            "source": it.update.source,
            "version": f"{it.update.current_version} → {it.update.new_version}",
            "status": it.status.value,
            "severity": (
                it.analysis.severity.value if it.analysis else None
            ),
            "breaking_changes": (
                list(it.analysis.breaking_changes or [])
                if it.analysis else []
            ),
            "summary": (
                it.analysis.summary if it.analysis else None
            ),
        }

    a_s = _shape(a)
    b_s = _shape(b)
    set_a = set(a_s["breaking_changes"])
    set_b = set(b_s["breaking_changes"])
    return {
        "ok": True,
        "a": a_s,
        "b": b_s,
        "diff": {
            "severity_changed": a_s["severity"] != b_s["severity"],
            "breaking_changes_added": sorted(set_b - set_a),
            "breaking_changes_removed": sorted(set_a - set_b),
            "summary_a": a_s["summary"],
            "summary_b": b_s["summary"],
        },
    }


def _tool_system_info(cfg: Config, db: Database, _params: dict) -> dict:
    """One-call rollup: version + update counts + audit/snooze/mute totals.

    Designed for an agent's first probe — "give me the shape of this
    install". Cheaper than `doctor` (no LLM/TLS/DNS probes) and richer
    than `status_health` (counts the user-facing surface too).
    """
    from . import __version__
    from .audit import build_report
    from .models import UpdateStatus

    counts = {s.value: 0 for s in UpdateStatus}
    for it in db.list(limit=500):
        counts[it.status.value] = counts.get(it.status.value, 0) + 1
    report = build_report(cfg, db)
    snoozed = (
        len(db.list_snoozed(limit=500))
        if hasattr(db, "list_snoozed") else 0
    )
    mutes = (
        len(db.list_audit_mutes())
        if hasattr(db, "list_audit_mutes") else 0
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
    from . import __version__
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


def _tool_snooze_update(_cfg: Config, db: Database, params: dict) -> dict:
    """Set or clear a snooze timestamp on an update.

    Pass `snooze_until` as ISO 8601 (UTC). Pass empty string / null
    to clear. Returns the resulting snooze value.
    """
    update_id = params.get("update_id")
    raw = params.get("snooze_until")
    if not update_id or not isinstance(update_id, str):
        return {"ok": False, "error": "update_id is required"}
    snooze: str | None = None
    if raw:
        if not isinstance(raw, str):
            return {"ok": False, "error": "snooze_until must be a string"}
        # Validate parseable.
        from ._time import parse_iso
        try:
            parse_iso(raw)
        except ValueError:
            return {
                "ok": False,
                "error": f"snooze_until not ISO 8601: {raw!r}",
            }
        snooze = raw
    if not db.set_snooze(update_id, snooze):
        return {"ok": False, "error": f"no update with id={update_id}"}
    return {
        "ok": True, "update_id": update_id,
        "snooze_until": db.get_snooze(update_id),
    }


def _tool_recurring_failures(_cfg: Config, db: Database, params: dict) -> dict:
    """List updates that have failed N+ times."""
    if not hasattr(db, "list_recurring_failures"):
        return {"count": 0, "items": []}
    min_count = max(1, int(params.get("min_count") or 2))
    limit = max(1, min(int(params.get("limit") or 50), 500))
    rows = db.list_recurring_failures(min_count=min_count, limit=limit)
    return {"count": len(rows), "items": rows}


def _tool_dns_check(cfg: Config, _db: Database, params: dict) -> dict:
    """Resolve hostnames; return failures only.

    `hostnames` param overrides; defaults to TLS check URLs' hosts.
    """
    hostnames = params.get("hostnames")
    if not isinstance(hostnames, list) or not hostnames:
        # Derive from tls_check.urls — same hosts the user already
        # cares about for cert validity.
        from urllib.parse import urlparse
        hostnames = []
        for raw in cfg.tls_check.urls or []:
            p = urlparse(raw if "://" in raw else "https://" + raw)
            if p.hostname:
                hostnames.append(p.hostname)
    if not hostnames:
        return {
            "count": 0, "items": [],
            "reason": "no hostnames configured",
        }
    from .dns_check import check_hostnames
    findings = check_hostnames([str(h) for h in hostnames])
    return {
        "count": len(findings),
        "items": [f.to_context() for f in findings],
    }


def _tool_disk_pressure_check(cfg: Config, _db: Database, params: dict) -> dict:
    """Run the disk-pressure probe on-demand.

    Uses `params.paths` if provided; falls back to
    `cfg.disk_pressure.paths`. Returns empty when neither has content
    so the caller can tell "no findings" from "no paths configured".
    """
    from .disk_pressure import evaluate as eval_disk
    paths = params.get("paths")
    if not isinstance(paths, list) or not paths:
        paths = cfg.disk_pressure.paths
    if not paths:
        return {"count": 0, "items": [], "reason": "no paths configured"}
    findings = eval_disk([str(p) for p in paths])
    return {
        "count": len(findings),
        "items": [f.to_context() for f in findings],
    }


def _tool_compose_overrides(cfg: Config, _db: Database, params: dict) -> dict:
    """List `docker-compose.override.yml` files in the configured scan paths.

    Read-only inventory — the runtime graph diverges from the analysed
    base file wherever an override exists.
    """
    from .compose_override import scan as scan_overrides
    paths = params.get("paths")
    if not isinstance(paths, list) or not paths:
        paths = cfg.sources.docker.compose_scan_paths
    if not paths:
        return {"count": 0, "items": [], "reason": "no paths configured"}
    presences = scan_overrides([str(p) for p in paths])
    return {
        "count": len(presences),
        "items": [p.to_context() for p in presences],
    }


def _tool_tls_check_run(cfg: Config, _db: Database, params: dict) -> dict:
    """Run TLS probes on-demand.

    Uses `params.urls` first, falls back to `cfg.tls_check.urls`.
    Returns one entry per URL — failures included so the caller
    sees the full picture.
    """
    from .tls_check import check_urls
    urls = params.get("urls")
    if not isinstance(urls, list) or not urls:
        urls = cfg.tls_check.urls
    if not urls:
        return {"count": 0, "items": [], "reason": "no URLs configured"}
    # I10: cap arbitrary caller-supplied lists. Each probe runs serial at
    # ~10s — a 1000-URL request would block the dispatcher for ~3 hours.
    MAX_URLS = 50
    urls = urls[:MAX_URLS]
    warn_days = int(params.get("warn_days") or cfg.tls_check.warn_days)
    checks = check_urls([str(u) for u in urls], warn_days=max(1, warn_days))
    return {
        "count": len(checks),
        "items": [c.to_context() for c in checks],
    }


def _tool_health_check_results(_cfg: Config, db: Database, params: dict) -> dict:
    """Recent post-update health-check rows.

    Returns the failures by default (the actionable set); pass
    `include_ok=true` for the full history.
    """
    if not hasattr(db, "list_recent_health_checks"):
        return {"count": 0, "items": []}
    limit = int(params.get("limit") or 100)
    rows = db.list_recent_health_checks(limit=max(1, min(limit, 500)))
    if not params.get("include_ok"):
        rows = [r for r in rows if not r.get("ok")]
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
    "audit": {
        "description": (
            "Run the proactive auditor and return the report (counts + every "
            "finding with its source signal cited). Same data as `/api/audit`."
        ),
        "params_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "impl": _tool_audit,
    },
    "rollback_recipe": {
        "description": (
            "Return the rollback recipe (docker CLI + compose forms) for an "
            "update_id. Includes cascade warnings for `depends_on:` services."
        ),
        "params_schema": {
            "type": "object",
            "properties": {"update_id": {"type": "string"}},
            "required": ["update_id"],
            "additionalProperties": False,
        },
        "impl": _tool_rollback_recipe,
    },
    "history_csv": {
        "description": "Dump the update history as CSV (capped at 5000 rows).",
        "params_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
            },
            "additionalProperties": False,
        },
        "impl": _tool_history_csv,
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
    "get_user_note": {
        "description": "Read the free-text user_note for an update.",
        "params_schema": {
            "type": "object",
            "properties": {"update_id": {"type": "string"}},
            "required": ["update_id"],
            "additionalProperties": False,
        },
        "impl": _tool_get_user_note,
    },
    "set_user_note": {
        "description": "Attach (or clear with null/empty) the user_note for an update.",
        "params_schema": {
            "type": "object",
            "properties": {
                "update_id": {"type": "string"},
                "note": {"type": ["string", "null"]},
            },
            "required": ["update_id"],
            "additionalProperties": False,
        },
        "impl": _tool_set_user_note,
    },
    "search_updates": {
        "description": "Substring search over subject/summary/breaking_changes/user_note.",
        "params_schema": {
            "type": "object",
            "properties": {
                "q": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["q"],
            "additionalProperties": False,
        },
        "impl": _tool_search_updates,
    },
    "explain": {
        "description": (
            "Return the stored LLM prompt + raw response for an "
            "update (same data as `/updates/<id>/explain`)."
        ),
        "params_schema": {
            "type": "object",
            "properties": {"update_id": {"type": "string"}},
            "required": ["update_id"],
            "additionalProperties": False,
        },
        "impl": _tool_explain,
    },
    "set_star": {
        "description": "Toggle the bookmark flag on an update.",
        "params_schema": {
            "type": "object",
            "properties": {
                "update_id": {"type": "string"},
                "starred": {"type": "boolean"},
            },
            "required": ["update_id", "starred"],
            "additionalProperties": False,
        },
        "impl": _tool_set_star,
    },
    "list_starred": {
        "description": "List bookmarked updates (newest first, capped 200).",
        "params_schema": {"type": "object", "properties": {},
                          "additionalProperties": False},
        "impl": _tool_list_starred,
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
    "disk_pressure_check": {
        "description": (
            "Probe disk free-space on listed paths (defaults to "
            "cfg.disk_pressure.paths). Returns one finding per "
            "filesystem under pressure."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
        "impl": _tool_disk_pressure_check,
    },
    "compose_overrides": {
        "description": (
            "List docker-compose.override.yml files found in compose "
            "scan paths (or supply `paths`). The runtime graph "
            "diverges from the analysed base file wherever one exists."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
        "impl": _tool_compose_overrides,
    },
    "doctor": {
        "description": (
            "One-shot diagnostic — runs every active probe (LLM "
            "health, TLS certs, DNS resolution, disk pressure, "
            "compose overrides + env perms, audit summary). Returns "
            "a `healthy` boolean plus per-section detail."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "skip_llm": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "impl": _tool_doctor,
    },
    "compose_graph_mermaid": {
        "description": (
            "Render the compose dependency graph as Mermaid. `paths` "
            "param overrides cfg.sources.docker.compose_scan_paths."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
        "impl": _tool_compose_graph_mermaid,
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
    "purge_old_updates": {
        "description": (
            "Delete APPLIED + DISMISSED updates older than "
            "`older_than_days` (default 90). `dry_run=true` counts "
            "without deleting. Snoozed rows are never purged."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "older_than_days": {"type": "integer", "minimum": 1},
                "dry_run": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "impl": _tool_purge_old_updates,
    },
    "scan_window_check": {
        "description": (
            "Probe whether the scan-window gate is currently blocking "
            "scans. Returns `{enabled, blocked, reason}`. Lets agents "
            "decide between trigger-now and wait-for-window."
        ),
        "params_schema": {
            "type": "object", "properties": {},
            "additionalProperties": False,
        },
        "impl": _tool_scan_window_check,
    },
    "get_explainer": {
        "description": (
            "Return the cached LLM prompt + raw response for an "
            "update. Cheaper than `explain` (no LLM re-run). Returns "
            "`{ok, prompt, raw_response, model, created_at}` or "
            "`{ok: false, error}` when no cache exists."
        ),
        "params_schema": {
            "type": "object",
            "properties": {"update_id": {"type": "string"}},
            "required": ["update_id"],
            "additionalProperties": False,
        },
        "impl": _tool_get_explainer,
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
    "where_is": {
        "description": (
            "Locate a compose service / container by name. Returns "
            "every match with `file`, `line`, `project`, plus a "
            "context excerpt of the YAML around it. Substring + "
            "case-insensitive fallback when no exact hit."
        ),
        "params_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        "impl": _tool_where_is,
    },
    "update_diff": {
        "description": (
            "Compare two analyzed updates by id. Returns "
            "`{ok, a, b, diff: {severity_changed, "
            "breaking_changes_added/removed, summary_a/b}}`."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "a": {"type": "string"},
                "b": {"type": "string"},
            },
            "required": ["a", "b"],
            "additionalProperties": False,
        },
        "impl": _tool_update_diff,
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
    "clear_all_snoozes": {
        "description": (
            "Clear snooze_until on every snoozed update. Returns the "
            "count of rows cleared."
        ),
        "params_schema": {
            "type": "object", "properties": {},
            "additionalProperties": False,
        },
        "impl": _tool_clear_all_snoozes,
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
    "dangling_images": {
        "description": (
            "Walk the local docker image cache and report dangling "
            "`<none>:<none>` images (orphans from pulls). Returns "
            "count + total_bytes/MiB + per-image details. Returns "
            "`ok=false` when docker isn't reachable so a homelab "
            "without docker doesn't crash the agent."
        ),
        "params_schema": {
            "type": "object", "properties": {},
            "additionalProperties": False,
        },
        "impl": _tool_dangling_images,
    },
    "is_snoozed": {
        "description": (
            "Quick lookup whether an update is currently snoozed "
            "(snooze_until in the future). Returns `{snoozed: bool, "
            "snooze_until: str|null}`."
        ),
        "params_schema": {
            "type": "object",
            "properties": {"update_id": {"type": "string"}},
            "required": ["update_id"],
            "additionalProperties": False,
        },
        "impl": _tool_is_snoozed,
    },
    "list_snoozed": {
        "description": (
            "List currently-snoozed updates (snooze_until > now). "
            "Sorted soonest-to-expire first."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "additionalProperties": False,
        },
        "impl": _tool_list_snoozed,
    },
    "snooze_update": {
        "description": (
            "Set or clear a snooze timestamp on an update. Empty / "
            "null `snooze_until` clears the snooze."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "update_id": {"type": "string"},
                "snooze_until": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "null"},
                    ],
                    "description": "ISO 8601 UTC timestamp or null to clear.",
                },
            },
            "required": ["update_id"],
            "additionalProperties": False,
        },
        "impl": _tool_snooze_update,
    },
    "recurring_failures": {
        "description": (
            "List updates that have failed `min_count`+ times (default 2)."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "min_count": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "additionalProperties": False,
        },
        "impl": _tool_recurring_failures,
    },
    "dns_check": {
        "description": (
            "Resolve hostnames (defaults to TLS check URLs' hosts). "
            "Returns failures only."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "hostnames": {
                    "type": "array", "items": {"type": "string"},
                },
            },
            "additionalProperties": False,
        },
        "impl": _tool_dns_check,
    },
    "tls_check_run": {
        "description": (
            "Run TLS cert probes on-demand (defaults to "
            "cfg.tls_check.urls). One result per URL."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "urls": {"type": "array", "items": {"type": "string"}},
                "warn_days": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": False,
        },
        "impl": _tool_tls_check_run,
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

