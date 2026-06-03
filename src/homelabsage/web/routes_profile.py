"""`/profile` — self-discoverable summary of what's enabled.

Useful when the user has been editing settings via the UI for months
and wants to know "what's actually turned on?" without spelunking
through config.yaml. Also pairs with `/audit` — `/profile` answers
"what could fire?", `/audit` answers "what did fire?".

Pure read-only. Auth-gated like the rest of the dashboard.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from jinja2 import Environment

from .. import __version__
from ..config import Config


def _signal(label: str, enabled: bool, note: str = "") -> dict:
    return {"label": label, "enabled": enabled, "note": note}


def _build_signals(cfg: Config) -> list[dict]:
    """Flatten every config block into a list of `{label, enabled, note}`
    rows. Keeps the template trivial."""
    sources = cfg.sources
    outputs = cfg.outputs
    rows: list[dict] = []

    # Sources
    rows.append(_signal("docker plugin", sources.docker.enabled,
                        f"socket={sources.docker.socket}"))
    rows.append(_signal("homeassistant plugin", sources.homeassistant.enabled))
    rows.append(_signal("fedora plugin", sources.fedora.enabled,
                        f"host={sources.fedora.host}" if sources.fedora.enabled else ""))
    rows.append(_signal("github_watched plugin", sources.github_watched.enabled))

    # Per-detector knobs (docker)
    rows.append(_signal("CVE scan (trivy/grype)", sources.docker.cve_scan))
    rows.append(_signal("Repo-health classifier", sources.docker.repo_health))
    rows.append(_signal("Releases-diff", sources.docker.releases_diff))
    rows.append(_signal("Find alternatives", sources.docker.find_alternatives))
    rows.append(_signal("Floating-tag tracker", sources.docker.track_floating_tags))
    rows.append(_signal("Image-size growth", sources.docker.image_size_growth_detect))
    rows.append(_signal("Release-cadence", sources.docker.release_cadence))
    rows.append(_signal("Sidecar discovery", sources.docker.detect_sidecars))
    rows.append(_signal("Resource-limit absence",
                        sources.docker.detect_resource_limits))
    rows.append(_signal("Compose scan paths",
                        bool(sources.docker.compose_scan_paths),
                        f"{len(sources.docker.compose_scan_paths)} configured"))

    # Outputs
    rows.append(_signal("Notion", outputs.notion.enabled))
    rows.append(_signal("Telegram", outputs.telegram.enabled,
                        f"min={outputs.telegram.min_severity}"))
    rows.append(_signal("Discord", outputs.discord.enabled,
                        f"min={outputs.discord.min_severity}"))
    rows.append(_signal("Ntfy", outputs.ntfy.enabled,
                        f"min={outputs.ntfy.min_severity}"))
    rows.append(_signal("Gotify", outputs.gotify.enabled,
                        f"min={outputs.gotify.min_severity}"))
    rows.append(_signal("Apprise", outputs.apprise.enabled,
                        f"{len(outputs.apprise.urls)} URL(s)"))
    rows.append(_signal("SMTP", outputs.smtp.enabled,
                        outputs.smtp.host if outputs.smtp.enabled else ""))
    rows.append(_signal("Severity batching", outputs.batching.enabled,
                        f"below={outputs.batching.below_severity}"))

    # Engine gates / runtime
    rows.append(_signal("Parity gate", cfg.parity_gate.enabled))
    rows.append(_signal("Weekly digest", cfg.digest.enabled,
                        cfg.digest.cron if cfg.digest.enabled else ""))
    rows.append(_signal("Backup-staleness probes", cfg.backup_health.enabled,
                        f"{len(cfg.backup_health.repos)} repo(s)"))
    rows.append(_signal("Post-update health-check", cfg.health_check.enabled,
                        f"grace={cfg.health_check.grace_minutes}m"))
    rows.append(_signal("Log-anomaly detector", cfg.log_anomaly.enabled,
                        f"σ={cfg.log_anomaly.sigma_threshold}"))
    rows.append(_signal("Compose linter", cfg.compose_lint.enabled))
    rows.append(_signal("Tag-lag detector", cfg.tag_lag.enabled,
                        f"warn={cfg.tag_lag.warn_after_days}d"))
    rows.append(_signal("Image-pins", cfg.image_pins.enabled,
                        f"{len(cfg.image_pins.pins)} pin(s)"))
    rows.append(_signal("Auto-apply whitelist", cfg.auto_apply.enabled,
                        f"{len(cfg.auto_apply.allowlist)} allow-list entries, "
                        f"ceiling={cfg.auto_apply.max_severity}"))
    rows.append(_signal("Pre-flight gate", cfg.web.preflight_gate))
    rows.append(_signal("i18n", cfg.i18n.lang != "en", f"lang={cfg.i18n.lang}"))
    rows.append(_signal(
        "Scheduler", cfg.scheduler.enabled,
        f"{cfg.scheduler.cron} ({cfg.scheduler.timezone})"
        if cfg.scheduler.enabled else "",
    ))

    return rows


def register_profile_route(app: FastAPI, cfg: Config, env: Environment) -> None:
    @app.get("/profile", response_class=HTMLResponse)
    async def profile() -> HTMLResponse:
        rows = _build_signals(cfg)
        # `version` etc. are exposed so the template doesn't depend on the
        # caller knowing which signals are interesting — it can read more
        # from the cfg if needed.
        tmpl = env.get_template("profile.html")
        return HTMLResponse(tmpl.render(
            rows=rows, version=__version__,
            llm_active=cfg.llm_active, llm_provider=cfg.llm.provider,
        ))

    @app.get("/api/profile")
    async def profile_api() -> dict:
        return {
            "version": __version__,
            "llm_active": cfg.llm_active,
            "llm_provider": cfg.llm.provider,
            "rows": _build_signals(cfg),
        }
