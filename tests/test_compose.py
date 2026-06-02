"""Tests for the docker-compose dependency graph builder."""

from __future__ import annotations

from pathlib import Path

from homelabsage.compose import (
    build_graph,
    parse_compose_file,
)

COMPOSE_A = """\
services:
  postgres:
    image: postgres:16
    container_name: db
    networks: [internal]
  api:
    image: my/api:1
    container_name: api
    depends_on: [postgres]
    networks: [internal, public]
  web:
    image: nginx
    container_name: web
    depends_on:
      api:
        condition: service_healthy
      cache:
        condition: service_started
    networks: [public]
  cache:
    image: redis:7
    networks: [internal]
"""


COMPOSE_B = """\
services:
  worker:
    image: my/worker:2
    container_name: worker_b
    depends_on:
      - postgres
"""


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_parse_compose_file_handles_short_and_long_depends_on(tmp_path: Path):
    f = tmp_path / "compose.yaml"
    f.write_text(COMPOSE_A)
    services = parse_compose_file(f)
    by_name = {s.name: s for s in services}
    # short form
    assert by_name["api"].depends_on == ["postgres"]
    # long form (dict)
    assert sorted(by_name["web"].depends_on) == ["api", "cache"]


def test_parse_compose_file_returns_empty_on_bad_yaml(tmp_path: Path):
    f = tmp_path / "compose.yaml"
    f.write_text(":\n  - invalid: : :")
    assert parse_compose_file(f) == []


def test_parse_compose_file_returns_empty_on_missing_services(tmp_path: Path):
    f = tmp_path / "compose.yaml"
    f.write_text("version: '3'\nnetworks:\n  foo: {}\n")
    assert parse_compose_file(f) == []


def test_build_graph_dependents_short_path(tmp_path: Path):
    _write(tmp_path / "stack-a" / "compose.yaml", COMPOSE_A)
    graph = build_graph([tmp_path])
    # api depends on postgres → dependents_of("postgres") includes "api"
    deps = graph.dependents_of("postgres")
    assert {d.name for d in deps} == {"api"}


def test_build_graph_resolves_container_name_alias(tmp_path: Path):
    """Docker only knows `container_name`. The plugin queries by container_name."""
    _write(tmp_path / "stack-a" / "compose.yaml", COMPOSE_A)
    graph = build_graph([tmp_path])
    # container_name='db' → service='postgres' → dependent 'api'
    deps = graph.dependents_of("db")
    assert {d.name for d in deps} == {"api"}


def test_build_graph_dedups_across_files(tmp_path: Path):
    _write(tmp_path / "stack-a" / "compose.yaml", COMPOSE_A)
    _write(tmp_path / "stack-b" / "compose.yaml", COMPOSE_B)
    graph = build_graph([tmp_path])
    # Both files declare a service depending on postgres
    deps = graph.dependents_of("postgres")
    names = sorted(d.name for d in deps)
    assert names == ["api", "worker"]


def test_network_neighbours_excludes_default_and_self(tmp_path: Path):
    _write(tmp_path / "stack-a" / "compose.yaml", COMPOSE_A)
    graph = build_graph([tmp_path])
    # `api` shares `internal` with postgres+cache, `public` with web
    neighbours = {n.name for n in graph.network_neighbours_of("api")}
    assert neighbours == {"postgres", "cache", "web"}


def test_network_neighbours_returns_empty_for_unknown_service(tmp_path: Path):
    _write(tmp_path / "stack-a" / "compose.yaml", COMPOSE_A)
    graph = build_graph([tmp_path])
    assert graph.network_neighbours_of("nonexistent") == []


def test_build_graph_skips_missing_roots(tmp_path: Path):
    """A configured-but-missing path is logged and ignored, not raised."""
    graph = build_graph([tmp_path / "does-not-exist", tmp_path])
    assert graph.services == {}


def test_compose_files_with_legacy_filenames_are_picked_up(tmp_path: Path):
    """Old `docker-compose.yml` filename must still be discovered."""
    legacy = tmp_path / "old" / "docker-compose.yml"
    _write(legacy, COMPOSE_B)
    graph = build_graph([tmp_path])
    assert "worker" in graph.services
