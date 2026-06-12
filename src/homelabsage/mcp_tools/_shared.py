"""Helpers shared by every MCP tool module — update shaping + sync-coro bridge."""

from __future__ import annotations

import asyncio
import logging
import threading

log = logging.getLogger(__name__)


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
