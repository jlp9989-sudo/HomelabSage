"""`docker compose config` wrapper — syntax-level validator.

Sister module to `compose_lint`: that one finds smells, this one finds
*broken* files. We shell out to the docker CLI's `compose config -q`
which parses the YAML the way docker would actually load it (catching
v1-vs-v2 quirks, env-var interpolation failures, ${VAR:?required}
violations, etc).

Why a separate module: the linter loads raw YAML with PyYAML — it
doesn't see docker's interpolation pass. The validator does.

Pure subprocess; no docker SDK needed. Missing `docker` binary is a
no-op (returns empty findings, logged at DEBUG).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .compose import _iter_compose_files

log = logging.getLogger(__name__)

_TIMEOUT = 30.0


@dataclass
class ComposeValidationFinding:
    """One broken compose file."""

    file: Path
    error: str

    def to_context(self) -> dict:
        return {"file": str(self.file), "error": self.error}


def _run_docker_compose_config(file: Path) -> tuple[int, str]:
    """Run `docker compose -f <file> config -q`. (rc, combined_err)."""
    if not shutil.which("docker"):
        return -1, "docker binary not on PATH"
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", str(file), "config", "-q"],
            timeout=_TIMEOUT, check=False,
            capture_output=True, text=True, errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return -2, f"docker compose timed out / OSError: {e}"
    # `compose config` writes errors to stderr; stdout is the resolved
    # YAML on success. With -q nothing is written on success.
    return result.returncode, (result.stderr or result.stdout).strip()


def validate_compose_file(file: Path) -> ComposeValidationFinding | None:
    """Return a finding when `compose config -q` fails."""
    rc, msg = _run_docker_compose_config(file)
    if rc == 0:
        return None
    if rc == -1:
        # Missing docker binary — silently skip; the auditor will not
        # surface a per-file "no docker" line for every compose file.
        return None
    # Squash multiline error to the first non-empty line for the
    # auditor's one-line detail.
    first_line = next(
        (ln.strip() for ln in (msg or "").splitlines() if ln.strip()),
        "compose validation failed (no error message)",
    )
    return ComposeValidationFinding(file=file, error=first_line[:500])


def validate_paths(roots: Iterable[str | Path]) -> list[ComposeValidationFinding]:
    """Walk every compose file under `roots` and validate each."""
    out: list[ComposeValidationFinding] = []
    for path in _iter_compose_files(roots):
        f = validate_compose_file(path)
        if f is not None:
            out.append(f)
    return out


__all__ = [
    "ComposeValidationFinding",
    "validate_compose_file",
    "validate_paths",
]
