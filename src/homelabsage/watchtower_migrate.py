"""Watchtower-refugee migration helper.

Watchtower (containrrr/watchtower) was archived on 17 Dec 2025
(github.com/containrrr/watchtower/discussions/2135). The nicholas-fedor
fork exists but the trust deficit left a wedge wide open: that's where
HomelabSage's "reasoned update verdict" lives.

This module detects a running Watchtower container in the user's
Docker stack, parses its `WATCHTOWER_*` env vars + its
`com.centurylinklabs.watchtower.*` per-container labels, and produces a
report the user can use to decide what to migrate where.

The report names every container that Watchtower was managing, the
schedule it ran on, whether `--cleanup` / `--monitor-only` were set,
and which containers had the `enable=true/false` opt-in/out label. No
state is modified — this is read-only inventory + recommendations.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


_WATCHTOWER_IMAGE_RE = re.compile(
    r"^(?:[^/]+/)*(?:containrrr|nicholas-fedor)/watchtower\b", re.IGNORECASE,
)

# `WATCHTOWER_*` env vars Watchtower actually documents. We don't try to
# be exhaustive — capturing the most-used 80 % is enough to give the user
# a meaningful migration starting point.
_KNOWN_ENV = {
    "WATCHTOWER_SCHEDULE",
    "WATCHTOWER_POLL_INTERVAL",
    "WATCHTOWER_CLEANUP",
    "WATCHTOWER_MONITOR_ONLY",
    "WATCHTOWER_NOTIFICATION_URL",
    "WATCHTOWER_NOTIFICATIONS",
    "WATCHTOWER_LABEL_ENABLE",
    "WATCHTOWER_INCLUDE_RESTARTING",
    "WATCHTOWER_INCLUDE_STOPPED",
    "WATCHTOWER_REVIVE_STOPPED",
    "WATCHTOWER_NO_PULL",
    "WATCHTOWER_DEBUG",
    "WATCHTOWER_RUN_ONCE",
}

# Per-container labels Watchtower checks on managed containers.
_LABEL_PREFIX = "com.centurylinklabs.watchtower."


@dataclass
class ContainerVerdict:
    """One row in the migration report — what to do with this container."""

    name: str
    image: str
    watchtower_label_enable: str | None  # 'true' / 'false' / None
    watchtower_monitor_only_label: str | None
    recommendation: str
    rationale: str


@dataclass
class WatchtowerReport:
    """Inventory + recommendations. None when no Watchtower instance found."""

    watchtower_container: str
    watchtower_image: str
    watchtower_args: list[str]
    env: dict[str, str]
    managed_containers: list[ContainerVerdict] = field(default_factory=list)
    summary: str = ""

    def to_markdown(self) -> str:
        """Render the report as a single Markdown block the user can save.

        Tight on purpose — the goal is a 30-second scan that surfaces the
        migration shape. Detailed per-container reasoning is intentionally
        out of scope (the regular analyzer covers that).
        """
        lines: list[str] = [
            "# Watchtower migration plan",
            "",
            f"Found Watchtower instance: **`{self.watchtower_container}`** "
            f"(`{self.watchtower_image}`).",
        ]
        if self.summary:
            lines.append("")
            lines.append(self.summary)

        if self.env:
            lines.append("")
            lines.append("## Watchtower configuration")
            for k, v in sorted(self.env.items()):
                masked = "<redacted>" if "URL" in k or "TOKEN" in k or "PASS" in k else v
                lines.append(f"- `{k}` = `{masked}`")
        if self.watchtower_args:
            lines.append("")
            lines.append("## Watchtower CLI args")
            lines.append("```")
            lines.append(" ".join(shlex.quote(a) for a in self.watchtower_args))
            lines.append("```")

        if self.managed_containers:
            lines.append("")
            lines.append("## Per-container plan")
            for verdict in self.managed_containers:
                lines.append(f"- **`{verdict.name}`** ({verdict.image})")
                lines.append(f"  - Recommendation: {verdict.recommendation}")
                lines.append(f"  - Why: {verdict.rationale}")
        else:
            lines.append("")
            lines.append("_No other containers detected — Watchtower had nothing to manage._")

        lines += [
            "",
            "---",
            "*HomelabSage doesn't auto-update; it analyses what would change "
            "before you apply. Re-run `homelabsage check` and review verdicts "
            "in the dashboard, then apply manually with `docker compose pull && up -d`.*",
        ]
        return "\n".join(lines)


def _classify_container(
    name: str,
    image: str,
    labels: dict[str, str],
    label_enable_mode: bool,
) -> ContainerVerdict:
    """Decide what HomelabSage recommends for one user container."""
    enable = labels.get(f"{_LABEL_PREFIX}enable")
    monitor_only = labels.get(f"{_LABEL_PREFIX}monitor-only")

    if enable and enable.lower() == "false":
        rec = "leave out of HomelabSage scan"
        why = (
            f"the user marked {name!r} with `watchtower.enable=false`; "
            "preserve that exclusion via `sources.docker.skip:` regex."
        )
    elif label_enable_mode and (not enable or enable.lower() != "true"):
        rec = "leave out of HomelabSage scan"
        why = (
            f"Watchtower ran in `--label-enable` mode and {name!r} lacked "
            "an explicit `watchtower.enable=true`. Mirror the exclusion."
        )
    elif monitor_only and monitor_only.lower() == "true":
        rec = "scan + analyse only (no apply)"
        why = (
            f"{name!r} was Watchtower-monitored; HomelabSage analyses "
            "every detected update so the analyser keeps that role."
        )
    else:
        rec = "include in HomelabSage scan + review verdicts before applying"
        why = (
            f"{name!r} was on Watchtower's auto-update path; HomelabSage "
            "replaces that with reasoned verdicts you approve per update."
        )
    return ContainerVerdict(
        name=name,
        image=image,
        watchtower_label_enable=enable,
        watchtower_monitor_only_label=monitor_only,
        recommendation=rec,
        rationale=why,
    )


def detect(containers: list[Any]) -> WatchtowerReport | None:
    """Inspect the given Docker SDK Container list, return a migration report.

    `containers` is whatever `client.containers.list(all=True)` returned
    — duck-typed so tests can supply lightweight stand-ins. Returns
    `None` when no Watchtower image is found among the running set.
    """
    wt_container = None
    for c in containers:
        try:
            image_ref = (c.image.tags[0] if c.image.tags else "") or ""
        except Exception:
            image_ref = ""
        if image_ref and _WATCHTOWER_IMAGE_RE.search(image_ref):
            wt_container = c
            break
    if wt_container is None:
        return None

    cfg = wt_container.attrs.get("Config", {}) or {}
    env_list = cfg.get("Env") or []
    env: dict[str, str] = {}
    for line in env_list:
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.startswith("WATCHTOWER_") and k in _KNOWN_ENV:
            env[k] = v
    cmd = cfg.get("Cmd") or []

    label_enable_mode = (
        env.get("WATCHTOWER_LABEL_ENABLE", "").lower() in ("true", "1", "yes")
        or "--label-enable" in cmd
    )

    image_ref = (
        wt_container.image.tags[0] if wt_container.image.tags else "watchtower"
    )

    verdicts: list[ContainerVerdict] = []
    for c in containers:
        if c is wt_container:
            continue
        try:
            image = c.image.tags[0] if c.image.tags else ""
            labels = (c.attrs.get("Config", {}) or {}).get("Labels") or {}
        except Exception:
            image, labels = "", {}
        verdicts.append(_classify_container(c.name, image, labels, label_enable_mode))

    schedule = env.get("WATCHTOWER_SCHEDULE") or env.get("WATCHTOWER_POLL_INTERVAL")
    summary = (
        f"Watchtower was running on `{schedule}` cadence."
        if schedule else
        "Watchtower had no explicit schedule — was likely on the default 24h poll."
    )

    return WatchtowerReport(
        watchtower_container=wt_container.name,
        watchtower_image=image_ref,
        watchtower_args=list(cmd),
        env=env,
        managed_containers=verdicts,
        summary=summary,
    )
