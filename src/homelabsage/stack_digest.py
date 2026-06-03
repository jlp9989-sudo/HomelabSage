"""Stack-level grouped digest.

When 5 services in the same compose project all have updates at once
(common: a `docker compose pull` weekly cron, an LSIO bulk-tag push,
the user upgrading half their library at once), the engine emits 5
separate push notifications. With severity batching that already
collapses *low-severity* updates, but high/critical items still fire
per-item.

This module groups by `Update.context.compose_project` (which the
docker plugin already attaches) and emits ONE rollup per project
when ≥ `min_group_size` updates land. Each project's bullet is the
container subject + version diff + severity emoji. The other push
outputs see the rollup as a single high-severity Update through the
existing batch path.

The grouping is opt-in via `outputs.batching.stack_group` so users
who prefer per-item notifications get unchanged behaviour.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .models import AnalyzedUpdate

_SEVERITY_EMOJI = {
    "critical": "🔴", "high": "🟠", "medium": "🟡", "info": "🔵",
}


@dataclass
class StackGroup:
    """One per compose project."""

    project: str
    items: list[AnalyzedUpdate] = field(default_factory=list)

    @property
    def max_severity(self) -> str:
        order = {"info": 0, "medium": 1, "high": 2, "critical": 3}
        best = "info"
        for it in self.items:
            if it.analysis is None:
                continue
            if order.get(it.analysis.severity.value, 0) > order[best]:
                best = it.analysis.severity.value
        return best


def group_by_project(
    items: list[AnalyzedUpdate],
    *,
    min_group_size: int = 3,
) -> tuple[list[StackGroup], list[AnalyzedUpdate]]:
    """Split items into (grouped_by_project, ungroupable_singletons).

    A project counts as groupable when it has ≥ `min_group_size`
    items in the input list. Anything below threshold stays in the
    `singletons` return so the caller can dispatch them normally.
    """
    by_project: dict[str, list[AnalyzedUpdate]] = defaultdict(list)
    no_project: list[AnalyzedUpdate] = []
    for it in items:
        proj = (it.update.context or {}).get("compose_project") or ""
        if not isinstance(proj, str) or not proj:
            no_project.append(it)
            continue
        by_project[proj].append(it)

    groups: list[StackGroup] = []
    singletons: list[AnalyzedUpdate] = list(no_project)
    for proj, lst in by_project.items():
        if len(lst) >= min_group_size:
            groups.append(StackGroup(project=proj, items=lst))
        else:
            singletons.extend(lst)
    return groups, singletons


def render_group_markdown(group: StackGroup) -> str:
    """Render one group's rollup as Markdown. Used by the push outputs."""
    lines = [
        f"## Stack `{group.project}` — {len(group.items)} updates pending",
    ]
    for it in group.items:
        emoji = _SEVERITY_EMOJI.get(
            it.analysis.severity.value if it.analysis else "info", "•",
        )
        u = it.update
        lines.append(
            f"- {emoji} **{u.subject}**  `{u.current_version}` → "
            f"`{u.new_version}`"
        )
        if it.analysis and it.analysis.summary:
            lines.append(f"  - {it.analysis.summary[:140]}")
    return "\n".join(lines)


def render_groups_summary(groups: list[StackGroup]) -> str:
    """Render the whole group set as one Markdown body."""
    if not groups:
        return ""
    parts = [
        f"# Stack-level update digest — {len(groups)} stack(s) affected",
        "",
    ]
    for g in groups:
        parts.append(render_group_markdown(g))
        parts.append("")
    return "\n".join(parts)


__all__ = [
    "StackGroup",
    "group_by_project",
    "render_group_markdown",
    "render_groups_summary",
]
