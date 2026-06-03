"""Stack-level rollback recipe generator.

When something breaks after an update, the second question (after "what
broke?") is "how do I get back to the previous version *right now*?".
This module answers that mechanically — given any `AnalyzedUpdate`, it
emits a copy-pasteable recipe:

  docker stop <container>
  docker rm <container>
  docker pull <image>@<prior-digest>
  docker run [reconstructed cli]
    -- OR --
  cd <compose-dir>
  sed -i 's|<new-image>|<prior-image>|' compose.yaml
  docker compose up -d <service>

The prior-image+digest comes from `Update.current_version` /
`Update.context["image"]` which the docker plugin already records on
every scan. No new state — we're just surfacing what's there.

We deliberately produce TWO forms:

  - docker CLI form (always works, even for portainer / docker run
    setups where there's no compose file)
  - docker compose form (when the source's container was managed by
    compose AND the linter has discovered the file)

This is pure data — no LLM, no docker socket. The user runs the
commands themselves. We never offer to apply automatically because a
broken update on a critical service is the wrong moment for the tool to
take destructive action without explicit confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .compose import DependencyGraph
from .models import AnalyzedUpdate


@dataclass
class RollbackRecipe:
    """One container's rollback plan. Renderable to Markdown."""

    container_name: str
    prior_image: str
    new_image: str | None
    cli_steps: list[str]
    compose_steps: list[str]
    compose_file: Path | None
    cascade_warnings: list[str]


def _prior_image_ref(item: AnalyzedUpdate) -> str:
    """Derive the prior image:tag from the Update's current_version.

    For container updates the plugin sets `context.image` to the new
    image; the prior is constructed as `<registry_slug>:<old-tag>`.
    Floating-tag containers carry digests so we use `<slug>@<digest>`.
    """
    ctx = item.update.context or {}
    slug = ctx.get("registry_slug") or item.update.subject
    # Floating-tag pipeline: `current_version` = `"local @ <digest12>"`
    cv = item.update.current_version or ""
    if cv.startswith("local @ "):
        digest = cv.removeprefix("local @ ").strip()
        # Re-expand to a full sha256 reference. Docker accepts the
        # 12-char prefix when the image is local, but for pulling we
        # need the full digest the user has on disk — they should pull
        # it via `docker images --digests` and substitute. We document
        # this in the rendered recipe.
        return f"{slug}@sha256:{digest}<full-digest-needed>"
    if cv == "(unknown)":
        return f"{slug}:<previous-tag-needed>"
    # Plain semver tag
    return f"{slug}:{cv}"


def _new_image_ref(item: AnalyzedUpdate) -> str | None:
    ctx = item.update.context or {}
    image = ctx.get("image")
    if isinstance(image, str):
        return image
    return None


def build_recipe(
    item: AnalyzedUpdate,
    *,
    graph: DependencyGraph | None = None,
) -> RollbackRecipe:
    """Build the rollback recipe for a single update.

    `graph` is the compose graph from `compose.build_graph(...)`. When
    provided, the recipe includes a compose-file path + service name +
    `cascade_warnings` listing services that depend on this one (and
    therefore also need a restart).
    """
    container_name = item.update.subject
    prior = _prior_image_ref(item)
    new = _new_image_ref(item)

    # docker CLI form is always offered — it's the universal escape hatch.
    cli_steps: list[str] = [
        f"docker stop {container_name}",
        f"docker rm {container_name}",
        f"docker pull {prior}",
        f"docker run -d --name {container_name} {prior}  "
        "# reconstruct -p/-v/-e from `docker inspect <name>` before running",
    ]

    compose_steps: list[str] = []
    compose_file: Path | None = None
    cascade_warnings: list[str] = []
    if graph is not None:
        svc = graph.services.get(container_name) or next(
            (s for s in graph.services.values() if s.container_name == container_name),
            None,
        )
        if svc is not None and svc.file is not None:
            compose_file = svc.file
            project_dir = svc.file.parent
            compose_steps = [
                f"cd {project_dir}",
                (
                    f"sed -i.bak 's|image: {new or '<new-image>'}|"
                    f"image: {prior}|' {svc.file.name}"
                ),
                f"docker compose up -d {svc.name}",
            ]
            for dep in graph.dependents_of(container_name):
                cascade_warnings.append(
                    f"`{dep.name}` declares `depends_on: {svc.name}` — restart it after "
                    "the rollback completes."
                )

    return RollbackRecipe(
        container_name=container_name,
        prior_image=prior,
        new_image=new,
        cli_steps=cli_steps,
        compose_steps=compose_steps,
        compose_file=compose_file,
        cascade_warnings=cascade_warnings,
    )


def render_markdown(recipe: RollbackRecipe) -> str:
    """Render a `RollbackRecipe` to a copy-pasteable Markdown body."""
    lines: list[str] = [
        f"# Rollback recipe — `{recipe.container_name}`",
        "",
        f"Prior image: `{recipe.prior_image}`",
    ]
    if recipe.new_image:
        lines.append(f"Currently running: `{recipe.new_image}`")
    lines.append("")

    if recipe.compose_steps:
        lines += [
            "## Option A — docker compose (preferred)",
            f"_Compose file: `{recipe.compose_file}`_",
            "",
            "```sh",
            *recipe.compose_steps,
            "```",
            "",
        ]

    lines += [
        "## " + ("Option B — " if recipe.compose_steps else "")
        + "docker CLI (always works)",
        "",
        "```sh",
        *recipe.cli_steps,
        "```",
    ]

    if recipe.cascade_warnings:
        lines += [
            "",
            "## Cascade — also restart these",
            "",
        ]
        lines.extend(f"- {w}" for w in recipe.cascade_warnings)

    if "<full-digest-needed>" in recipe.prior_image:
        lines += [
            "",
            "> ⚠️  The prior digest is truncated to 12 chars. Run "
            "`docker images --digests | grep " + recipe.container_name.split('-')[0] + "` "
            "and substitute the full `sha256:...` before pulling.",
        ]
    elif "<previous-tag-needed>" in recipe.prior_image:
        lines += [
            "",
            "> ⚠️  The previous tag wasn't recorded (this looks like a "
            "first-scan upgrade). Check your registry or compose-file history.",
        ]
    return "\n".join(lines)


__all__ = ["RollbackRecipe", "build_recipe", "render_markdown"]
