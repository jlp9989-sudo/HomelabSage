"""Detect containers running with `--network=host`.

Host-network containers bypass `exposed_ports` (no NAT layer to
inspect) and port-conflict with the host. Often used intentionally
(Tailscale exit nodes, Plex DLNA) but easy to introduce by accident
when copying compose snippets from random forums.

Reads `HostConfig.NetworkMode == "host"`. The detector emits a
finding at `info` severity by default — host-net is a *signal*, not
a *bug*. The user can mute via `audit-mute add network_mode_host
network_mode_host <subject>` when intentional.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class HostNetFinding:
    """Per-container host-network verdict."""

    network_mode: str          # the literal NetworkMode value
    severity: str              # `info` by default

    def to_context(self) -> dict:
        return {
            "network_mode": self.network_mode,
            "severity": self.severity,
        }


def evaluate(host_config: dict) -> HostNetFinding | None:
    """Return a finding when NetworkMode == "host". None otherwise.

    Best-effort: a missing / non-dict HostConfig returns None.
    """
    if not isinstance(host_config, dict):
        return None
    mode = host_config.get("NetworkMode")
    if not isinstance(mode, str):
        return None
    if mode.lower() != "host":
        return None
    return HostNetFinding(network_mode=mode, severity="info")


__all__ = ["HostNetFinding", "evaluate"]
