"""Read `.renovaterc` / `renovate.json` from upstream repos.

Renovate is the de-facto auto-update bot for GitHub/Codeberg. When a
maintainer has tuned a `renovate.json` config they've encoded knowledge
the rest of the world doesn't see: which versions are safe to
auto-merge, which deps are pinned by policy, which release branches
they actively maintain.

That config is a strong "the maintainer says this is OK" signal that
the HomelabSage analyzer can use to ground its recommendation. We
don't follow the auto-merge rules literally — we just surface the
maintainer's intent on `Update.context.renovate` so the LLM mentions it.

Files we look for, in priority order:
  - `.renovaterc.json`     (modern)
  - `.renovaterc`          (legacy plain JSON)
  - `renovate.json`        (alternative root location)
  - `.github/renovate.json` (subdir convention)

We only PARSE the file. We don't try to interpret Renovate's full
schema — just extract the fields the analyzer can reason about:

  - `extends`        — preset configs the maintainer uses (e.g. `config:js-app`)
  - `automerge`      — boolean / "all" / per-rule toggle
  - `automergeType`  — "branch" / "pr" / "platform" hint
  - `packageRules`   — list summary (count + first few `matchPackagePatterns`)

Failure modes (no file / 404 / bad JSON) all return None silently.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

from .github import _headers, _resolve_api

log = logging.getLogger(__name__)


_CANDIDATE_PATHS: tuple[str, ...] = (
    ".renovaterc.json",
    ".renovaterc",
    "renovate.json",
    ".github/renovate.json",
)


@dataclass
class RenovateConfig:
    """What we attach to `Update.context.renovate`."""

    found_path: str
    automerge: object | None       # bool or None
    automerge_type: str | None
    extends: list[str]
    package_rules_count: int

    def to_context(self) -> dict:
        return {
            "found_path": self.found_path,
            "automerge": self.automerge,
            "automerge_type": self.automerge_type,
            "extends": self.extends[:10],
            "package_rules_count": self.package_rules_count,
        }


async def _fetch_raw(url: str, headers: dict, timeout: float = 15.0) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(url, headers=headers)
            if r.status_code != 200:
                return None
            return r.text
    except httpx.HTTPError as e:
        log.debug("renovate fetch %s failed: %s", url, e)
        return None


def parse_config(text: str) -> RenovateConfig | None:
    """Parse a JSON body. Returns None for unparseable input.

    Renovate also accepts JSON5 (comments, trailing commas) — when
    stdlib json fails we fall back to a forgiving cleanup pass that
    strips `//` and `/* */` comments. This catches ~90% of real-world
    JSON5 without depending on a 3rd-party parser.
    """
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Strip // line comments and /* block */ comments — covers the
        # JSON5 cases Renovate users actually write.
        import re
        stripped = re.sub(r"//[^\n]*", "", text)
        stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
        # Strip trailing commas before closing braces/brackets.
        stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    extends_raw = data.get("extends")
    extends: list[str] = []
    if isinstance(extends_raw, list):
        extends = [str(e) for e in extends_raw]
    elif isinstance(extends_raw, str):
        extends = [extends_raw]
    package_rules = data.get("packageRules")
    pr_count = len(package_rules) if isinstance(package_rules, list) else 0
    return RenovateConfig(
        found_path="(parsed)",
        automerge=data.get("automerge"),
        automerge_type=(
            str(data["automergeType"]) if isinstance(data.get("automergeType"), str)
            else None
        ),
        extends=extends,
        package_rules_count=pr_count,
    )


async def fetch_renovate_config(repo: str) -> RenovateConfig | None:
    """Try every known location until one returns 200.

    `repo` is `owner/name` for GitHub or `codeberg.org/owner/name` for
    Codeberg (the shape `_resolve_api` already understands).
    """
    api_base, slug = _resolve_api(repo)
    headers = _headers(api_base)
    for path in _CANDIDATE_PATHS:
        # Codeberg/Gitea expose raw files at `<root>/<owner>/<repo>/raw/HEAD/<file>`;
        # `_resolve_api` returns `<root>/api/v1`, so we trim that suffix.
        codeberg_root = api_base.removesuffix("/api/v1")
        raw_url = (
            f"https://raw.githubusercontent.com/{slug}/HEAD/{path}"
            if api_base == "https://api.github.com"
            else f"{codeberg_root}/{slug}/raw/HEAD/{path}"
        )
        text = await _fetch_raw(raw_url, headers)
        if text is None:
            continue
        cfg = parse_config(text)
        if cfg is not None:
            cfg.found_path = path
            return cfg
    return None


__all__ = [
    "RenovateConfig",
    "fetch_renovate_config",
    "parse_config",
]
