"""`/metrics` — Prometheus text-exposition endpoint.

No 3rd-party dep — we emit the OpenMetrics text format directly. The
counters HomelabSage exposes:

  homelabsage_updates_total{status="new|analyzed|applied|dismissed|failed"}
  homelabsage_updates_severity{severity="critical|high|medium|info"}
  homelabsage_llm_calls_total{provider, model, succeeded}
  homelabsage_heartbeats_total{ok}
  homelabsage_pending_dispatches
  homelabsage_audit_findings{severity}
  homelabsage_interview_questions{status}

These are mostly snapshot gauges, not real counters — we read the
current SQLite state per request. That's fine for Prometheus scrape
intervals (default 15 s) but won't catch sub-scrape flap. The pattern
matches how `node_exporter` exposes filesystem usage too.

Auth-bypassed like `/healthz` and `/widget/*` — Prometheus scrapers
don't carry credentials by default and the data we expose is
non-sensitive aggregate counts.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from .. import __version__
from ..config import Config
from ..db import Database
from ..models import InterviewStatus, UpdateStatus

_HELP_LINES = (
    "# HELP homelabsage_info Build info\n"
    "# TYPE homelabsage_info gauge\n"
)


def _esc_label(v: str) -> str:
    """Escape a label value per the Prometheus text format spec."""
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_metrics(cfg: Config, db: Database) -> str:
    lines: list[str] = []
    lines.append(_HELP_LINES.rstrip())
    lines.append(
        f'homelabsage_info{{version="{_esc_label(__version__)}"}} 1'
    )

    # Updates by status
    by_status = {s.value: 0 for s in UpdateStatus}
    by_severity: dict[str, int] = {
        "critical": 0, "high": 0, "medium": 0, "info": 0,
    }
    for it in db.list(limit=2000):
        by_status[it.status.value] = by_status.get(it.status.value, 0) + 1
        if it.analysis:
            by_severity[it.analysis.severity.value] = (
                by_severity.get(it.analysis.severity.value, 0) + 1
            )
    lines.append("\n# HELP homelabsage_updates_total Updates by status")
    lines.append("# TYPE homelabsage_updates_total gauge")
    for status_name, count in by_status.items():
        lines.append(
            f'homelabsage_updates_total{{status="{_esc_label(status_name)}"}} '
            f'{count}'
        )
    lines.append("\n# HELP homelabsage_updates_severity Updates by severity")
    lines.append("# TYPE homelabsage_updates_severity gauge")
    for sev, count in by_severity.items():
        lines.append(
            f'homelabsage_updates_severity{{severity="{_esc_label(sev)}"}} '
            f'{count}'
        )

    # Pending dispatches
    pending = len(db.list_pending_dispatches())
    lines.append("\n# HELP homelabsage_pending_dispatches Push notifications queued")
    lines.append("# TYPE homelabsage_pending_dispatches gauge")
    lines.append(f"homelabsage_pending_dispatches {pending}")

    # Interview questions
    lines.append(
        "\n# HELP homelabsage_interview_questions Interview questions by status"
    )
    lines.append("# TYPE homelabsage_interview_questions gauge")
    for status in InterviewStatus:
        n = db.count_interview_questions(status)
        lines.append(
            f'homelabsage_interview_questions{{status="{_esc_label(status.value)}"}} '
            f'{n}'
        )

    # Heartbeats — 24h success/failure
    hb = db.heartbeat_summary(hours=24)
    lines.append("\n# HELP homelabsage_heartbeats_total Heartbeat pings 24h")
    lines.append("# TYPE homelabsage_heartbeats_total counter")
    lines.append(
        f'homelabsage_heartbeats_total{{ok="true"}} {hb.get("succeeded") or 0}'
    )
    lines.append(
        f'homelabsage_heartbeats_total{{ok="false"}} {hb.get("failed") or 0}'
    )

    # LLM usage 30d
    summary = db.usage_summary(days=30)
    lines.append(
        "\n# HELP homelabsage_llm_tokens LLM token usage by provider/model 30d"
    )
    lines.append("# TYPE homelabsage_llm_tokens counter")
    for row in summary.get("by_provider_model", []):
        provider = _esc_label(row.get("provider") or "")
        model = _esc_label(row.get("model") or "")
        tin = int(row.get("tokens_in") or 0)
        tout = int(row.get("tokens_out") or 0)
        labels = f'provider="{provider}",model="{model}"'
        lines.append(f'homelabsage_llm_tokens{{{labels},direction="in"}} {tin}')
        lines.append(f'homelabsage_llm_tokens{{{labels},direction="out"}} {tout}')

    return "\n".join(lines) + "\n"


def register_metrics_route(app: FastAPI, cfg: Config, db: Database) -> None:
    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(
            _render_metrics(cfg, db),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )
