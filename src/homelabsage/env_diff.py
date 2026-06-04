"""Container env-var diff detector.

When a maintainer adds a new env var to their image, two things
typically happen: (1) docs get updated days/weeks later, and (2)
upgrading the container leaves the variable unset, with consequences
ranging from "broken feature" to "container crashloops on boot".

This module is a pure-function diff between a running container's
env and the env declared in the incoming image's `Config.Env`. We
flag:

  - **NEW** vars present in image but missing from container — most
    likely to break things; flagged `medium` (or `high` when the
    value looks credential-shaped: ends in `_TOKEN`, `_KEY`, `_PASSWORD`,
    `_SECRET`).
  - **REMOVED** vars present in container but absent from image —
    usually safe (operator set extras), but worth surfacing as `info`.
  - **CHANGED defaults** where both define the same var but with
    different default values; `info` (only surfaces facts, the LLM
    decides whether it matters).

Image `Env` already includes the maintainer-provided values. We
compare against the EFFECTIVE container env (the user-set vars +
inherited image defaults). Docker doesn't tell us which was which,
but the diff is informative either way: if a var present in BOTH
matches the image default exactly, it's still possibly a no-op the
user can clear; if it differs, the user overrode it.
"""

from __future__ import annotations

from dataclasses import dataclass

_CREDENTIAL_SUFFIXES = (
    "_TOKEN", "_KEY", "_PASSWORD", "_SECRET", "_PASS", "_PWD",
    "_APIKEY", "_API_KEY", "_AUTH",
)


def _is_credential_like(var: str) -> bool:
    upper = var.upper()
    return any(upper.endswith(s) for s in _CREDENTIAL_SUFFIXES)


@dataclass
class EnvDiffEntry:
    """One row in the diff. `kind` ∈ {new, removed, changed}."""

    name: str
    kind: str
    container_value: str | None
    image_value: str | None
    severity: str   # info | medium | high

    def to_context(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "container_value": self.container_value,
            "image_value": self.image_value,
            "severity": self.severity,
        }


def _parse_env_list(items: list[str]) -> dict[str, str]:
    """Docker stores env as `["KEY=value", ...]`. Split on the first `=`.

    Items without `=` are kept with empty value (rare, but legal).
    """
    out: dict[str, str] = {}
    for raw in items or []:
        if not isinstance(raw, str):
            continue
        key, sep, val = raw.partition("=")
        if not key:
            continue
        out[key] = val if sep else ""
    return out


def diff(
    container_env: list[str] | None,
    image_env: list[str] | None,
    *,
    ignore_vars: set[str] | None = None,
) -> list[EnvDiffEntry]:
    """Compute the diff. Deterministic ordering: NEW, REMOVED, CHANGED.

    `ignore_vars` skips common noise (PATH, HOME, HOSTNAME injected
    by docker — these would dominate every diff). Defaults applied
    when None passed.
    """
    if ignore_vars is None:
        ignore_vars = {
            "PATH", "HOME", "HOSTNAME", "TERM", "LANG", "LC_ALL", "USER",
            "PWD", "SHLVL", "_",
        }
    c = _parse_env_list(container_env or [])
    i = _parse_env_list(image_env or [])
    out: list[EnvDiffEntry] = []

    # NEW: in image, not in container
    for name in sorted(i):
        if name in ignore_vars or name in c:
            continue
        sev = "high" if _is_credential_like(name) else "medium"
        out.append(EnvDiffEntry(
            name=name, kind="new",
            container_value=None, image_value=i[name], severity=sev,
        ))

    # REMOVED: in container, not in image
    for name in sorted(c):
        if name in ignore_vars or name in i:
            continue
        out.append(EnvDiffEntry(
            name=name, kind="removed",
            container_value=c[name], image_value=None, severity="info",
        ))

    # CHANGED defaults: in both, different values
    for name in sorted(c):
        if name in ignore_vars or name not in i:
            continue
        if c[name] != i[name]:
            out.append(EnvDiffEntry(
                name=name, kind="changed",
                container_value=c[name], image_value=i[name],
                severity="info",
            ))

    return out


__all__ = ["EnvDiffEntry", "diff"]
