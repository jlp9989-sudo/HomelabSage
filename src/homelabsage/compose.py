"""docker-compose dependency graph builder.

Scans one or more directories for `docker-compose.yml` / `compose.yaml` /
`compose.yml` files, extracts service-level `depends_on` and shared-network
relationships, and answers "which services depend on X?" queries.

Used by the docker plugin to enrich each Update with a `cascade` block:
when service A has an update, every B that depends on A gets listed so the
analyzer can warn the user "applying this will also restart B, C, D".

Scoping:
  - We support the modern `depends_on: [a, b]` short form and the long form
    (`depends_on: {a: {condition: ...}}`).
  - We treat shared networks as a weak signal — being on the same network
    doesn't mean B depends on A's state, only that the topology touches.
    These are surfaced as `network_neighbours` (separate from `depends_on`).
  - YAML is parsed permissively; malformed files are logged and skipped, not
    raised — the analyzer must keep running.
  - Stack discovery is recursive but bounded: we cap the walk depth so a
    user pointing us at `/mnt/user` doesn't read every file on the array.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


# Filenames Docker Compose / Dockge / portainer-stack-files default to. We
# include the legacy hyphenated form because Unraid templates still ship it.
_COMPOSE_FILENAMES = {"docker-compose.yml", "docker-compose.yaml",
                      "compose.yml", "compose.yaml"}

# How deep we'll descend below a configured scan root. Most stack layouts are
# `<root>/<stack>/compose.yaml`; a hard cap stops accidental whole-disk walks.
_MAX_DEPTH = 4


@dataclass
class ComposeService:
    """Service-level data we care about per compose file."""

    name: str
    container_name: str | None  # the `container_name:` field, when set
    image: str | None
    depends_on: list[str] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)
    file: Path | None = None  # which compose.yaml declared it
    project: str | None = None  # directory containing the compose file


@dataclass
class DependencyGraph:
    """All services found across scan paths, plus reverse-dependency lookup.

    Reverse map is the load-bearing query: given service A, who declares A
    in `depends_on`?
    """

    services: dict[str, ComposeService]
    # Stored keys are SERVICE NAMES as declared in compose; the lookup helper
    # below also resolves container_name aliases.
    _dependents: dict[str, set[str]]

    def dependents_of(self, name: str) -> list[ComposeService]:
        """Return ComposeService entries that depend on `name`.

        `name` is matched against both `service_name` and `container_name`
        because Docker only knows the latter at runtime — the plugin always
        starts from a `c.name` (= container_name) and needs to map back.
        """
        # First: alias container_name → service_name
        aliases = [name]
        for s in self.services.values():
            if s.container_name == name and s.name not in aliases:
                aliases.append(s.name)
        out: list[ComposeService] = []
        seen: set[str] = set()
        for alias in aliases:
            for dep_name in self._dependents.get(alias, set()):
                if dep_name in seen:
                    continue
                seen.add(dep_name)
                if dep_name in self.services:
                    out.append(self.services[dep_name])
        return out

    def network_neighbours_of(self, name: str) -> list[ComposeService]:
        """Services that sit on at least one network in common with `name`.

        Excludes `name` itself and the implicit `default` network (every
        compose project gets one whether the user declares it or not).
        """
        if name not in self.services:
            # Try alias
            for s in self.services.values():
                if s.container_name == name:
                    name = s.name
                    break
            else:
                return []
        nets = set(self.services[name].networks) - {"default"}
        if not nets:
            return []
        out: list[ComposeService] = []
        for svc_name, svc in self.services.items():
            if svc_name == name:
                continue
            if nets & set(svc.networks):
                out.append(svc)
        return out


def _iter_compose_files(roots: Iterable[str | Path]) -> Iterable[Path]:
    """Yield every compose-shaped file under any root, bounded by `_MAX_DEPTH`."""
    for root in roots:
        p = Path(root)
        if not p.exists():
            log.debug("compose scan: root %s missing, skip", p)
            continue
        if p.is_file() and p.name in _COMPOSE_FILENAMES:
            yield p
            continue
        # Walk with depth cap.
        base_depth = len(p.parts)
        for sub in p.rglob("*"):
            if not sub.is_file():
                continue
            if sub.name not in _COMPOSE_FILENAMES:
                continue
            if len(sub.parts) - base_depth > _MAX_DEPTH:
                continue
            yield sub


def _parse_depends_on(raw: Any) -> list[str]:
    """`depends_on` can be a list or a dict in compose v2/v3. Normalise to list."""
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, dict):
        return list(raw.keys())
    return []


def _parse_networks(raw: Any) -> list[str]:
    """`networks:` can also be either form; we want the names only."""
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, dict):
        return list(raw.keys())
    return []


def parse_compose_file(path: Path) -> list[ComposeService]:
    """Read one compose file. Errors swallowed → empty list."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.debug("compose: cannot read %s: %s", path, e)
        return []
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        log.warning("compose: YAML error in %s: %s", path, e)
        return []
    if not isinstance(data, dict):
        return []
    services = data.get("services")
    if not isinstance(services, dict):
        return []
    out: list[ComposeService] = []
    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        out.append(ComposeService(
            name=svc_name,
            container_name=svc.get("container_name"),
            image=svc.get("image"),
            depends_on=_parse_depends_on(svc.get("depends_on")),
            networks=_parse_networks(svc.get("networks")),
            file=path,
            project=path.parent.name,
        ))
    return out


