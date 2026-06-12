"""Update-row tools: list/get/search, status, notes, stars, snooze, diffs, explainers."""

from __future__ import annotations

import logging

from ..config import Config
from ..db import Database
from ..models import UpdateStatus
from ._shared import _full_update, _summarise_update

log = logging.getLogger(__name__)


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
        from ..health_check import queue_for_update
        queue_for_update(
            db, update_id=update_id,
            container_name=item.update.subject, cfg=cfg.health_check,
        )
    return {"ok": True, "update_id": update_id, "status": status.value}


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
        from .._time import parse_iso
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
    raw = db.get_snooze(update_id)
    if not raw:
        return {"snoozed": False, "snooze_until": None}
    from .._time import parse_iso, utcnow
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
    limit = max(1, min(int(params.get("limit") or 200), 500))
    rows = db.list_snoozed(limit=limit)
    return {"count": len(rows), "items": rows}


def _tool_clear_all_snoozes(_cfg: Config, db: Database, _params: dict) -> dict:
    """Clear `snooze_until` on every snoozed update."""
    cleared = db.clear_all_snoozes()
    return {"ok": True, "cleared": cleared}


def _tool_purge_old_updates(_cfg: Config, db: Database, params: dict) -> dict:
    """Drop APPLIED+DISMISSED updates older than `older_than_days`."""
    days = max(1, int(params.get("older_than_days") or 90))
    dry = bool(params.get("dry_run") or False)
    n = db.purge_old_updates(older_than_days=days, dry_run=dry)
    return {"ok": True, "purged": n, "older_than_days": days, "dry_run": dry}


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


def _tool_recurring_failures(_cfg: Config, db: Database, params: dict) -> dict:
    """List updates that have failed N+ times."""
    min_count = max(1, int(params.get("min_count") or 2))
    limit = max(1, min(int(params.get("limit") or 50), 500))
    rows = db.list_recurring_failures(min_count=min_count, limit=limit)
    return {"count": len(rows), "items": rows}


def _tool_explain(_cfg: Config, db: Database, params: dict) -> dict:
    """Return the stored prompt + raw LLM response for an update.

    Same data the `/updates/<id>/explain` web page renders. Useful for
    an agent that wants to audit a verdict ("show me what the model
    actually said") without scraping HTML.
    """
    update_id = params.get("update_id")
    if not update_id:
        raise ValueError("update_id is required")
    row = db.get_explainer(update_id)
    if row is None:
        raise ValueError(f"no explainer for update_id={update_id!r}")
    return row


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
    row = db.get_explainer(update_id)
    if row is None:
        return {"ok": False, "error": f"no explainer for {update_id}"}
    return {"ok": True, **row}


def _tool_history_csv(_cfg: Config, db: Database, params: dict) -> dict:
    """Dump the update history as CSV in the response body.

    For agents that want to ingest the audit log directly. Output is
    capped at `limit` rows (default 1000) so a busy DB doesn't blow
    the JSON-RPC envelope.
    """
    from ..history import dump_to_string
    limit = int(params.get("limit") or 1000)
    limit = max(1, min(limit, 5000))
    body = dump_to_string(db, limit=limit)
    return {"csv": body, "rows": body.count("\n") - 1 if body else 0}


def _tool_rollback_recipe(cfg: Config, db: Database, params: dict) -> dict:
    """Return the rollback recipe (CLI + compose forms) for an update."""
    update_id = params.get("update_id")
    if not update_id:
        raise ValueError("update_id is required")
    item = db.get(update_id)
    if item is None:
        raise ValueError(f"no update with id={update_id!r}")
    from ..rollback import build_recipe, render_markdown
    # Build the compose graph so the recipe surfaces the docker-compose
    # form + cascade warnings. Empty when no compose_scan_paths are
    # configured — the recipe still renders the CLI form.
    graph = None
    if cfg.sources.docker.compose_scan_paths:
        try:
            from ..compose import build_graph as build_compose_graph
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



TOOLS: dict[str, dict] = {
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
}
