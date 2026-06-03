"""Tests for the stack-level rollback recipe generator."""

from __future__ import annotations

from pathlib import Path

from homelabsage.compose import ComposeService, DependencyGraph
from homelabsage.models import Analysis, AnalyzedUpdate, Severity, Update
from homelabsage.rollback import RollbackRecipe, build_recipe, render_markdown


def _item(
    *,
    container: str = "mealie",
    current: str = "1.0",
    new: str = "2.0",
    image: str = "ghcr.io/mealie/mealie:2.0",
    registry_slug: str = "ghcr.io/mealie/mealie",
) -> AnalyzedUpdate:
    return AnalyzedUpdate(
        update=Update(
            source="docker", subject=container,
            current_version=current, new_version=new,
            context={"image": image, "registry_slug": registry_slug},
        ),
        analysis=Analysis(severity=Severity.MEDIUM, summary="upgrade"),
    )


def test_build_recipe_cli_form_always_present():
    recipe = build_recipe(_item())
    assert any("docker stop mealie" in s for s in recipe.cli_steps)
    assert any("docker rm mealie" in s for s in recipe.cli_steps)
    assert recipe.prior_image == "ghcr.io/mealie/mealie:1.0"
    assert recipe.new_image == "ghcr.io/mealie/mealie:2.0"
    # No compose graph passed → no compose form
    assert recipe.compose_steps == []
    assert recipe.compose_file is None


def test_build_recipe_includes_compose_form(tmp_path: Path):
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text(
        "services:\n  mealie:\n    image: ghcr.io/mealie/mealie:2.0\n",
        encoding="utf-8",
    )
    svc = ComposeService(
        name="mealie", container_name="mealie",
        image="ghcr.io/mealie/mealie:2.0",
        file=compose_path,
    )
    graph = DependencyGraph(services={"mealie": svc}, _dependents={})
    recipe = build_recipe(_item(), graph=graph)
    assert recipe.compose_file == compose_path
    assert any("docker compose up -d mealie" in s for s in recipe.compose_steps)
    assert any("sed -i" in s for s in recipe.compose_steps)


def test_build_recipe_cascade_warnings(tmp_path: Path):
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text("services:\n  db:\n    image: pg:15\n",
                            encoding="utf-8")
    svc_db = ComposeService(name="db", container_name="db",
                            image="pg:15", file=compose_path)
    svc_app = ComposeService(name="app", container_name="app",
                             image="app:1", file=compose_path,
                             depends_on=["db"])
    graph = DependencyGraph(
        services={"db": svc_db, "app": svc_app},
        _dependents={"db": {"app"}},
    )
    recipe = build_recipe(_item(container="db"), graph=graph)
    assert any("app" in w for w in recipe.cascade_warnings)


def test_build_recipe_floating_tag_full_digest_warning():
    """Floating-tag updates carry the prior digest truncated to 12 chars.
    The prior_image stays a valid docker ref shape; the digest hint
    travels in a separate field."""
    upd = AnalyzedUpdate(update=Update(
        source="docker", subject="watchtower",
        current_version="local @ abc123def456",
        new_version="registry @ 999abc",
        context={"registry_slug": "containrrr/watchtower"},
    ))
    recipe = build_recipe(upd)
    assert "<previous-digest-needed>" in recipe.prior_image
    assert recipe.prior_digest_short == "abc123def456"
    md = render_markdown(recipe)
    assert "abc123def456" in md
    assert "docker images --digests" in md


def test_render_markdown_unknown_prior_warns():
    upd = AnalyzedUpdate(update=Update(
        source="docker", subject="x",
        current_version="(unknown)", new_version="2.0",
        context={"registry_slug": "foo/bar"},
    ))
    recipe = build_recipe(upd)
    md = render_markdown(recipe)
    assert "previous tag wasn't recorded" in md


def test_render_markdown_includes_both_options(tmp_path: Path):
    """When compose form is present, label both Option A and Option B."""
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text("services:\n  mealie:\n    image: x:1\n",
                            encoding="utf-8")
    svc = ComposeService(name="mealie", container_name="mealie",
                         image="x:1", file=compose_path)
    graph = DependencyGraph(services={"mealie": svc}, _dependents={})
    recipe = build_recipe(_item(), graph=graph)
    md = render_markdown(recipe)
    assert "Option A — docker compose" in md
    assert "Option B — docker CLI" in md


def test_recipe_dataclass_round_trip():
    r = RollbackRecipe(
        container_name="x", prior_image="x:1", new_image="x:2",
        cli_steps=["a", "b"], compose_steps=[],
        compose_file=None, cascade_warnings=[],
    )
    md = render_markdown(r)
    assert "x:1" in md
    assert "docker CLI" in md
