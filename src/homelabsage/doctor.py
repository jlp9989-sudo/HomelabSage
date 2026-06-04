"""Bundled diagnostic — shared between CLI / `/api/doctor` / MCP.

Reads cfg + db, runs every active probe, returns a structured
report. Each section reports `ok: bool`, `skipped: bool` (when the
feature isn't configured), `findings: list[dict]`, and an
optional `reason`.

Pure orchestrator: every probe is imported lazily so the doctor
can be invoked from contexts where some optional deps are missing.
Never raises; failures land in the report.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import Config, get_active_llm_config
from .db import Database

log = logging.getLogger(__name__)


def _probe_llm(cfg: Config) -> dict[str, Any]:
    from .llm_health import probe as probe_llm
    llm_cfg = get_active_llm_config(cfg)
    verdict = probe_llm(llm_cfg.endpoint, api_key=llm_cfg.api_key, timeout=5.0)
    return {
        "ok": verdict.ok,
        "skipped": False,
        "endpoint": llm_cfg.endpoint,
        "reason": verdict.reason,
        "status_code": verdict.status_code,
    }


def _probe_tls(cfg: Config) -> dict[str, Any]:
    if not cfg.tls_check.urls:
        return {"ok": True, "skipped": True, "reason": "no URLs configured"}
    from .tls_check import check_urls
    findings = check_urls(
        list(cfg.tls_check.urls),
        warn_days=cfg.tls_check.warn_days,
    )
    bad = [f for f in findings if f.severity in ("high", "critical", "medium")]
    return {
        "ok": not bad,
        "skipped": False,
        "findings": [f.to_context() for f in findings],
        "bad_count": len(bad),
    }


def _probe_dns(cfg: Config) -> dict[str, Any]:
    if not cfg.tls_check.urls:
        return {"ok": True, "skipped": True, "reason": "no hostnames derivable"}
    from urllib.parse import urlparse

    from .dns_check import check_hostnames
    hosts: list[str] = []
    for raw in cfg.tls_check.urls:
        p = urlparse(raw if "://" in raw else "https://" + raw)
        if p.hostname:
            hosts.append(p.hostname)
    if not hosts:
        return {"ok": True, "skipped": True, "reason": "no hostnames"}
    findings = check_hostnames(hosts)
    return {
        "ok": not findings,
        "skipped": False,
        "hostnames_checked": len(hosts),
        "findings": [f.to_context() for f in findings],
    }


def _probe_disk(cfg: Config) -> dict[str, Any]:
    if not (cfg.disk_pressure.enabled and cfg.disk_pressure.paths):
        return {"ok": True, "skipped": True, "reason": "not configured"}
    from .disk_pressure import evaluate as eval_disk
    findings = eval_disk(list(cfg.disk_pressure.paths))
    return {
        "ok": not findings,
        "skipped": False,
        "findings": [f.to_context() for f in findings],
    }


def _probe_compose(cfg: Config) -> dict[str, Any]:
    if not cfg.sources.docker.compose_scan_paths:
        return {"ok": True, "skipped": True, "reason": "no scan paths"}
    from .compose_override import scan as scan_overrides
    from .env_perms import scan as scan_env_perms
    overrides = scan_overrides(list(cfg.sources.docker.compose_scan_paths))
    env_perm_findings = scan_env_perms(
        list(cfg.sources.docker.compose_scan_paths),
    )
    return {
        # Override presence is info-only; env-perm findings are actionable.
        "ok": not env_perm_findings,
        "skipped": False,
        "overrides": [o.to_context() for o in overrides],
        "env_perms": [f.to_context() for f in env_perm_findings],
    }


def _probe_audit(cfg: Config, db: Database) -> dict[str, Any]:
    try:
        from .audit import build_report
        report = build_report(cfg, db)
        return {
            "ok": not (
                report.counts_by_severity.get("critical")
                or report.counts_by_severity.get("high")
            ),
            "skipped": False,
            "counts_by_severity": report.counts_by_severity,
            "counts_by_category": report.counts_by_category,
            "healthy": report.healthy,
        }
    except Exception as e:
        log.exception("doctor audit failed")
        return {
            "ok": False,
            "skipped": False,
            "reason": f"audit build failed: {type(e).__name__}: {e}",
        }


def build_report(
    cfg: Config,
    db: Database,
    *,
    skip_llm: bool = False,
) -> dict[str, Any]:
    """Run every active probe; return the structured report.

    `skip_llm` is the only knob — the rest is implicit-on based on
    whether the cfg block is configured.
    """
    sections: dict[str, dict[str, Any]] = {}
    if skip_llm:
        sections["llm"] = {"ok": True, "skipped": True, "reason": "skip_llm flag"}
    else:
        sections["llm"] = _probe_llm(cfg)
    sections["tls"] = _probe_tls(cfg)
    sections["dns"] = _probe_dns(cfg)
    sections["disk"] = _probe_disk(cfg)
    sections["compose"] = _probe_compose(cfg)
    sections["audit"] = _probe_audit(cfg, db)

    # Overall verdict: any non-skipped section that's not ok flips it.
    healthy = all(
        s.get("ok") or s.get("skipped")
        for s in sections.values()
    )
    llm_unreachable = (
        not sections["llm"].get("skipped")
        and not sections["llm"].get("ok")
    )
    return {
        "healthy": healthy,
        "llm_unreachable": llm_unreachable,
        "sections": sections,
    }


__all__ = ["build_report"]
