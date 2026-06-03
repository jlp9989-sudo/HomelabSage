"""Resource-limit absence detector.

One unbounded container can take down the whole host: a memory leak
keeps growing until the OOM-killer comes for an unrelated process,
or a runaway CPU loop steals every core. `mem_limit:` + `cpus:` is
the seatbelt; this detector flags containers running without one.

Heuristic: a container has effective limits when EITHER
  - `HostConfig.Memory > 0`           (mem_limit / mem_reservation), OR
  - `HostConfig.NanoCpus > 0`         (cpus / deploy.resources.limits.cpus)
  - `HostConfig.CpuQuota > 0`         (cpu_quota fallback)

We only flag the ABSENCE — never tell the user what value to set. The
right number depends on host headroom, not on us. The analyzer prompt
just says "consider adding `mem_limit:` if this container has ever
OOMed before".

Pure-data: walks the container set and returns findings. No side
effects.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ResourceLimitFinding:
    """One container running without limits."""

    container_name: str
    has_mem_limit: bool
    has_cpu_limit: bool

    def to_context(self) -> dict:
        return {
            "has_mem_limit": self.has_mem_limit,
            "has_cpu_limit": self.has_cpu_limit,
        }


def has_memory_limit(host_config: dict) -> bool:
    """True iff Memory or MemoryReservation is set to a positive value."""
    if not isinstance(host_config, dict):
        return False
    for key in ("Memory", "MemoryReservation"):
        v = host_config.get(key)
        try:
            if v and int(v) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def has_cpu_limit(host_config: dict) -> bool:
    """True iff NanoCpus, CpuQuota or CpuShares is non-zero.

    `CpuShares` is the weakest form (relative weighting, no hard cap),
    but it counts as "the user thought about this" so we don't flag.
    """
    if not isinstance(host_config, dict):
        return False
    for key in ("NanoCpus", "CpuQuota", "CpuPeriod", "CpuShares"):
        v = host_config.get(key)
        try:
            if v and int(v) > 0:
                # CpuShares default is 1024 (the kernel default).
                # We only treat it as a limit when the user clearly
                # set it to something non-default — otherwise the
                # field is meaningless noise.
                if key == "CpuShares" and int(v) == 1024:
                    continue
                return True
        except (TypeError, ValueError):
            continue
    return False


def evaluate(container_attrs: dict) -> ResourceLimitFinding | None:
    """Return a finding when EITHER limit is missing, else None.

    The dual check (mem OR cpu) is intentional: many homelab users set
    one but forget the other. Flagging both independently lets the user
    decide which to fix first.
    """
    host = (container_attrs or {}).get("HostConfig") or {}
    name = (container_attrs or {}).get("Name", "").lstrip("/")
    mem_ok = has_memory_limit(host)
    cpu_ok = has_cpu_limit(host)
    if mem_ok and cpu_ok:
        return None
    return ResourceLimitFinding(
        container_name=name or "(unknown)",
        has_mem_limit=mem_ok,
        has_cpu_limit=cpu_ok,
    )


__all__ = [
    "ResourceLimitFinding",
    "evaluate",
    "has_cpu_limit",
    "has_memory_limit",
]