# ─── mtime cache ────────────────────────────────────────────────────────
#
# The docker plugin rebuilds the dependency graph on every scan. The walk
# itself is cheap, but the YAML parse for ~30 stacks adds up to ~30ms per
# scan — which we pay even when nothing changed. Cache the graph keyed on
# the aggregate mtime of every compose file under the scan roots so
# unchanged inputs return immediately.
#
# The cache is intentionally tiny (one slot per scan-paths fingerprint) and
# process-local: re-running the same scan paths after a config edit hits
# the disk again only when the underlying files have actually changed.

_GRAPH_CACHE: dict[str, tuple[tuple[tuple[str, int], ...], DependencyGraph]] = {}


def _compose_files_signature(roots: Iterable[str | Path]) -> tuple[tuple[str, int], ...]:
    """Aggregate mtime of every compose file under the roots.

    Returns a sorted tuple of `(path, mtime_ns)` pairs so equality is
    structural — adding, removing or editing any file invalidates the
    cache entry on the next call.
    """
    items: list[tuple[str, int]] = []
    for path in _iter_compose_files(roots):
        try:
            items.append((str(path), path.stat().st_mtime_ns))
        except OSError:
            # File disappeared between walk and stat; let the next caller
            # rediscover it on the next scan.
            continue
    items.sort()
    return tuple(items)


def _cache_key(roots: Iterable[str | Path]) -> str:
    """Stable key for `_GRAPH_CACHE` — roots are normalised + sorted."""
    return "|".join(sorted(str(Path(r)) for r in roots))


def build_graph(
    scan_paths: Iterable[str | Path],
    *,
    use_cache: bool = True,
) -> DependencyGraph:
    """Walk every root, parse every compose file, build the graph.

    Cached across calls when nothing under `scan_paths` has changed. Pass
    `use_cache=False` to force a rebuild — handy in tests and in `homelabsage
    diagnostics`-style probes where the user expects fresh data.
    """
    roots = list(scan_paths)
    key = _cache_key(roots)
    signature = _compose_files_signature(roots) if use_cache else None

    if use_cache:
        cached = _GRAPH_CACHE.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]

    services: dict[str, ComposeService] = {}
    dependents: dict[str, set[str]] = defaultdict(set)
    for path in _iter_compose_files(roots):
        for svc in parse_compose_file(path):
            # Same service name can recur across different compose projects;
            # keep the first occurrence and append the rest under a project-
            # qualified key so the graph remains queryable.
            svc_key = svc.name
            if svc_key in services and services[svc_key].file != svc.file:
                svc_key = f"{svc.project}/{svc.name}"
            services[svc_key] = svc
            for dep in svc.depends_on:
                dependents[dep].add(svc_key)

    graph = DependencyGraph(services=services, _dependents=dict(dependents))
    if use_cache and signature is not None:
        _GRAPH_CACHE[key] = (signature, graph)
    return graph


def clear_graph_cache() -> None:
    """Drop every cached graph. Tests use this between cases; runtime code
    rarely needs it because the mtime check invalidates on file edits."""
    _GRAPH_CACHE.clear()
