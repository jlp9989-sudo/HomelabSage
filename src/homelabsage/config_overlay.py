"""User-config overlay — `config.user.yaml` next to `config.yaml`.

Design rules:

* `config.yaml` is the deploy-time default (read-only in production, usually
  baked into an image or mounted from a compose file).
* `config.user.yaml` is the writable overlay edited via the web UI. It lives
  in the same directory as `config.yaml` and applies on top.
* Deep merge: nested dicts merge key-by-key; LISTS REPLACE rather than concat,
  because list semantics in a config rarely match what append would do
  ("skip" regex list, mounts, etc).
* `None` overlay values are treated as "key not present" — to *remove* an
  override we delete the key from the overlay file (helper `remove_key`).
* All writes are atomic: write to `<file>.tmp`, then `os.replace`. So a crash
  mid-write can never leave the user with a half-written YAML.
* This module is pure stdlib + pyyaml; it does NOT import Pydantic. Validation
  is the caller's responsibility (load_config does it after merging).

The two-file split is the point: users can copy/restore/diff `config.user.yaml`
in isolation, and the deploy default in `config.yaml` is never touched by the
UI. No DB needed — YAML is the right shape.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


def user_overlay_path(base_config_path: str | os.PathLike) -> Path:
    """Conventional location of the overlay file alongside `base_config_path`.

    `config.yaml` → `config.user.yaml`
    `homelabsage.yaml` → `homelabsage.user.yaml`
    `foo` → `foo.user`
    """
    p = Path(base_config_path)
    if p.suffix:
        return p.with_name(f"{p.stem}.user{p.suffix}")
    return p.with_name(f"{p.name}.user")


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Return a NEW dict: `base` recursively overridden by `overlay`.

    * Dict + dict → recurse.
    * Anything else → overlay wins (including list, scalar, None).
    * `base` is not mutated.
    """
    out = deepcopy(base)
    for key, value in overlay.items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(value, dict)
        ):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def overlay_keys(overlay: dict[str, Any], *, prefix: str = "") -> list[str]:
    """Flatten `overlay` to a list of dotted paths of every overridden leaf.

    Used by the UI to highlight which fields have been customised.

    >>> overlay_keys({"llm": {"model": "x"}, "scheduler": {"enabled": False}})
    ['llm.model', 'scheduler.enabled']
    """
    paths: list[str] = []
    for key, value in overlay.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            paths.extend(overlay_keys(value, prefix=f"{path}."))
        else:
            paths.append(path)
    return sorted(paths)


def load_overlay(path: Path) -> dict[str, Any]:
    """Read `path` as YAML. Empty / missing / non-dict → empty dict."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        log.warning("user overlay %s is malformed YAML, ignoring: %s", path, e)
        return {}
    if not isinstance(data, dict):
        log.warning("user overlay %s is not a dict at the top level, ignoring", path)
        return {}
    return data


def atomic_write_yaml(path: Path, data: dict[str, Any]) -> None:
    """Serialise `data` to `path` atomically when possible.

    Default path: write to `<path>.<rand>.tmp` in the same directory, then
    `os.replace` to the final name. That's atomic on Linux and Windows.

    Fallback path: when `path` is bind-mounted as a single file inside a
    container (common with Docker `-v host.yaml:/app/config.user.yaml`),
    `os.replace` raises EBUSY because the inode is pinned. In that case
    we fall back to a non-atomic in-place write to the same fd. The
    atomicity guarantee is lost, but the write still succeeds. We warn
    so users running in that configuration know to mount the PARENT
    directory if they need atomic overlay writes.

    Permissions are 0o600 — the overlay can hold the user's API keys,
    so it should not be world-readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    serialised = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)

    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as tmp:
        tmp.write(serialised)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    try:
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except OSError as e:
        # Don't leave a stray .tmp behind regardless of which path we take.
        tmp_path.unlink(missing_ok=True)
        if getattr(e, "errno", None) == 16:  # EBUSY — bind-mounted single file
            log.warning(
                "atomic_write_yaml: %s is bind-mounted (EBUSY on rename); "
                "falling back to non-atomic in-place write. Mount the parent "
                "directory instead of the file alone to restore atomicity.",
                path,
            )
            # Open the existing inode and overwrite in-place. Truncate first
            # so the new (potentially shorter) content doesn't leave trailing
            # bytes from the previous write.
            with open(path, "w", encoding="utf-8") as f:
                f.write(serialised)
                f.flush()
                os.fsync(f.fileno())
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
            return
        raise


# ─── dotted-path helpers (used by the settings PATCH/DELETE endpoints) ─────

def set_dotted(d: dict[str, Any], dotted: str, value: Any) -> dict[str, Any]:
    """Return a NEW dict with `value` set at `dotted` (e.g. `"llm.model"`).

    Intermediate dicts are created as needed. Existing siblings are preserved.
    """
    out = deepcopy(d)
    parts = dotted.split(".")
    cur = out
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value
    return out


def remove_dotted(d: dict[str, Any], dotted: str) -> dict[str, Any]:
    """Return a NEW dict with the leaf at `dotted` removed.

    Empty parent dicts created only as a side effect of the removal are also
    pruned, so a clean revert leaves no orphan `{}` blocks behind. Missing
    leaves are a no-op (not an error — the caller's intent is "make sure it's
    not there").
    """
    out = deepcopy(d)
    parts = dotted.split(".")
    stack: list[tuple[dict, str]] = []
    cur: Any = out
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            return out
        stack.append((cur, p))
        cur = cur[p]
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)
        # Prune empty parents
        for parent, key in reversed(stack):
            child = parent.get(key)
            if isinstance(child, dict) and not child:
                parent.pop(key, None)
            else:
                break
    return out
