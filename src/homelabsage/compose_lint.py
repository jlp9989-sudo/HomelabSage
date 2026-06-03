"""Compose-file linter — best-practice findings consumed by the auditor.

A "second opinion" column to the analyzer's per-image verdict: even if
the image itself is healthy, the way it's wired in compose can be the
real risk. This module emits one `ComposeLintFinding` per smelly pattern:

  - `links_deprecated`     — `links:` removed in compose v2+; long
                              dead but Unraid CA templates still ship it.
  - `latest_tag`           — `image: foo:latest` (or no tag at all,
                              which equals `:latest`) on a critical
                              service — repro hell when something
                              regresses upstream.
  - `no_healthcheck`       — no `healthcheck:` block AND the image isn't
                              one of the "obviously stateless" excludes
                              (sidecar networks, init containers).
  - `no_restart`           — no `restart:` policy — container won't
                              come back on reboot.
  - `privileged_no_reason` — `privileged: true` without `cap_add:` ↔
                              probably an old "I just want it to work"
                              decision worth revisiting.
  - `host_network`         — `network_mode: host` — bypasses every
                              isolation guarantee. Often necessary
                              (Plex, Unifi, mdns); never silent.
  - `bind_mount_to_appdata_with_chmod_777` — heuristic only — flags
                              binds whose path ends with `/data` /
                              `/config` AND no `user:` set, which is
                              the LSIO PUID/PGID gotcha.

The linter never modifies files. It returns findings the auditor renders.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .compose import _COMPOSE_FILENAMES, _iter_compose_files

log = logging.getLogger(__name__)


@dataclass
class ComposeLintFinding:
    """One smell, one file, one service."""

    severity: str          # info | medium | high
    rule: str              # short tag — matches the categories above
    file: Path
    service: str           # service name as declared in compose
    detail: str            # human-readable one-liner

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "rule": self.rule,
            "file": str(self.file),
            "service": self.service,
            "detail": self.detail,
        }


@dataclass
class ComposeLintReport:
    findings: list[ComposeLintFinding] = field(default_factory=list)

    def by_severity(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for f in self.findings:
            c[f.severity] = c.get(f.severity, 0) + 1
        return c

    def by_rule(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for f in self.findings:
            c[f.rule] = c.get(f.rule, 0) + 1
        return c


# Services whose names match this list are exempt from the no_healthcheck
# rule — they're typically stateless sidecars or init helpers where a
# healthcheck is noise. Override per-call.
_DEFAULT_HEALTHCHECK_EXEMPT: frozenset[str] = frozenset({
    "init", "migrate", "setup", "wait-for", "tailscale",
})


def _image_has_explicit_tag(image: str) -> bool:
    """`foo:1.2.3` or `foo@sha256:...` → explicit. `foo` → implicit latest."""
    if not image:
        return False
    bare = image.split("/")[-1]
    return ":" in bare or "@sha256:" in image


def _image_uses_latest(image: str) -> bool:
    if not image:
        return False
    return image.endswith(":latest") or not _image_has_explicit_tag(image)


def _lint_service(
    file: Path, service_name: str, body: dict,
    *, healthcheck_exempt: frozenset[str],
) -> list[ComposeLintFinding]:
    findings: list[ComposeLintFinding] = []
    image = body.get("image") or ""
    build = body.get("build")

    # 1. links: deprecated since compose v2 (2018)
    if "links" in body:
        findings.append(ComposeLintFinding(
            severity="medium", rule="links_deprecated",
            file=file, service=service_name,
            detail=(
                "`links:` was removed in compose v2 (2018). "
                "Replace with `depends_on:` + a shared network."
            ),
        ))

    # 2. :latest tag or implicit-latest
    if image and _image_uses_latest(image):
        findings.append(ComposeLintFinding(
            severity="medium", rule="latest_tag",
            file=file, service=service_name,
            detail=(
                f"Image `{image}` uses `:latest` (or no tag). "
                f"Pin to a specific version so rollbacks are possible."
            ),
        ))

    # 3. no healthcheck
    if not body.get("healthcheck") and service_name not in healthcheck_exempt \
            and (image or build):
        findings.append(ComposeLintFinding(
            severity="info", rule="no_healthcheck",
            file=file, service=service_name,
            detail=(
                "No `healthcheck:` block. Docker reports the container "
                "as running even when the app inside has hung."
            ),
        ))

    # 4. no restart policy
    if "restart" not in body and "deploy" not in body:
        findings.append(ComposeLintFinding(
            severity="info", rule="no_restart",
            file=file, service=service_name,
            detail=(
                "No `restart:` policy. Container won't come back after "
                "the host reboots."
            ),
        ))

    # 5. privileged without cap_add (suggests it was set when the user
    # didn't yet know about capabilities)
    if body.get("privileged") is True and not body.get("cap_add"):
        findings.append(ComposeLintFinding(
            severity="high", rule="privileged_no_reason",
            file=file, service=service_name,
            detail=(
                "`privileged: true` without any `cap_add:` — replace "
                "with the minimum capabilities the container actually needs."
            ),
        ))

    # 6. network_mode: host
    nm = body.get("network_mode")
    if isinstance(nm, str) and nm.lower() == "host":
        findings.append(ComposeLintFinding(
            severity="info", rule="host_network",
            file=file, service=service_name,
            detail=(
                "`network_mode: host` bypasses isolation. Often needed "
                "for mDNS / Plex / Unifi — keep if intentional."
            ),
        ))

    # 7. LSIO PUID/PGID gotcha — binds to /data or /config without user:
    volumes = body.get("volumes") or []
    if isinstance(volumes, list) and "user" not in body:
        for v in volumes:
            if not isinstance(v, str):
                continue
            # `host:container[:mode]`
            parts = v.split(":")
            if len(parts) < 2:
                continue
            ctr_path = parts[1]
            if ctr_path.rstrip("/").endswith(("/data", "/config")):
                findings.append(ComposeLintFinding(
                    severity="info", rule="bind_no_user",
                    file=file, service=service_name,
                    detail=(
                        f"Bind to `{ctr_path}` without `user:` — LSIO-style "
                        "images expect PUID/PGID env vars; non-LSIO images "
                        "may write as root and break the host mount."
                    ),
                ))
                break

    return findings


def lint_compose_file(
    file: Path,
    *,
    healthcheck_exempt: frozenset[str] | None = None,
) -> list[ComposeLintFinding]:
    """Parse one compose file and return findings.

    Returns an empty list on any parse failure — same contract as
    `compose.parse_compose_file`. The auditor calls this best-effort.
    """
    exempt = healthcheck_exempt or _DEFAULT_HEALTHCHECK_EXEMPT
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8", errors="replace"))
    except (OSError, yaml.YAMLError) as e:
        log.warning("compose_lint: cannot parse %s: %s", file, e)
        return []
    if not isinstance(data, dict):
        return []
    services = data.get("services")
    if not isinstance(services, dict):
        return []
    findings: list[ComposeLintFinding] = []
    for svc_name, body in services.items():
        if not isinstance(body, dict):
            continue
        findings.extend(_lint_service(
            file, str(svc_name), body, healthcheck_exempt=exempt,
        ))
    return findings


def lint_paths(
    roots: Iterable[str | Path],
    *,
    healthcheck_exempt: frozenset[str] | None = None,
) -> ComposeLintReport:
    """Walk every compose file under `roots` and aggregate findings."""
    report = ComposeLintReport()
    for path in _iter_compose_files(roots):
        report.findings.extend(lint_compose_file(
            path, healthcheck_exempt=healthcheck_exempt,
        ))
    return report


__all__ = [
    "ComposeLintFinding",
    "ComposeLintReport",
    "lint_compose_file",
    "lint_paths",
    "_COMPOSE_FILENAMES",   # re-export so tests can use it without depending on compose.py
]
