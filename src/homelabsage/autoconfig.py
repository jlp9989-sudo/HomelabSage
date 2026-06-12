"""Auto-configuration — detect config values from the host, propose, apply.

The settings tree has ~38 blocks, and several detectors ship dormant not
because they're optional in spirit but because they need lists only the
user could type: compose paths, disk paths, parity. Yet everything in
those lists is discoverable from the Docker socket and the host:

  - `com.docker.compose.project.config_files` labels carry the exact
    compose file that launched every compose-managed container.
  - `docker info` knows the Docker root dir (where pulls land).
  - `/proc/mdstat` + `/boot/config` give away Unraid / mdraid parity.
  - Traefik router labels carry the public hostnames worth TLS-probing.

This module turns that into the flow the product wants everywhere:
**the machine does the work, the user reviews.** `build_proposals()`
inspects and returns evidence-carrying proposals; nothing is written
until the user applies them (CLI `homelabsage autoconfig --apply`, the
`/autoconfig` page, or per-proposal selection). Apply writes to the same
`config.user.yaml` overlay as the settings UI — full-Config Pydantic
validation first, atomic write, and the override badges show provenance.

Principles:
  - Read-only by default. Proposals are data, not side effects.
  - Additive only: list proposals MERGE with what the user already wrote
    (their entries always survive); scalars are only proposed when they
    differ from the current effective value.
  - Best-effort: no Docker socket / no labels / no probes → fewer
    proposals, never an exception.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Proposal:
    """One detected config change, carrying its own justification.

    `value` is the complete new value for `dotted` (already merged with
    the current one for lists) so apply is a plain overlay write with no
    extra logic.
    """

    dotted: str       # e.g. "sources.docker.compose_scan_paths"
    value: Any
    current: Any
    evidence: str     # human-readable "why", shown in CLI/UI for review

    def to_dict(self) -> dict[str, Any]:
        return {
            "dotted": self.dotted,
            "value": self.value,
            "current": self.current,
            "evidence": self.evidence,
        }


# ─── individual detections ────────────────────────────────────────────────


def _collapse_dirs(dirs: list[str]) -> list[str]:
    """Collapse sibling stack dirs to their shared parent.

    Dockge/Portainer layouts put one dir per stack under a common root
    (`/opt/stacks/mealie`, `/opt/stacks/immich`, …). Listing the root
    keeps the config short and future stacks get scanned for free — the
    compose walker is depth-bounded so a broad root is safe. Dirs that
    share a parent with at least one sibling collapse; loners stay as-is.
    """
    unique = sorted(set(dirs))
    by_parent: dict[str, list[str]] = {}
    for d in unique:
        by_parent.setdefault(str(Path(d).parent), []).append(d)
    out: set[str] = set()
    for parent, children in by_parent.items():
        if len(children) >= 2 and parent not in ("/", "."):
            out.add(parent)
        else:
            out.update(children)
    return sorted(out)


def detect_compose_paths(containers: list[Any]) -> tuple[list[str], int]:
    """Compose file dirs from container labels. Returns (dirs, n_sources).

    `com.docker.compose.project.config_files` is set by compose itself on
    every container it manages — comma-separated absolute paths of the
    compose files used. We take each file's directory and collapse
    siblings to their common stack root.
    """
    dirs: list[str] = []
    seen_containers = 0
    for c in containers:
        labels = (c.attrs.get("Config", {}) or {}).get("Labels") or {}
        raw = labels.get("com.docker.compose.project.config_files", "")
        if not raw:
            continue
        seen_containers += 1
        for part in raw.split(","):
            part = part.strip()
            if part:
                dirs.append(str(Path(part).parent))
    return _collapse_dirs(dirs), seen_containers


_HOST_RULE_RE = re.compile(r"Host\(\s*`([^`]+)`\s*\)")


def detect_traefik_hosts(containers: list[Any]) -> list[str]:
    """Public hostnames from Traefik router rules (`Host(`x`)`)."""
    hosts: set[str] = set()
    for c in containers:
        labels = (c.attrs.get("Config", {}) or {}).get("Labels") or {}
        for k, v in labels.items():
            if k.startswith("traefik.http.routers.") and isinstance(v, str):
                hosts.update(_HOST_RULE_RE.findall(v))
    return sorted(hosts)


def detect_parity_capable() -> str | None:
    """Evidence string when this host can run a parity check, else None."""
    if Path("/boot/config").is_dir():
        return "Unraid detected (/boot/config present)"
    if Path("/proc/mdstat").is_file():
        try:
            text = Path("/proc/mdstat").read_text()
        except OSError:
            return None
        if any(line.startswith("md") for line in text.splitlines()):
            return "mdraid array detected in /proc/mdstat"
    return None


def _docker_root_dir(client: Any) -> str | None:
    try:
        info = client.info()
    except Exception as e:
        log.debug("autoconfig: docker info failed: %s", e)
        return None
    root = info.get("DockerRootDir")
    return str(root) if root else None


def _merged_list(current: list[str], detected: list[str]) -> list[str]:
    """Union preserving the user's entries first, then new ones sorted."""
    new = [d for d in sorted(set(detected)) if d not in current]
    return list(current) + new


# ─── orchestrator ─────────────────────────────────────────────────────────


