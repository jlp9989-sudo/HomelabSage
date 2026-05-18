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

# Env vars that are pure container-runtime noise — interpreter sha sums,
# init system internals, build metadata, locale. Every LLM tested at the
# 12-may benchmark cited at least one of these as if it were homelab-relevant
# (Llama-Groq quoted `PYTHON_SHA256` verbatim). Drop them before assembling
# the prompt; if a real config var collides, the user can override per-image
# via env_allowlist (TODO when someone hits the case).
_NOISE_ENV_PREFIXES = (
    "PYTHON",       # PYTHON_VERSION, PYTHON_SHA256, PYTHON_PIP_VERSION, PYTHONUNBUFFERED, PYTHONDONTWRITEBYTECODE
    "S6_",          # s6-overlay internals on LSIO images
    "_S6_",
    "GPG_KEY",      # interpreter build-time signing keys
    "NODE_VERSION",
    "NPM_CONFIG_",
    "YARN_VERSION",
    "GOLANG_VERSION",
    "RUBY_",
    "PERL_",
)
_NOISE_ENV_EXACT = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "TERM",
        "HOSTNAME",
        "HOME",
        "PWD",
        "SHLVL",
        "DEBIAN_FRONTEND",
        "LANGUAGE",
    }
)


def _is_noise_env(name: str) -> bool:
    """True if this env var is container-runtime boilerplate the LLM should not see."""
    if name in _NOISE_ENV_EXACT:
        return True
    return any(name.startswith(p) for p in _NOISE_ENV_PREFIXES)

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
    """Clean an `Env` list before sending it to the LLM.

    Two passes in one:

    1. Drop entries that are pure container-runtime noise (interpreter sha
       sums, init system internals, locale, PATH). The LLM has no way to tell
       these are irrelevant and routinely cites them as if they meant
       something.
    2. Redact values whose key name looks secret-shaped (token/key/password/…).
       Done by name, not by value, so unusual secrets in plain-looking keys
       still get caught if the key contains one of the secret words.

    Entries without `=` are passed through unchanged (older daemons can emit
    plain strings, and dropping them silently would hide a real bug).
    """
    out: list[str] = []
    for entry in env or []:
        if "=" not in entry:
            out.append(entry)
            continue
        name, _, value = entry.partition("=")
        if _is_noise_env(name):
            continue
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


# Rule 7 of the curator prompt instructs the LLM to emit this literal line
# (and nothing else) when it can't infer a purpose. The wording in the
# template uses an em-dash; some models normalise it to a regular dash, drop
# spaces, or add quotes — accept all of those. The body contract is "first
# non-empty line is the fallback" — Gemini sometimes appends bullets despite
# the "stop" instruction, but if the fallback is the lead, the rest is
# unsupported speculation we should not write to disk anyway.
_FALLBACK_RE = re.compile(
    r"""
    \(?            # opt opening paren
    \s*
    no\ purpose\ stated\ yet
    [\s—–\-:,]+   # em-dash / en-dash / hyphen / punctuation between
    fill\ in
    \s*
    \)?            # opt closing paren
    """,
    re.IGNORECASE | re.VERBOSE,
)


def is_purpose_fallback(body: str) -> bool:
    """True if the LLM bailed out with the Rule 7 fallback as its lead.

    Robust to small wording drift (em-dash → dash, missing parens, slight
    whitespace) and to follow-up bullets the model may add by mistake.
    """
    if not body:
        return False
    for line in body.splitlines():
        stripped = line.strip().lstrip("#> *-").strip().strip("`'\"")
        if not stripped:
            continue
        return bool(_FALLBACK_RE.search(stripped))
    return False
