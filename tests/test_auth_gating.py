"""Auth-gating of the infra-revealing endpoints (v0.13.3).

`/api/doctor` and `/api/stack-health` expose filesystem paths, internal
hostnames and backup-repo URLs, and run live probes. They must require
auth when auth is enabled; only liveness + count surfaces stay open.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from homelabsage.config import Config
from homelabsage.web import create_app


def _authed_client(tmp_path: Path) -> TestClient:
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")
    cfg.web.auth.enabled = True
    cfg.web.auth.username = "javi"
    cfg.web.auth.password = "secret"
    return TestClient(create_app(cfg))


def test_doctor_and_stack_health_require_auth(tmp_path):
    client = _authed_client(tmp_path)
    assert client.get("/api/doctor").status_code == 401
    assert client.get("/api/stack-health").status_code == 401


def test_liveness_surfaces_stay_open_under_auth(tmp_path):
    client = _authed_client(tmp_path)
    # These carry no infra detail and stay bypassed for probes/scrapers.
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/version").status_code == 200
    assert client.get("/metrics").status_code == 200


def test_doctor_reachable_with_credentials(tmp_path):
    client = _authed_client(tmp_path)
    r = client.get("/api/doctor?skip_llm=1", auth=("javi", "secret"))
    assert r.status_code == 200


def test_endpoints_open_when_auth_disabled(tmp_path):
    cfg = Config()
    cfg.storage.database_path = str(tmp_path / "t.db")  # auth off by default
    client = TestClient(create_app(cfg))
    assert client.get("/api/stack-health").status_code == 200