def build_proposals(
    cfg: Config, *, docker_client: Any | None = None,
) -> list[Proposal]:
    """Inspect the host and return every config change worth reviewing.

    `docker_client` is injectable for tests; None means "connect to the
    local socket, and skip docker-derived detections if that fails".
    Detections that match the current effective config produce nothing —
    an empty result means "your config already reflects this host".
    """
    proposals: list[Proposal] = []

    containers: list[Any] = []
    if docker_client is None:
        try:
            import docker
            docker_client = docker.from_env()
        except Exception as e:
            log.debug("autoconfig: no docker socket: %s", e)
            docker_client = None
    if docker_client is not None:
        try:
            containers = list(docker_client.containers.list(all=True))
        except Exception as e:
            log.debug("autoconfig: container list failed: %s", e)

    # 1. compose_scan_paths ← compose labels
    if containers:
        detected, n = detect_compose_paths(containers)
        current = list(cfg.sources.docker.compose_scan_paths or [])
        merged = _merged_list(current, detected)
        if detected and merged != current:
            new_only = [d for d in merged if d not in current]
            proposals.append(Proposal(
                dotted="sources.docker.compose_scan_paths",
                value=merged, current=current,
                evidence=(
                    f"compose labels on {n} container(s) point at "
                    f"{', '.join(new_only)} — enables the dependency-cascade "
                    f"and compose-lint detectors"
                ),
            ))

    # 2. disk_pressure.paths ← docker root dir (where pulls actually land)
    if docker_client is not None:
        root = _docker_root_dir(docker_client)
        current = list(cfg.disk_pressure.paths or [])
        if root and root not in current:
            proposals.append(Proposal(
                dotted="disk_pressure.paths",
                value=_merged_list(current, [root]), current=current,
                evidence=(
                    f"docker info reports images land in {root} — probing it "
                    f"powers the disk-pressure audit and the will-it-fit check"
                ),
            ))

    # 3. parity_gate ← Unraid / mdraid present
    parity_evidence = detect_parity_capable()
    if parity_evidence and not cfg.parity_gate.enabled:
        proposals.append(Proposal(
            dotted="parity_gate.enabled", value=True, current=False,
            evidence=(
                f"{parity_evidence} — gating pushes during parity checks "
                f"avoids notifying mid-resync; queued items auto-flush after"
            ),
        ))

    # 4. size-growth + will-it-fit ← cheap once a disk path is known.
    # Only proposed when a Docker socket exists — recommending docker
    # detectors on a docker-less host would be noise.
    if docker_client is not None:
        will_have_disk_paths = bool(cfg.disk_pressure.paths) or any(
            p.dotted == "disk_pressure.paths" for p in proposals
        )
        if not cfg.sources.docker.image_size_growth_detect:
            proposals.append(Proposal(
                dotted="sources.docker.image_size_growth_detect",
                value=True, current=False,
                evidence=(
                    "one Docker Hub manifest request per update flags bloated "
                    "images (e.g. 80 MB → 600 MB) before you pull them"
                ),
            ))
        if will_have_disk_paths and not cfg.sources.docker.image_fit_check:
            proposals.append(Proposal(
                dotted="sources.docker.image_fit_check",
                value=True, current=False,
                evidence=(
                    "with a disk path configured, the analyzer can warn when a "
                    "pull would not fit in the remaining free space"
                ),
            ))

    # 5. tls_check.urls ← Traefik router hostnames
    if containers:
        hosts = detect_traefik_hosts(containers)
        current = list(cfg.tls_check.urls or [])
        detected_urls = [f"https://{h}" for h in hosts]
        merged = _merged_list(current, detected_urls)
        if detected_urls and merged != current:
            new_only = [u for u in merged if u not in current]
            proposals.append(Proposal(
                dotted="tls_check.urls",
                value=merged, current=current,
                evidence=(
                    f"Traefik router rules expose {', '.join(new_only)} — "
                    f"probing them catches certificates about to expire"
                ),
            ))

    return proposals


# ─── apply ────────────────────────────────────────────────────────────────


def apply_proposals(
    cfg_path: Path,
    proposals: list[Proposal],
    *,
    only: set[str] | None = None,
) -> list[str]:
    """Write the selected proposals to the user overlay. Returns applied keys.

    Same write discipline as the settings UI: build the new overlay in
    memory, validate the FULL merged Config so Pydantic rejects anything
    inconsistent before a byte is written, then write atomically. `only`
    restricts to a subset of dotted keys (the review UI's checkboxes).

    Raises `pydantic.ValidationError` when the merged config is invalid —
    callers surface it; nothing has been written at that point.
    """
    import yaml

    from .config_overlay import (
        atomic_write_yaml,
        deep_merge,
        load_overlay,
        set_dotted,
        user_overlay_path,
    )

    selected = [
        p for p in proposals if only is None or p.dotted in only
    ]
    if not selected:
        return []

    overlay = load_overlay(user_overlay_path(cfg_path))
    for p in selected:
        overlay = set_dotted(overlay, p.dotted, p.value)

    # Validate the full merged Config before touching disk (mirrors the
    # settings UI's write path).
    base = yaml.safe_load(cfg_path.read_text()) or {}
    if not isinstance(base, dict):
        base = {}
    Config(**deep_merge(base, overlay))

    atomic_write_yaml(user_overlay_path(cfg_path), overlay)
    applied = [p.dotted for p in selected]
    log.info("autoconfig: applied %s", applied)
    return applied


__all__ = [
    "Proposal",
    "apply_proposals",
    "build_proposals",
    "detect_compose_paths",
    "detect_parity_capable",
    "detect_traefik_hosts",
]
