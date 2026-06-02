"""Tests for the diagnostics probe and `/diagnostics` route."""

from __future__ import annotations

import pytest

from homelabsage.config import DockerSourceConfig
from homelabsage.diagnostics import (
    ContainerDiagnostic,
    diagnose_containers,
    summarise,
)

# ─── helpers ─────────────────────────────────────────────────────────────


class _Image:
    def __init__(self, tags, labels=None):
        self.tags = tags
        self.labels = labels or {}
        self.attrs = {}


class _Container:
    def __init__(self, name, *, image_tag="", labels=None, image_label_version=None,
                 status="running"):
        self.name = name
        self.image = _Image([image_tag] if image_tag else [],
                            labels={"org.opencontainers.image.version": image_label_version}
                            if image_label_version else {})
        self.attrs = {
            "State": {"Status": status, "FinishedAt": ""},
            "Config": {"Image": image_tag, "Labels": labels or {}},
            "NetworkSettings": {"Ports": {}},
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        }


class _Containers:
    def __init__(self, items):
        self._items = items
    def list(self, all=False):  # noqa: A002 - mirror docker SDK signature
        return self._items


class _Client:
    def __init__(self, items):
        self.containers = _Containers(items)


@pytest.fixture
def fake_docker(monkeypatch):
    """Replace DockerPlugin._client with a returns-`_Client` lambda."""
    items: list[_Container] = []

    def _set(its):
        items.clear()
        items.extend(its)

    monkeypatch.setattr(
        "homelabsage.plugins.docker.DockerPlugin._client",
        lambda self: _Client(items),
    )
    return _set


# ─── unit tests ─────────────────────────────────────────────────────────


def test_diagnose_returns_empty_when_daemon_down(monkeypatch):
    def _raise(self):
        raise RuntimeError("boom")
    monkeypatch.setattr(
        "homelabsage.plugins.docker.DockerPlugin._client", _raise
    )
    assert diagnose_containers(DockerSourceConfig(enabled=True)) == []


def test_diagnose_marks_tracked_for_ghcr_semver(fake_docker):
    fake_docker([
        _Container(
            "openclaw",
            image_tag="ghcr.io/openclaw/openclaw:5.7.0",
            labels={"org.opencontainers.image.source": "https://github.com/openclaw/openclaw"},
        ),
    ])
    out = diagnose_containers(DockerSourceConfig(enabled=True))
    assert len(out) == 1
    d = out[0]
    assert d.verdict == "tracked"
    assert d.repo == "openclaw/openclaw"
    assert d.current_version == "5.7.0"


def test_diagnose_marks_no_repo_when_unresolved(fake_docker):
    fake_docker([
        _Container("randomthing", image_tag="randomthing:1.0.0"),
    ])
    out = diagnose_containers(
        DockerSourceConfig(enabled=True, track_floating_tags=False)
    )
    assert out[0].verdict == "no_repo"
    assert any("could not resolve" in n for n in out[0].notes)


def test_diagnose_marks_floating_tag_for_docker_hub_latest(fake_docker):
    fake_docker([
        # Tag is `latest` → no semver. Image string parses as Docker Hub
        # (no registry prefix, no slash) → floating_tag verdict.
        _Container("nginx", image_tag="nginx:latest"),
    ])
    out = diagnose_containers(DockerSourceConfig(enabled=True))
    assert out[0].verdict == "floating_tag"
    assert "digest" in " ".join(out[0].notes).lower()


def test_diagnose_skipped_by_rule_records_pattern(fake_docker):
    fake_docker([
        _Container("redis_db", image_tag="redis:7"),
        _Container("api", image_tag="ghcr.io/x/y:1.0.0"),
    ])
    cfg = DockerSourceConfig(enabled=True, skip=[r"_(db|redis)$"])
    out = {d.name: d for d in diagnose_containers(cfg)}
    assert out["redis_db"].verdict == "skipped_by_rule"
    assert out["redis_db"].skip_pattern == r"_(db|redis)$"
    # The non-matching one is unaffected
    assert out["api"].verdict != "skipped_by_rule"


def test_diagnose_marks_no_version_when_repo_present_but_tag_unparseable(fake_docker):
    fake_docker([
        _Container(
            "weirdthing",
            image_tag="ghcr.io/owner/weirdthing:cuda12",
            labels={"org.opencontainers.image.source": "https://github.com/owner/weirdthing"},
        ),
    ])
    out = diagnose_containers(DockerSourceConfig(enabled=True))
    assert out[0].verdict == "no_version"
    assert out[0].repo == "owner/weirdthing"


def test_diagnose_records_override_note_when_used(fake_docker):
    fake_docker([
        # No source label, no GHCR pattern → would normally fail. Override
        # supplies the repo, so the verdict should still be tracked.
        _Container("custom_app", image_tag="custom_app:2.0.0"),
    ])
    cfg = DockerSourceConfig(
        enabled=True,
        overrides={"custom_app": "owner/custom_app"},
    )
    out = diagnose_containers(cfg)
    assert out[0].repo == "owner/custom_app"
    assert any("override" in n.lower() for n in out[0].notes)


def test_summarise_counts_per_verdict():
    diagnostics = [
        ContainerDiagnostic(name="a", status="running", image="x:1", image_short="x", verdict="tracked"),
        ContainerDiagnostic(name="b", status="running", image="y:1", image_short="y", verdict="tracked"),
        ContainerDiagnostic(name="c", status="exited", image="z:1", image_short="z", verdict="no_repo"),
    ]
    assert summarise(diagnostics) == {"tracked": 2, "no_repo": 1}


# ─── integration: web route ─────────────────────────────────────────────


def test_diagnostics_page_renders(monkeypatch, tmp_path):
    """End-to-end: hit GET /diagnostics and validate the response shape."""
    from fastapi.testclient import TestClient

    from homelabsage.config import Config
    from homelabsage.web import create_app

    cfg = Config()
    cfg.web.auth.enabled = False
    cfg.scheduler.enabled = False
    cfg.storage.database_path = str(tmp_path / "s.sqlite")

    # Replace diagnose_containers so the route doesn't try to talk to docker.
    monkeypatch.setattr(
        "homelabsage.web.routes_diagnostics.diagnose_containers",
        lambda cfg_arg: [
            ContainerDiagnostic(
                name="openclaw", status="running",
                image="ghcr.io/openclaw/openclaw:5.7.0",
                image_short="openclaw",
                verdict="tracked",
                repo="openclaw/openclaw",
                current_version="5.7.0",
            )
        ],
    )

    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/diagnostics")
        assert r.status_code == 200
        assert "What HomelabSage sees" in r.text
        assert "openclaw" in r.text
        assert "tracked" in r.text

        rj = c.get("/api/diagnostics")
        assert rj.status_code == 200
        data = rj.json()
        assert data["counts"] == {"tracked": 1}
        assert data["rows"][0]["repo"] == "openclaw/openclaw"
