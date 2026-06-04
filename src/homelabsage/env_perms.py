"""Compose `.env` permissions check.

A `.env` file with `0644` is world-readable. On Unraid that's
typically irrelevant (single-user box), but anyone with shell access
to a container that bind-mounts the compose dir for "config exposure"
becomes a credentials hole. The auditor surfaces these so the user
can `chmod 600`.

We walk the compose scan paths and stat `.env` (and `*.env`) files
one level deep. Group-readable or other-readable → `medium`.
Group-writable → `high`. World-writable → `critical`. We never
inspect file contents — that's `secret_guard`'s territory.

Best-effort: paths we can't stat are skipped silently.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass
class EnvPermFinding:
    """Per-file env-permission verdict."""

    path: str
    mode_octal: str
    severity: str   # medium | high | critical
    reason: str

    def to_context(self) -> dict:
        return {
            "path": self.path,
            "mode_octal": self.mode_octal,
            "severity": self.severity,
            "reason": self.reason,
        }


def _evaluate_mode(mode: int) -> tuple[str, str] | None:
    """Return (severity, reason) or None when permissions are fine."""
    if mode & (stat.S_IWGRP | stat.S_IWOTH) == (stat.S_IWGRP | stat.S_IWOTH):
        return "critical", "world+group writable"
    if mode & stat.S_IWOTH:
        return "critical", "world writable"
    if mode & stat.S_IWGRP:
        return "high", "group writable"
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        bits = []
        if mode & stat.S_IRGRP:
            bits.append("group")
        if mode & stat.S_IROTH:
            bits.append("world")
        return "medium", f"{'+'.join(bits)} readable"
    return None


def _check_one(path: Path) -> EnvPermFinding | None:
    try:
        st = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    verdict = _evaluate_mode(st.st_mode)
    if verdict is None:
        return None
    sev, reason = verdict
    return EnvPermFinding(
        path=str(path),
        mode_octal=oct(stat.S_IMODE(st.st_mode)),
        severity=sev,
        reason=reason,
    )


def scan(paths: list[str]) -> list[EnvPermFinding]:
    """Walk each compose dir; stat `.env` + `*.env` files.

    Recurses one level (the same depth as `compose_override.scan`)
    so Dockge-style `stacks/<name>/.env` layouts are caught.
    """
    seen: set[str] = set()
    out: list[EnvPermFinding] = []

    def _walk_dir(d: Path) -> None:
        try:
            entries = list(d.iterdir())
        except OSError:
            return
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue
            name = entry.name
            if name == ".env" or name.endswith(".env"):
                key = str(entry.resolve()) if entry.exists() else str(entry)
                if key in seen:
                    continue
                seen.add(key)
                finding = _check_one(entry)
                if finding is not None:
                    out.append(finding)

    for raw in paths:
        p = Path(raw).expanduser()
        try:
            if p.is_file():
                _walk_dir(p.parent)
                continue
            if not p.is_dir():
                continue
        except OSError:
            continue
        _walk_dir(p)
        try:
            children = [c for c in p.iterdir() if c.is_dir()]
        except OSError:
            children = []
        for c in children:
            _walk_dir(c)
    return out


__all__ = ["EnvPermFinding", "scan"]
