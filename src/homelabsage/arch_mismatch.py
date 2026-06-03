"""Architecture-mismatch detector.

The "I run Unraid on x86 but my friend's ARM box can't use my image"
problem is bigger than that — when an upstream maintainer drops arm64
manifests, every Raspberry Pi / Mac Mini M1 / ARM-based home server
breaks silently on the next pull.

This module checks: does the candidate image actually have a manifest
entry for the host's `os/arch` pair? When no, we attach
`Update.context.arch_mismatch` with the available platforms so the
analyzer can warn before the user pulls.

Inputs:
  - The local `platform.machine()` / `platform.system()` pair (cached
    once per process).
  - The image manifest (multi-arch images carry a `manifests` array;
    single-arch ones don't and are skipped).

Output: pure verdict. No HTTP — the docker plugin's existing
`registries.dockerhub_manifest` is what fetches the data.
"""

from __future__ import annotations

import logging
import platform
from dataclasses import dataclass

log = logging.getLogger(__name__)


# Docker uses `linux/amd64` / `linux/arm64` notation; Python's `platform`
# module returns x86_64 / aarch64. Map between them.
_MACHINE_TO_DOCKER: dict[str, str] = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "arm/v7",
    "armv6l": "arm/v6",
    "i386": "386",
    "i686": "386",
    "ppc64le": "ppc64le",
    "s390x": "s390x",
    "riscv64": "riscv64",
}


@dataclass
class ArchFinding:
    """Per-update verdict."""

    host_arch: str           # `linux/amd64`
    available: list[str]     # all `os/arch` strings the manifest carries
    matches: bool

    def to_context(self) -> dict:
        return {
            "host_arch": self.host_arch,
            "available": self.available,
            "matches": self.matches,
        }


def host_platform() -> str:
    """Best-effort `os/arch` for the running host. `linux/amd64` is the
    most common homelab; the table above covers the rest."""
    os_name = platform.system().lower()  # `linux`, `darwin`, …
    machine = platform.machine().lower()
    arch = _MACHINE_TO_DOCKER.get(machine, machine)
    return f"{os_name}/{arch}"


def evaluate(manifest_list: dict | None, *, host: str | None = None) -> ArchFinding | None:
    """Return a finding only when there IS a mismatch.

    `manifest_list` is the JSON payload from `docker manifest inspect`
    (`registries.dockerhub_manifest` returns this shape). Single-arch
    manifests don't carry a `manifests` array — we return None for
    those because there's nothing to verify.
    """
    if not isinstance(manifest_list, dict):
        return None
    manifests = manifest_list.get("manifests")
    if not isinstance(manifests, list) or not manifests:
        return None
    host_str = host or host_platform()
    available: list[str] = []
    for m in manifests:
        if not isinstance(m, dict):
            continue
        plat = m.get("platform") or {}
        if not isinstance(plat, dict):
            continue
        os_name = (plat.get("os") or "").lower()
        arch = (plat.get("architecture") or "").lower()
        variant = (plat.get("variant") or "").lower()
        if variant:
            available.append(f"{os_name}/{arch}/{variant}")
        else:
            available.append(f"{os_name}/{arch}")
    # Deduplicate, preserve order
    seen: set[str] = set()
    ordered: list[str] = []
    for p in available:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    available = ordered
    matches = host_str in available or any(
        # `linux/arm64/v8` should be considered as covering `linux/arm64`.
        p.startswith(host_str + "/") for p in available
    )
    if matches:
        return None
    return ArchFinding(host_arch=host_str, available=available, matches=False)


__all__ = ["ArchFinding", "evaluate", "host_platform"]
