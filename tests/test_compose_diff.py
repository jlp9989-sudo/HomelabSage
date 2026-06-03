"""Tests for the compose-file update-diff generator."""

from __future__ import annotations

from pathlib import Path

from homelabsage.compose import ComposeService, DependencyGraph
from homelabsage.compose_diff import (
    _swap_image_line,
    build_diff,
    render_markdown,
)
from homelabsage.models import AnalyzedUpdate, Update


def _make_graph(file: Path, service_name: str, container_name: str | None = None):
    svc = ComposeService(
        name=service_name,
        container_name=container_name or service_name,
        image=None, file=file,
    )
    return DependencyGraph(services={service_name: svc}, _dependents={})


def _item(container: str, new_image: str, new_version: str) -> AnalyzedUpdate:
    return AnalyzedUpdate(update=Update(
        source="docker", subject=container,
        current_version="1.0", new_version=new_version,
        context={"image": new_image, "registry_slug": new_image.rsplit(":", 1)[0]},
    ))


# ─── _swap_image_line ──────────────────────────────────────────────


def test_swap_image_line_basic():
    body = (
        "services:\n"
        "  mealie:\n"
        "    image: mealie:1.0\n"
        "    restart: unless-stopped\n"
        "  db:\n"
        "    image: postgres:15\n"
    )
    new, old = _swap_image_line(body, "mealie", "mealie:2.0")
    assert old == "mealie:1.0"
    assert "mealie:2.0" in new
    assert "postgres:15" in new            # db unchanged


def test_swap_image_line_only_first_service_match():
    """A service header with the same name in a sibling stack must not match."""
    body = (
        "services:\n"
        "  mealie:\n"
        "    image: mealie:1.0\n"
    )
    new, old = _swap_image_line(body, "other", "x:2")
    assert old is None
    assert new == body


def test_swap_image_line_handles_quoted_image():
    body = (
        "services:\n"
        "  app:\n"
        "    image: 'mealie:1.0'\n"
    )
    new, old = _swap_image_line(body, "app", "mealie:2.0")
    assert old == "mealie:1.0"
    assert "mealie:2.0" in new


# ─── build_diff ────────────────────────────────────────────────────


def test_build_diff_returns_none_when_no_compose(tmp_path: Path):
    graph = DependencyGraph(services={}, _dependents={})
    item = _item("mealie", "mealie:2.0", "2.0")
    assert build_diff(item, graph=graph) is None


def test_build_diff_produces_unified_diff(tmp_path: Path):
    f = tmp_path / "compose.yaml"
    f.write_text(
        "services:\n  mealie:\n    image: mealie:1.0\n", encoding="utf-8",
    )
    graph = _make_graph(f, "mealie")
    diff = build_diff(_item("mealie", "mealie:2.0", "2.0"), graph=graph)
    assert diff is not None
    assert diff.old_image == "mealie:1.0"
    assert diff.new_image == "mealie:2.0"
    assert "-    image: mealie:1.0" in diff.diff
    assert "+    image: mealie:2.0" in diff.diff


def test_build_diff_falls_back_when_image_line_absent(tmp_path: Path):
    """Service exists in graph but compose uses `build:` not `image:`."""
    f = tmp_path / "compose.yaml"
    f.write_text(
        "services:\n  mealie:\n    build: .\n", encoding="utf-8",
    )
    graph = _make_graph(f, "mealie")
    diff = build_diff(_item("mealie", "mealie:2.0", "2.0"), graph=graph)
    assert diff is not None
    assert diff.old_image == "(not found)"
    assert diff.diff == ""


def test_render_markdown_includes_diff_block(tmp_path: Path):
    f = tmp_path / "compose.yaml"
    f.write_text(
        "services:\n  mealie:\n    image: mealie:1.0\n", encoding="utf-8",
    )
    graph = _make_graph(f, "mealie")
    diff = build_diff(_item("mealie", "mealie:2.0", "2.0"), graph=graph)
    md = render_markdown(diff)
    assert "```diff" in md
    assert "mealie:1.0" in md
    assert "mealie:2.0" in md


def test_render_markdown_explains_when_no_diff(tmp_path: Path):
    f = tmp_path / "compose.yaml"
    f.write_text(
        "services:\n  mealie:\n    build: .\n", encoding="utf-8",
    )
    graph = _make_graph(f, "mealie")
    diff = build_diff(_item("mealie", "mealie:2.0", "2.0"), graph=graph)
    md = render_markdown(diff)
    assert "No `image:` line" in md


def test_build_diff_resolves_container_name_alias(tmp_path: Path):
    """Service in graph is `app`, container_name is `mealie-prod`."""
    f = tmp_path / "compose.yaml"
    f.write_text(
        "services:\n  app:\n    container_name: mealie-prod\n"
        "    image: app:1\n",
        encoding="utf-8",
    )
    graph = _make_graph(f, "app", container_name="mealie-prod")
    diff = build_diff(_item("mealie-prod", "app:2", "2"), graph=graph)
    assert diff is not None
    assert diff.service_name == "app"
    assert "app:2" in diff.new_image
