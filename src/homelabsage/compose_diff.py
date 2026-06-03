"""Compose-file update-diff generator.

Given an `AnalyzedUpdate` for a docker container, produce a unified diff
that shows the user EXACTLY which line of EXACTLY which compose file
would change if they applied the update. Pure preview — never writes.

The diff is built mechanically:

  1. Find the service in the compose graph that matches the container.
  2. Read the current YAML body.
  3. Replace the `image:` line for that service with the new tag.
  4. Run difflib.unified_diff between the two strings.

We replace ONLY the literal `image:` line and ONLY for the target
service — no smart YAML rewriting. The result is something the user
can `patch -p0 < the.diff` if they want, or paste into a PR. Keeping it
text-level avoids the trap where roundtripping YAML reflows comments and
re-quotes strings the user wrote a specific way.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

from .compose import DependencyGraph
from .models import AnalyzedUpdate


@dataclass
class ComposeDiff:
    """One container, one compose file, one diff."""

    container_name: str
    service_name: str
    file: Path
    old_image: str
    new_image: str
    diff: str          # unified diff body, empty when no change found


def _find_service(
    graph: DependencyGraph, container_name: str,
):
    """Return the ComposeService matching `container_name` (by service
    name or container_name alias), or None when not found."""
    svc = graph.services.get(container_name)
    if svc is not None:
        return svc
    for s in graph.services.values():
        if s.container_name == container_name:
            return s
    return None


def _swap_image_line(
    body: str, service_name: str, new_image: str,
) -> tuple[str, str | None]:
    """Replace the first `image:` line inside `service_name:` with `new_image`.

    Returns `(new_body, old_image_string)`. `old_image_string` is None
    when no replacement happened (service not found, or no `image:` line).

    We do this with a stateful walk rather than YAML round-trip so the
    user's comments / indentation / quoting style stay intact.
    """
    out_lines: list[str] = []
    in_service = False
    service_indent = -1
    found_old: str | None = None
    service_header_re = re.compile(rf"^(\s*){re.escape(service_name)}:\s*(?:#.*)?$")
    image_line_re = re.compile(r"^(\s*)image:\s*(.+?)(\s*(?:#.*)?)$")
    for line in body.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        m = service_header_re.match(stripped)
        if m:
            in_service = True
            service_indent = len(m.group(1))
            out_lines.append(line)
            continue
        if in_service:
            # Exit when we hit a less-indented non-blank line — that's
            # a new top-level key or sibling service.
            if stripped.strip() and not stripped.startswith(" " * (service_indent + 1)):
                in_service = False
            if in_service and found_old is None:
                im = image_line_re.match(stripped)
                if im:
                    found_old = im.group(2).strip().strip("'\"")
                    new_line = f"{im.group(1)}image: {new_image}{im.group(3)}\n"
                    out_lines.append(new_line)
                    continue
        out_lines.append(line)
    return "".join(out_lines), found_old


def build_diff(
    item: AnalyzedUpdate,
    *,
    graph: DependencyGraph,
) -> ComposeDiff | None:
    """Return a `ComposeDiff` or None when no compose file backs the container."""
    container_name = item.update.subject
    svc = _find_service(graph, container_name)
    if svc is None or svc.file is None:
        return None
    ctx = item.update.context or {}
    new_image = ctx.get("image")
    if not isinstance(new_image, str):
        # Build the new image from the slug + new_version when context
        # didn't carry the full ref (older floating-tag rows).
        slug = ctx.get("registry_slug") or container_name
        new_image = f"{slug}:{item.update.new_version}"
    try:
        body = svc.file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    new_body, old_image = _swap_image_line(body, svc.name, new_image)
    if old_image is None:
        return ComposeDiff(
            container_name=container_name, service_name=svc.name,
            file=svc.file, old_image="(not found)", new_image=new_image,
            diff="",
        )
    diff = "".join(difflib.unified_diff(
        body.splitlines(keepends=True),
        new_body.splitlines(keepends=True),
        fromfile=f"{svc.file.name} (current)",
        tofile=f"{svc.file.name} (after upgrade)",
        n=2,
    ))
    return ComposeDiff(
        container_name=container_name, service_name=svc.name,
        file=svc.file, old_image=old_image, new_image=new_image,
        diff=diff,
    )


def render_markdown(diff: ComposeDiff) -> str:
    lines: list[str] = [
        f"# Compose diff for `{diff.container_name}`",
        "",
        f"File: `{diff.file}`",
        f"Service: `{diff.service_name}`",
        f"Image change: `{diff.old_image}` → `{diff.new_image}`",
        "",
    ]
    if not diff.diff:
        lines.append(
            "_No `image:` line found for this service — the compose file may "
            "use `build:` instead, or the service name doesn't match._"
        )
        return "\n".join(lines)
    lines += ["```diff", diff.diff.rstrip("\n"), "```"]
    return "\n".join(lines)


__all__ = ["ComposeDiff", "build_diff", "render_markdown"]
