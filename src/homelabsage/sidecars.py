"""Sidecar / init-container discovery.

Some containers are tightly coupled to a primary container — they
share the network namespace (`network_mode: container:<name>`), the
PID namespace (`pid_mode: container:<name>`), or are declared as
`init: true` in compose. Examples:

  - A VPN sidecar `gluetun` hosting the network for `qbittorrent`.
  - An nginx-proxy that lives in the same netns as the app it fronts.
  - An init container that warms a cache directory before the main
    service starts.

When the primary updates, the sidecar's lifecycle is implicated. This
module surfaces that relationship so the analyzer can mention
"restarting `qbittorrent` will also drop the VPN sidecar — give it
~30s to reattach".

Detection is pure: it walks the running container set + the compose
graph and emits `SidecarLink` objects. The docker plugin attaches the
resulting list to `Update.context.sidecars` and a new prompt rule
mentions them in the recommendation when non-empty.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)


# `container:<name-or-id>` — both forms are valid in docker. Docker
# sometimes stores the name with a leading slash (`/gluetun`), so the
# allowed-char set includes `/`; we strip the slash downstream.
_NETWORK_MODE_RE = re.compile(r"^container:([\w./\-]+)$")
_PID_MODE_RE = re.compile(r"^container:([\w./\-]+)$")


@dataclass
class SidecarLink:
    """One sidecar↔primary relationship."""

    sidecar: str       # the name of the sidecar container
    primary: str       # the container whose ns it joined
    relation: str      # network | pid | init

    def to_context(self) -> dict:
        return {
            "sidecar": self.sidecar,
            "primary": self.primary,
            "relation": self.relation,
        }


def _network_primary(container_attrs: dict) -> str | None:
    """Return the primary name from `HostConfig.NetworkMode = container:<x>`."""
    host = (container_attrs or {}).get("HostConfig") or {}
    nm = host.get("NetworkMode") or ""
    if not isinstance(nm, str):
        return None
    m = _NETWORK_MODE_RE.match(nm)
    if not m:
        return None
    raw = m.group(1)
    # `container:foo` may name an id or a name. Strip leading `/` that
    # docker stores on container names.
    return raw.lstrip("/")


def _pid_primary(container_attrs: dict) -> str | None:
    host = (container_attrs or {}).get("HostConfig") or {}
    pid = host.get("PidMode") or ""
    if not isinstance(pid, str):
        return None
    m = _PID_MODE_RE.match(pid)
    if not m:
        return None
    return m.group(1).lstrip("/")


def discover_runtime(containers: list) -> list[SidecarLink]:
    """Walk `client.containers.list(all=True)` results and yield links.

    `containers` is a list of `docker.models.containers.Container` (or
    anything with `.name` + `.attrs`). Best-effort: a container whose
    attrs don't carry HostConfig is silently skipped.
    """
    # Build a name→short_id map so name aliases resolve to themselves
    # when `container:<short_id>` is the form used.
    by_id: dict[str, str] = {}
    for c in containers:
        try:
            cid = (c.id or "")[:12]
            by_id[cid] = c.name
        except Exception:
            continue

    links: list[SidecarLink] = []
    for c in containers:
        try:
            attrs = c.attrs or {}
        except Exception:
            continue
        net_primary_raw = _network_primary(attrs)
        if net_primary_raw and net_primary_raw != c.name:
            primary = by_id.get(net_primary_raw[:12], net_primary_raw)
            links.append(SidecarLink(
                sidecar=c.name, primary=primary, relation="network",
            ))
        pid_primary_raw = _pid_primary(attrs)
        if pid_primary_raw and pid_primary_raw != c.name:
            primary = by_id.get(pid_primary_raw[:12], pid_primary_raw)
            # Skip duplicate when net+pid both point at the same primary.
            if not any(
                link.sidecar == c.name and link.primary == primary
                and link.relation == "network"
                for link in links
            ):
                links.append(SidecarLink(
                    sidecar=c.name, primary=primary, relation="pid",
                ))
    return links


def discover_from_compose_graph(graph) -> list[SidecarLink]:
    """Scan a `DependencyGraph` for compose-declared init containers.

    `init: true` services aren't strictly sidecars in the runtime sense
    but they share the lifecycle — when the primary upgrades the init
    runs again first. We emit them with `relation="init"`.
    """
    links: list[SidecarLink] = []
    for svc in getattr(graph, "services", {}).values():
        # We don't currently parse `init:` from the compose file (the
        # cascade walker only records depends_on + networks). When that
        # gets added, this branch will populate. Until then we use
        # the depends_on chain as a proxy: a service that has exactly
        # one dependent IS the init+primary pattern.
        dependents = graph.dependents_of(svc.name)
        if len(dependents) == 1 and svc.name.lower().endswith(("init", "setup", "migrate")):
            links.append(SidecarLink(
                sidecar=svc.container_name or svc.name,
                primary=dependents[0].container_name or dependents[0].name,
                relation="init",
            ))
    return links


def sidecars_for(
    container_name: str,
    links: list[SidecarLink],
) -> list[SidecarLink]:
    """Filter to links where `container_name` is the primary.

    The docker plugin calls this when building each Update's context:
    we want "what depends on me?" not "what do I depend on?".
    """
    return [link for link in links if link.primary == container_name]


__all__ = [
    "SidecarLink",
    "discover_from_compose_graph",
    "discover_runtime",
    "sidecars_for",
]
