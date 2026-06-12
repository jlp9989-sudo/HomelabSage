"""Auto-configuration: detection, additive merge, validated apply, web flow.

The contract under test is the product's core promise applied to config:
the machine proposes with evidence, nothing is written until a human
applies, and the user's own entries always survive a merge.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from homelabsage.autoconfig import (
    Proposal,
    _collapse_dirs,
    apply_proposals,
    build_proposals,
    detect_compose_paths,
    detect_traefik_hosts,
)
from homelabsage.config import Config


class _FakeContainer:
    def __init__(self, labels: dict[str, str]):
        self.attrs = {"Config": {"Labels": labels}}


class _FakeDocker:
    """Minimal docker-SDK double: containers.list + info()."""

    def __init__(self, containers=(), root_dir="/var/lib/docker"):
        self._containers = list(containers)
        self._root = root_dir
        self.containers = self

    def list(self, all=True):  # noqa: A002 — mirrors the SDK kwarg
        return self._containers

    def info(self):
        return {"DockerRootDir": self._root}


def _compose_container(path: str) -> _FakeContainer:
    return _FakeContainer(
        {"com.docker.compose.project.config_files": path},
    )


# ─── pure detection ───────────────────────────────────────────────


def test_detect_compose_paths_collapses_stack_siblings():
    containers = [
        _compose_container("/opt/stacks/mealie/compose.yaml"),
        _compose_container("/opt/stacks/immich/compose.yaml"),
        _compose_container("/srv/solo/docker-compose.yml"),
        _FakeContainer({}),   # not compose-managed → ignored
    ]
    dirs, n = detect_compose_paths(containers)
    # Two siblings under /opt/stacks collapse to the root; the loner stays.
    assert dirs == ["/opt/stacks", "/srv/solo"]
    assert n == 3


def test_detect_compose_paths_multi_file_label():
    # compose -f a.yml -f override.yml → comma-separated label.
    c = _compose_container("/x/app/a.yml,/x/app/override.yml")
    dirs, n = detect_compose_paths([c])
    assert dirs == ["/x/app"]
    assert n == 1


def test_collapse_never_emits_filesystem_root():
    assert _collapse_dirs(["/a", "/b"]) == ["/a", "/b"]


def test_detect_traefik_hosts():
    c = _FakeContainer({
        "traefik.http.routers.web.rule": "Host(`app.example.com`)",
        "traefik.http.routers.web2.rule": "Host(`b.example.com`) && PathPrefix(`/x`)",
        "unrelated.label": "Host(`not-traefik.example.com`)",
    })
    assert detect_traefik_hosts([c]) == ["app.example.com", "b.example.com"]


# ─── build_proposals ──────────────────────────────────────────────


def test_build_proposals_full_detection():
    cfg = Config()
    client = _FakeDocker(
        containers=[
            _compose_container("/opt/stacks/mealie/compose.yaml"),
            _compose_container("/opt/stacks/immich/compose.yaml"),
        ],
        root_dir="/var/lib/docker",
    )
    proposals = {p.dotted: p for p in build_proposals(cfg, docker_client=client)}

    assert proposals["sources.docker.compose_scan_paths"].value == ["/opt/stacks"]
    assert proposals["disk_pressure.paths"].value == ["/var/lib/docker"]
    # Cheap detectors proposed ON; fit-check rides on the disk-path proposal.
    assert proposals["sources.docker.image_size_growth_detect"].value is True
    assert proposals["sources.docker.image_fit_check"].value is True
    # Every proposal carries evidence the user can judge.
    assert all(p.evidence for p in proposals.values())


def test_build_proposals_additive_merge_preserves_user_entries():
    cfg = Config()
    cfg.sources.docker.compose_scan_paths = ["/home/me/special"]
    client = _FakeDocker(
        containers=[
            _compose_container("/opt/stacks/a/compose.yaml"),
            _compose_container("/opt/stacks/b/compose.yaml"),
        ],
    )
    (p,) = [
        p for p in build_proposals(cfg, docker_client=client)
        if p.dotted == "sources.docker.compose_scan_paths"
    ]
    # User entry first and intact; detection appended.
    assert p.value == ["/home/me/special", "/opt/stacks"]


def test_build_proposals_nothing_when_config_already_matches(monkeypatch):
    # Parity detection reads the real host (/boot/config, /proc/mdstat) —
    # pin it so the test is deterministic on any machine, including an
    # actual Unraid box.
    import homelabsage.autoconfig as mod
    monkeypatch.setattr(mod, "detect_parity_capable", lambda: None)

    cfg = Config()
    cfg.sources.docker.compose_scan_paths = ["/opt/stacks"]
    cfg.disk_pressure.paths = ["/var/lib/docker"]
    cfg.sources.docker.image_size_growth_detect = True
    cfg.sources.docker.image_fit_check = True
    client = _FakeDocker(
        containers=[
            _compose_container("/opt/stacks/a/compose.yaml"),
            _compose_container("/opt/stacks/b/compose.yaml"),
        ],
    )
    assert build_proposals(cfg, docker_client=client) == []


def test_build_proposals_no_docker_socket_degrades_quietly(monkeypatch):
    """No socket → no docker-derived proposals, no exception."""
    import homelabsage.autoconfig as mod

    class _Boom:
        @staticmethod
        def from_env():
            raise OSError("no socket")

    monkeypatch.setitem(__import__("sys").modules, "docker", _Boom)
    monkeypatch.setattr(mod, "detect_parity_capable", lambda: None)
    assert build_proposals(Config()) == []


# ─── apply ────────────────────────────────────────────────────────


def _cfg_file(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text("llm:\n  provider: disabled\n")
    return p


def test_apply_writes_selected_to_overlay(tmp_path: Path):
    cfg_path = _cfg_file(tmp_path)
    proposals = [
        Proposal("disk_pressure.paths", ["/var/lib/docker"], [], "ev"),
        Proposal("parity_gate.enabled", True, False, "ev"),
    ]
    applied = apply_proposals(
        cfg_path, proposals, only={"disk_pressure.paths"},
    )
    assert applied == ["disk_pressure.paths"]
    overlay = yaml.safe_load((tmp_path / "config.user.yaml").read_text())
    assert overlay == {"disk_pressure": {"paths": ["/var/lib/docker"]}}


def test_apply_validates_before_writing(tmp_path: Path):
    """An invalid value must be rejected by Pydantic with nothing written."""
    from pydantic import ValidationError

    cfg_path = _cfg_file(tmp_path)
    bad = [Proposal("scheduler.timezone", "Not/AZone", "UTC", "ev")]
    with pytest.raises(ValidationError):
        apply_proposals(cfg_path, bad)
    assert not (tmp_path / "config.user.yaml").exists()


def test_apply_empty_selection_is_noop(tmp_path: Path):
    cfg_path = _cfg_file(tmp_path)
    assert apply_proposals(cfg_path, [], only=None) == []
    assert not (tmp_path / "config.user.yaml").exists()


# ─── web routes ───────────────────────────────────────────────────


def _client(tmp_path: Path, monkeypatch, proposals: list[Proposal]):
    from homelabsage.web import create_app

    cfg_path = _cfg_file(tmp_path)
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    import homelabsage.web.routes_autoconfig as routes

    monkeypatch.setattr(routes, "build_proposals", lambda c: proposals)
    return TestClient(create_app(cfg, cfg_path=cfg_path)), tmp_path


def test_api_autoconfig_returns_proposals(tmp_path, monkeypatch):
    props = [Proposal("parity_gate.enabled", True, False, "Unraid detected")]
    client, _ = _client(tmp_path, monkeypatch, props)
    r = client.get("/api/autoconfig")
    assert r.status_code == 200
    body = r.json()
    assert body["proposals"][0]["dotted"] == "parity_gate.enabled"
    assert body["proposals"][0]["evidence"] == "Unraid detected"


def test_autoconfig_page_renders_evidence(tmp_path, monkeypatch):
    props = [Proposal("disk_pressure.paths", ["/var/lib/docker"], [], "docker info says so")]
    client, _ = _client(tmp_path, monkeypatch, props)
    r = client.get("/autoconfig")
    assert r.status_code == 200
    assert "disk_pressure.paths" in r.text
    assert "docker info says so" in r.text
    assert 'name="keys"' in r.text   # checkbox per proposal


def test_autoconfig_apply_via_form_writes_overlay(tmp_path, monkeypatch):
    props = [Proposal("parity_gate.enabled", True, False, "ev")]
    client, base = _client(tmp_path, monkeypatch, props)
    r = client.post("/autoconfig/apply", data={"keys": "parity_gate.enabled"})
    assert r.status_code == 200
    assert "Applied 1 proposal" in r.text
    overlay = yaml.safe_load((base / "config.user.yaml").read_text())
    assert overlay == {"parity_gate": {"enabled": True}}


def test_autoconfig_apply_nothing_selected(tmp_path, monkeypatch):
    props = [Proposal("parity_gate.enabled", True, False, "ev")]
    client, base = _client(tmp_path, monkeypatch, props)
    r = client.post("/autoconfig/apply", data={})
    assert r.status_code == 200
    assert "Nothing selected" in r.text
    assert not (base / "config.user.yaml").exists()
