"""Volume-orphan detector — docker volumes nobody is using.

Common waste pattern: the user runs `docker compose down -v` to clear
a database, then `up` brings a fresh container with a new volume name
because the compose file's `volumes:` section was reorganised. The
old volume stays on disk forever — `docker system df` shows it,
nothing else does.

Pure data: takes a `(volumes, containers)` pair and returns the
orphan list. The docker plugin calls `client.volumes.list()` +
`client.containers.list(all=True)` once per scan and feeds the
results in. Surface findings via the auditor + a new `/api/volumes`
endpoint (out of scope for this module — this is just the pure
heuristic).

`Update.context.volume_orphans` carries the per-image hint
(filtered to volumes that share the project label) so the analyzer
can mention them in the recommended action.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OrphanVolume:
    """One unreferenced volume."""

    name: str
    driver: str
    mountpoint: str | None
    created_at: str | None
    labels: dict[str, str]

    def to_context(self) -> dict:
        return {
            "name": self.name,
            "driver": self.driver,
            "mountpoint": self.mountpoint,
            "created_at": self.created_at,
            "compose_project": self.labels.get("com.docker.compose.project"),
        }


def _used_volume_names(containers: list) -> set[str]:
    """Walk every container's `Mounts` array and collect referenced
    `Source` volume names. Bind mounts have type=bind and aren't
    volumes; we skip them via the `Type == "volume"` filter."""
    used: set[str] = set()
    for c in containers:
        try:
            attrs = c.attrs or {}
        except Exception:
            continue
        mounts = attrs.get("Mounts") or []
        if not isinstance(mounts, list):
            continue
        for m in mounts:
            if not isinstance(m, dict):
                continue
            if (m.get("Type") or "").lower() != "volume":
                continue
            name = m.get("Name")
            if isinstance(name, str) and name:
                used.add(name)
    return used


def find_orphans(volumes: list, containers: list) -> list[OrphanVolume]:
    """Return docker volumes not referenced by any container.

    `volumes` is the docker SDK's `client.volumes.list()` result —
    each item has `.name` and `.attrs`. `containers` is
    `client.containers.list(all=True)`.

    `all=True` matters — a volume only referenced by a STOPPED container
    is NOT an orphan; the user may have stopped that container on
    purpose and the data still matters. Counts everything Docker knows
    about as "in use".
    """
    used = _used_volume_names(containers)
    orphans: list[OrphanVolume] = []
    for v in volumes:
        try:
            name = v.name
        except AttributeError:
            continue
        if name in used:
            continue
        attrs = getattr(v, "attrs", None) or {}
        orphans.append(OrphanVolume(
            name=name,
            driver=str(attrs.get("Driver") or "local"),
            mountpoint=attrs.get("Mountpoint"),
            created_at=attrs.get("CreatedAt"),
            labels=dict(attrs.get("Labels") or {}),
        ))
    return orphans


__all__ = ["OrphanVolume", "find_orphans"]
