"""Exposed-port detector.

Containers binding privileged ports to `0.0.0.0` (the IPv4 wildcard)
expose attack surface the user may not realise. The classic homelab
fail-state: a service that worked great on the LAN gets a router port
forward and is suddenly internet-reachable with no rate-limiting.

We flag:
  - **privileged** (port < 1024) bound to `0.0.0.0` / `::`
  - **any** TCP port bound to a public IPv4 (not RFC1918, not
    loopback)

We do NOT flag the privileged-on-localhost case — that's the
expected pattern for sidecars and reverse proxies. We do NOT flag
0.0.0.0 on a non-privileged port — too noisy on a typical homelab
where Plex (32400), Sonarr (8989), etc. legitimately bind that way.

Output: list of `PortFinding`. The docker plugin attaches them to
`Update.context.exposed_ports` and the analyzer's prompt mentions
them in `recommended_action` for security-relevant updates.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

# RFC1918 + carrier-grade NAT + loopback + link-local. Anything
# matching these is treated as "non-public" — a port bound to a
# 10.x address is on the LAN, not on the internet.
_PRIVATE_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),       # CGNAT
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("::1/128"),
)


@dataclass
class PortFinding:
    """One smelly port binding."""

    container_port: int
    host_ip: str
    host_port: int
    proto: str        # tcp / udp
    reason: str       # privileged_wildcard | public_bind

    def to_context(self) -> dict:
        return {
            "container_port": self.container_port,
            "host_ip": self.host_ip,
            "host_port": self.host_port,
            "proto": self.proto,
            "reason": self.reason,
        }


def _is_private_or_loopback(ip: str) -> bool:
    """True iff the IP is in a private/loopback range. Empty/wildcard
    returns False — wildcards are the case we want to flag."""
    if not ip or ip in ("0.0.0.0", "::"):
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # un-parseable → don't flag
    return any(addr in net for net in _PRIVATE_NETS)


def evaluate(network_settings: dict) -> list[PortFinding]:
    """Walk the docker SDK's `NetworkSettings.Ports` dict.

    Shape per docker docs:
      {
        "8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"},
                     {"HostIp": "::", "HostPort": "8080"}],
        "80/tcp":   None,   # exposed but not published
      }

    None-valued entries (`EXPOSE` declarations not bound to the host)
    are not findings — they don't reach the host's network. Returns
    an empty list when the container has no published ports.
    """
    if not isinstance(network_settings, dict):
        return []
    ports = network_settings.get("Ports")
    if not isinstance(ports, dict):
        return []
    findings: list[PortFinding] = []
    for spec, bindings in ports.items():
        if not bindings:
            continue
        # spec is e.g. "8080/tcp"
        if "/" not in spec:
            continue
        try:
            cport_s, proto = spec.split("/", 1)
            cport = int(cport_s)
        except ValueError:
            continue
        for b in bindings:
            if not isinstance(b, dict):
                continue
            host_ip = (b.get("HostIp") or "").strip()
            host_port_raw = b.get("HostPort")
            try:
                host_port = int(host_port_raw) if host_port_raw else 0
            except (TypeError, ValueError):
                host_port = 0
            is_wildcard = host_ip in ("", "0.0.0.0", "::")
            if cport < 1024 and is_wildcard:
                findings.append(PortFinding(
                    container_port=cport, host_ip=host_ip or "0.0.0.0",
                    host_port=host_port, proto=proto,
                    reason="privileged_wildcard",
                ))
                continue
            if host_ip and not is_wildcard \
                    and not _is_private_or_loopback(host_ip):
                findings.append(PortFinding(
                    container_port=cport, host_ip=host_ip,
                    host_port=host_port, proto=proto,
                    reason="public_bind",
                ))
    return findings


__all__ = ["PortFinding", "evaluate"]
