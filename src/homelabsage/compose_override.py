"""Compose override-file detector.

Docker Compose merges `docker-compose.yml` + `docker-compose.override.yml`
implicitly when both exist in the same directory. The override can flip
images, env, mounts, even networks — silently changing the running
graph relative to what HomelabSage's compose graph parser sees if the
parser only reads the base file.

We don't try to merge them ourselves (the merge semantics are
non-trivial: deep merge with list-replace for some keys, list-extend
for others). Instead, we surface the existence of an override file
so the user knows their effective graph may differ from our analysis.

Override conventions we recognise:
  - `docker-compose.override.yml` / `.yaml`
  - `compose.override.yml` / `.yaml`

`COMPOSE_FILE` env-controlled override paths are NOT detected — they
live in the shell, not on disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_OVERRIDE_NAMES = (
    "docker-compose.override.yml",
    "docker-compose.override.yaml",
    "compose.override.yml",
    "compose.override.yaml",
)

_BASE_NAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)


@dataclass
class OverridePresence:
    """One compose project that has an override sitting next to the base file."""

    base_path: str
    override_path: str
    project_dir: str

    def to_context(self) -> dict:
        return {
            "base_path": self.base_path,
            "override_path": self.override_path,
            "project_dir": self.project_dir,
        }


def scan(paths: list[str]) -> list[OverridePresence]:
    """Walk each path; collect base+override pairs.

    `paths` is the same `sources.docker.compose_scan_paths` list the
    cascade detector + linter consume. Directories are walked one
    level deep (compose projects are conventionally flat dirs); a
    direct file path is treated as the base file and we look for the
    override in its parent.

    Best-effort: unreadable directories are skipped silently.
    """
    out: list[OverridePresence] = []
    seen: set[tuple[str, str]] = set()  # (base, override) dedupe

    def _check_dir(d: Path) -> None:
        base: Path | None = None
        for name in _BASE_NAMES:
            cand = d / name
            if cand.is_file():
                base = cand
                break
        if base is None:
            return
        for name in _OVERRIDE_NAMES:
            ovr = d / name
            if not ovr.is_file():
                continue
            key = (str(base), str(ovr))
            if key in seen:
                continue
            seen.add(key)
            out.append(OverridePresence(
                base_path=str(base),
                override_path=str(ovr),
                project_dir=str(d),
            ))
            break  # one override per project

    for raw in paths:
        p = Path(raw).expanduser()
        try:
            if p.is_file():
                _check_dir(p.parent)
                continue
            if not p.is_dir():
                continue
        except OSError:
            continue
        # Direct dir + one level of children (typical Dockge `stacks/` layout).
        _check_dir(p)
        try:
            children = [c for c in p.iterdir() if c.is_dir()]
        except OSError:
            children = []
        for c in children:
            _check_dir(c)
    return out


__all__ = ["OverridePresence", "scan"]
