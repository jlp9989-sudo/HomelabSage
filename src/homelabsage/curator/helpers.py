"""Pure helpers used by the curator. No I/O, no class state.

Kept in their own module so the orchestrator stays readable as the curator
grows (system-level note, incremental hook, etc).
"""

from __future__ import annotations

import re
from typing import Any

from docker.models.containers import Container

_SECRET_NAME_RE = re.compile(
    r"(token|key|password|secret|auth|credential|dsn|api[_-]?key|access[_-]?key)",
    re.IGNORECASE,
)

_FOOTER_RE = re.compile(
    r"<!--\s*curator:\s*(?P<name>[^@\s]+)@(?P<digest>[A-Za-z0-9]+)\s*-->"
)

# Labels worth showing the LLM — everything else is noise (auto-generated
# Docker internals, build metadata, etc).
_LABEL_ALLOW_PREFIX = (
    "com.docker.compose.project",
    "com.docker.compose.service",
    "org.opencontainers.image.source",
    "org.opencontainers.image.version",
    "org.opencontainers.image.title",
    "homepage.",
    "net.unraid.docker.",
    "traefik.",
    "diun.",
)


def redact_env(env: list[str]) -> list[str]:
    """Return env vars with secret-looking values replaced by `[REDACTED]`."""
    out: list[str] = []
    for entry in env or []:
        if "=" not in entry:
            out.append(entry)
            continue
        name, _, value = entry.partition("=")
        if _SECRET_NAME_RE.search(name):
            out.append(f"{name}=[REDACTED]")
        else:
            out.append(f"{name}={value}")
    return out


def format_ports(attrs: dict[str, Any]) -> list[str]:
    """`"8080/tcp -> 0.0.0.0:8080"` style strings; empty list if none."""
    ports = (attrs.get("NetworkSettings") or {}).get("Ports") or {}
    out: list[str] = []
    for container_port, bindings in ports.items():
        if not bindings:
            continue
        for b in bindings:
            host = f"{b.get('HostIp', '')}:{b.get('HostPort', '')}".lstrip(":")
            out.append(f"{container_port} -> {host}")
    return out


def format_mounts(attrs: dict[str, Any]) -> list[str]:
    """`"<source> -> <destination> (rw|ro)"` per mount."""
    out: list[str] = []
    for m in attrs.get("Mounts") or []:
        src = m.get("Source") or m.get("Name") or "?"
        dst = m.get("Destination") or "?"
        mode = "ro" if m.get("RW") is False else "rw"
        out.append(f"{src} -> {dst} ({mode})")
    return out


def filter_labels(labels: dict[str, str] | None) -> dict[str, str]:
    if not labels:
        return {}
    return {k: v for k, v in labels.items() if k.startswith(_LABEL_ALLOW_PREFIX)}


def digest_short(c: Container) -> str:
    """First 12 hex chars of the image id, sha256-prefix stripped."""
    image_id = (c.image.id or "").removeprefix("sha256:")
    return image_id[:12] or "unknown"


def safe_filename(name: str) -> str:
    """Filesystem-safe `.md` filename for a container.

    Keep alphanumerics, dot, dash, underscore. Everything else → `_`.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._-") or "container"
    return f"{cleaned}.md"


def strip_fences(text: str) -> str:
    """Drop ```markdown / ``` wrappers some models add despite the instruction."""
    s = text.strip()
    if s.startswith("```"):
        # First fence line: ```, ```markdown, ```md, …
        first_nl = s.find("\n")
        if first_nl > 0:
            s = s[first_nl + 1 :]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def existing_footer(text: str) -> tuple[str, str] | None:
    """Return `(name, digest)` from the curator footer, or None if absent."""
    m = _FOOTER_RE.search(text)
    if not m:
        return None
    return m.group("name"), m.group("digest")


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 16] + "\n…[truncated]…"


# Re-export the compiled regex for callers that need to strip stale footers
# from LLM output before appending a fresh one.
FOOTER_RE = _FOOTER_RE
