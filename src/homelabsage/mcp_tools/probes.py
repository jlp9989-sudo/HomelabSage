"""Host/infra probes: doctor, dns, disk, tls, compose graph, where-is, autoconfig."""

from __future__ import annotations

import logging

from .. import __version__
from ..config import Config
from ..db import Database

log = logging.getLogger(__name__)


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


def _tool_doctor(cfg: Config, db: Database, params: dict) -> dict:
    """Run the bundled diagnostic (LLM / TLS / DNS / disk / compose / audit).

    Same data as `homelabsage doctor` CLI and `GET /api/doctor`,
    surfaced for agents. Set `skip_llm=true` for offline runs.
    """
    from ..doctor import build_report as build_doctor_report
    skip_llm = bool(params.get("skip_llm") or False)
    return build_doctor_report(cfg, db, skip_llm=skip_llm)


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
    from ..dns_check import check_hostnames
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
    from ..disk_pressure import evaluate as eval_disk
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


def _tool_tls_check_run(cfg: Config, _db: Database, params: dict) -> dict:
    """Run TLS probes on-demand.

    Uses `params.urls` first, falls back to `cfg.tls_check.urls`.
    Returns one entry per URL — failures included so the caller
    sees the full picture.
    """
    from ..tls_check import check_urls
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


def _tool_compose_overrides(cfg: Config, _db: Database, params: dict) -> dict:
    """List `docker-compose.override.yml` files in the configured scan paths.

    Read-only inventory — the runtime graph diverges from the analysed
    base file wherever an override exists.
    """
    from ..compose_override import scan as scan_overrides
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


def _tool_compose_graph_mermaid(cfg: Config, _db: Database, params: dict) -> dict:
    """Render the compose dependency graph as Mermaid.

    `paths` param overrides the configured scan paths. Returns
    `{mermaid: "...", service_count: N, edge_count: M}`.
    """
    from ..cli.compose_graph import render_mermaid
    from ..compose import build_graph
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
    from ..compose import build_graph
    from ..where_is import find as find_where
    graph = build_graph(roots)
    hits = find_where(graph, name)
    return {
        "count": len(hits),
        "items": [h.to_context() for h in hits],
    }


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
    from ..dangling_images import find_dangling
    findings = find_dangling(images)
    total_bytes = sum(f.size_bytes for f in findings)
    return {
        "ok": True,
        "count": len(findings),
        "total_bytes": total_bytes,
        "total_mib": round(total_bytes / (1024 ** 2), 1),
        "items": [f.to_context() for f in findings],
    }


def _tool_scan_window_check(cfg: Config, _db: Database, _params: dict) -> dict:
    """Would a scan start right now, or is the scan-window gate blocking?

    Returns `{enabled, blocked, reason}`. Useful for agents that
    want to decide "trigger /run now" vs "wait until the window
    clears".
    """
    from ..scan_window import is_scan_blocked
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


def _tool_autoconfig(cfg: Config, _db: Database, _params: dict) -> dict:
    """Detect config values from this host and return evidence-carrying
    proposals (compose paths, disk paths, parity gate, cheap detectors,
    TLS hosts). Read-only — apply via the CLI `autoconfig --apply` or the
    `/autoconfig` page so a human reviews first.
    """
    from ..autoconfig import build_proposals
    proposals = build_proposals(cfg)
    return {"count": len(proposals),
            "proposals": [p.to_dict() for p in proposals]}



TOOLS: dict[str, dict] = {
    "health": {
        "description": "Probe HomelabSage. Returns version and the names of every enabled source/output.",
        "params_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "impl": _tool_health,
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
    "autoconfig": {
        "description": (
            "Detect config values from this host (Docker socket + "
            "filesystem probes) and return evidence-carrying proposals: "
            "compose scan paths, disk-pressure paths, parity gate, "
            "image-size/fit detectors, TLS hostnames. Read-only — a "
            "human applies via CLI --apply or the /autoconfig page."
        ),
        "params_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "impl": _tool_autoconfig,
    },
}
