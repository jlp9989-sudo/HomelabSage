"""DNS resolution probe for hostnames.

Companion to `tls_check`: the cert can be valid and the cert-check
passes, but the underlying DNS record drifts (cloudflared tunnel
deleted, CNAME pointing at a removed VPS, NXDOMAIN from a registrar
glitch). A user discovers it the hard way when they click their
own bookmark and Chrome shows DNS_PROBE_FINISHED_NXDOMAIN.

Pure stdlib (`socket.getaddrinfo`). Per-call timeout enforced via a
small dedicated thread-pool — NOT via `socket.setdefaulttimeout`,
which is process-wide and would silently bleed onto httpx, docker
SDK, ssl, smtplib calls running in other threads while a probe is
in flight. Returns one finding per failed resolution; successes
are silent.
"""

from __future__ import annotations

import concurrent.futures
import logging
import socket
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


# Module-level pool: cheap to keep around (4 idle threads), avoids the
# per-call shutdown-waits-for-running-futures issue of a per-call
# `with ThreadPoolExecutor(...)`. The whole point of the timeout is to
# detach our caller from a hung lookup; a per-call pool would defeat
# that by blocking on shutdown.
_RESOLVER = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="hls-dns",
)


def probe(hostname: str, *, timeout: float = 3.0) -> DNSFinding | None:
    """Resolve `hostname`. Return None on success, finding on failure.

    Empty hostname → None (caller's job to filter). IP literals also
    succeed (`getaddrinfo` accepts both).

    Timeout bounds OUR wait, not the underlying lookup — a hung
    resolver call keeps running in the worker thread until the system
    resolver eventually fails. That's fine: we measured the bound
    accurately from the caller's perspective without touching the
    process-wide socket default.
    """
    if not hostname or not hostname.strip():
        return None
    h = hostname.strip()
    fut = _RESOLVER.submit(socket.getaddrinfo, h, None)
    try:
        fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        return DNSFinding(
            hostname=h,
            error=f"TimeoutError: resolution exceeded {timeout}s",
            severity="medium",
        )
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
