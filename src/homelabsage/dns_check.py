"""DNS resolution probe for hostnames.

Companion to `tls_check`: the cert can be valid and the cert-check
passes, but the underlying DNS record drifts (cloudflared tunnel
deleted, CNAME pointing at a removed VPS, NXDOMAIN from a registrar
glitch). A user discovers it the hard way when they click their
own bookmark and Chrome shows DNS_PROBE_FINISHED_NXDOMAIN.

Pure stdlib (`socket.getaddrinfo`). Per-host timeout via the
`socket.setdefaulttimeout` context (no API in `getaddrinfo` itself).
Returns one finding per failed resolution; succeeds are silent.
"""

from __future__ import annotations

import logging
import socket
from contextlib import contextmanager
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class DNSFinding:
    """One hostname that failed to resolve."""

    hostname: str
    error: str
    severity: str   # always `medium` — DNS misses are not critical
                    #   on their own, but they signal the upstream
                    #   monitoring + alerting should pick them up.

    def to_context(self) -> dict:
        return {
            "hostname": self.hostname,
            "error": self.error,
            "severity": self.severity,
        }


@contextmanager
def _temp_timeout(seconds: float):
    """Apply a process-wide socket timeout for the duration of the block.

    `socket.getaddrinfo` ignores per-call timeouts; the only way to
    bound it is via `socket.setdefaulttimeout`. We save+restore so
    we don't poison the rest of the process.
    """
    prev = socket.getdefaulttimeout()
    socket.setdefaulttimeout(seconds)
    try:
        yield
    finally:
        socket.setdefaulttimeout(prev)


def probe(hostname: str, *, timeout: float = 3.0) -> DNSFinding | None:
    """Resolve `hostname`. Return None on success, finding on failure.

    Empty hostname → None (caller's job to filter). IP literals also
    succeed (`getaddrinfo` accepts both).
    """
    if not hostname or not hostname.strip():
        return None
    h = hostname.strip()
    try:
        with _temp_timeout(timeout):
            socket.getaddrinfo(h, None)
    except (socket.gaierror, OSError) as e:
        return DNSFinding(
            hostname=h,
            error=f"{type(e).__name__}: {e}",
            severity="medium",
        )
    return None


def check_hostnames(
    hostnames: list[str], *, timeout: float = 3.0,
) -> list[DNSFinding]:
    """Probe each hostname; return only failures.

    Bound by sequential per-host timeout (no concurrency yet — DNS
    is cheap when working, and we don't want to flood the resolver
    on a list that all NXDOMAIN at once).
    """
    out: list[DNSFinding] = []
    for h in hostnames:
        finding = probe(h, timeout=timeout)
        if finding is not None:
            out.append(finding)
    return out


__all__ = ["DNSFinding", "check_hostnames", "probe"]
