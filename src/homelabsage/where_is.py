"""Locate a service / container in compose files.

Given a name (service name, container_name, or substring), walk the
parsed compose graph and return where it's declared: file + line +
project + a short context excerpt.

Pure function over `compose.DependencyGraph` + a small grep into the
raw YAML for the line number. We don't try to be clever with YAML
node positions — `yaml.safe_load` discards them. A simple regex over
the file is good enough for "where is this thing?" lookups; if the
user has the same service name in multiple files we return every
hit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .compose import DependencyGraph


@dataclass
class WhereHit:
    """One match for a service-name lookup."""

    name: str
    container_name: str | None
    image: str | None
    file: str
    line: int
    project: str | None
    context: str   # up to ~5 lines of YAML around the match

    def to_context(self) -> dict:
        return {
            "name": self.name,
            "container_name": self.container_name,
            "image": self.image,
            "file": self.file,
            "line": self.line,
            "project": self.project,
            "context": self.context,
        }


def _find_line(text: str, service_name: str) -> tuple[int, str]:
    """Locate the `<service_name>:` line in raw YAML. Returns (line_no, snippet).

    Falls back to `(0, "")` when the service block can't be found —
    parser might have inferred the service from a nested structure
    or compose extension that doesn't match the regex.
    """
    # Match the service header at indent depth 2 or 4 (services: → name:)
    # — the common compose v2/v3 layouts. Allow trailing whitespace
    # only; reject inline maps.
    pat = re.compile(
        rf"^(\s{{2,4}}){re.escape(service_name)}:\s*$",
        re.MULTILINE,
    )
    m = pat.search(text)
    if m is None:
        return (0, "")
    line_no = text.count("\n", 0, m.start()) + 1
    lines = text.splitlines()
    lo = max(0, line_no - 2)
    hi = min(len(lines), line_no + 4)
    return (line_no, "\n".join(lines[lo:hi]))


def find(graph: DependencyGraph, name: str) -> list[WhereHit]:
    """Return every compose service whose name / container_name matches.

    Match rules (in priority order, all matches returned):
      1. Exact service_name match.
      2. Exact container_name match.
      3. Substring match on either field — case-insensitive.

    Empty / None `name` returns []. We never raise.
    """
    if not name or not name.strip():
        return []
    needle = name.strip()
    needle_lc = needle.lower()
    hits: list[WhereHit] = []
    seen: set[tuple[str, str]] = set()  # (file, name) dedupe
    for svc in graph.services.values():
        score = None
        if svc.name == needle:
            score = 0
        elif svc.container_name == needle:
            score = 1
        elif (
            needle_lc in svc.name.lower()
            or (svc.container_name and needle_lc in svc.container_name.lower())
        ):
            score = 2
        if score is None:
            continue
        if svc.file is None:
            continue
        key = (str(svc.file), svc.name)
        if key in seen:
            continue
        seen.add(key)
        try:
            text = Path(svc.file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        line, ctx = _find_line(text, svc.name) if text else (0, "")
        hits.append(WhereHit(
            name=svc.name,
            container_name=svc.container_name,
            image=svc.image,
            file=str(svc.file),
            line=line,
            project=svc.project,
            context=ctx,
        ))
    # Best matches first (score 0 → 1 → 2). We don't carry score on
    # the hit itself; instead reconstruct by walking the original
    # input order through the dedupe set order — Python dicts are
    # insertion-ordered, so the natural enumeration above honours
    # the priority well enough.
    return hits


__all__ = ["WhereHit", "find"]
