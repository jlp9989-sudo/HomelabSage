"""CSRF / Origin-Referer guard tests.

Each scenario exercises a real `create_app(cfg)` so middleware ordering
matches production. The guard runs BEFORE Basic Auth in the stack (FastAPI
reverse-order semantics) so we can test it independently of auth state.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.config import load_config


def _make_app(tmp_path: Path):
    cfg_yaml = tmp_path / "config.yaml"
    cfg_yaml.write_text(
        "llm: {provider: disabled}\n"
        f"storage: {{database_path: {tmp_path}/state.sqlite}}\n"
        f"notes: {{notes_dir: {tmp_path}/notes}}\n"
        "scheduler: {enabled: false}\n"
        "web: {auth: {enabled: false}}\n"
    )
    return TestClient(web.create_app(load_config(cfg_yaml), cfg_path=cfg_yaml))


# ─── safe methods always pass ─────────────────────────────────────────────

def test_get_passes_without_origin(tmp_path: Path):
    c = _make_app(tmp_path)
    r = c.get("/settings")
    assert r.status_code == 200


def test_get_passes_with_arbitrary_origin(tmp_path: Path):
    """GETs are nullipotent. Attacker-controlled Origin on a GET is harmless."""
    c = _make_app(tmp_path)
    r = c.get("/settings", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200


def test_healthz_passes_even_on_post(tmp_path: Path):
    """Health endpoint must remain reachable for unauthenticated probes
    regardless of HTTP method or headers."""
    c = _make_app(tmp_path)
    r = c.post("/healthz", headers={"Origin": "https://evil.example"})
    # /healthz is GET-only so we'd expect 405, but the CSRF guard must not
    # be the thing that rejects: 405 is fine, 403 is the bug we're testing.
    assert r.status_code != 403


# ─── state-changing methods: Origin enforcement ───────────────────────────

def test_post_with_matching_origin_passes(tmp_path: Path):
    """When the browser sends Origin and it matches Host, allow."""
    c = _make_app(tmp_path)
    r = c.post(
        "/settings/llm/update",
        data={"provider": "disabled"},
        headers={"Host": "testserver", "Origin": "http://testserver"},
    )
    # 200 or 400 are both OK — the point is "not 403".
    assert r.status_code != 403


def test_post_with_evil_origin_is_blocked(tmp_path: Path):
    """The textbook CSRF attempt: browser sends Origin from an attacker site."""
    c = _make_app(tmp_path)
    r = c.post(
        "/settings/llm/update",
        data={"provider": "openai"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert "cross-origin" in r.text.lower()


def test_post_with_evil_referer_is_blocked(tmp_path: Path):
    """Older clients strip Origin but keep Referer; cover that path too."""
    c = _make_app(tmp_path)
    r = c.post(
        "/settings/llm/update",
        data={"provider": "openai"},
        headers={"Referer": "https://evil.example/page"},
    )
    assert r.status_code == 403


def test_post_with_matching_referer_passes(tmp_path: Path):
    c = _make_app(tmp_path)
    r = c.post(
        "/settings/llm/update",
        data={"provider": "disabled"},
        headers={"Host": "testserver", "Referer": "http://testserver/settings/llm"},
    )
    assert r.status_code != 403


def test_post_without_origin_or_referer_passes(tmp_path: Path):
    """curl / API clients don't send these headers. Basic Auth gates entry."""
    c = _make_app(tmp_path)
    r = c.post("/settings/llm/update", data={"provider": "disabled"})
    assert r.status_code != 403


def test_origin_check_honours_x_forwarded_proto(tmp_path: Path):
    """Reverse proxies (Caddy / Traefik / nginx) terminate TLS and forward
    the inner request as http. The user's Origin will be `https://host`.
    We must trust X-Forwarded-Proto so the comparison matches the public URL."""
    c = _make_app(tmp_path)
    r = c.post(
        "/settings/llm/update",
        data={"provider": "disabled"},
        headers={
            "Host": "homelabsage.example.com",
            "X-Forwarded-Proto": "https",
            "Origin": "https://homelabsage.example.com",
        },
    )
    assert r.status_code != 403


def test_post_origin_must_include_scheme(tmp_path: Path):
    """An Origin without scheme/host is malformed — defensive normalisation
    treats it as missing rather than bypassing the check."""
    c = _make_app(tmp_path)
    r = c.post(
        "/settings/llm/update",
        data={"provider": "disabled"},
        headers={"Origin": "garbage"},
    )
    # Malformed Origin → falls through to "no Origin/Referer" branch → allowed
    assert r.status_code != 403


# ─── PATCH / DELETE / PUT all caught ──────────────────────────────────────

@pytest.mark.parametrize("method", ["PATCH", "DELETE", "PUT"])
def test_state_changing_methods_all_checked(tmp_path: Path, method: str):
    c = _make_app(tmp_path)
    headers = {"Origin": "https://evil.example"}
    if method == "PATCH":
        r = c.patch("/api/settings/llm", json={"provider": "openai"}, headers=headers)
    elif method == "DELETE":
        r = c.delete("/api/settings/llm", headers=headers)
    else:
        r = c.put("/settings/llm", headers=headers)
    assert r.status_code == 403


# ─── auth + csrf compose correctly ────────────────────────────────────────

def test_csrf_runs_before_auth_so_403_takes_priority(tmp_path: Path):
    """A request that's both cross-origin AND unauthenticated should be
    rejected as cross-origin (403), not as unauthorised (401). The guard
    runs outside auth so an attacker can't enumerate valid endpoints via
    401-vs-403 differences."""
    cfg_yaml = tmp_path / "config.yaml"
    cfg_yaml.write_text(
        "llm: {provider: disabled}\n"
        f"storage: {{database_path: {tmp_path}/state.sqlite}}\n"
        f"notes: {{notes_dir: {tmp_path}/notes}}\n"
        "scheduler: {enabled: false}\n"
        "web:\n"
        "  auth: {enabled: true, username: admin, password: 'pw'}\n"
    )
    c = TestClient(web.create_app(load_config(cfg_yaml), cfg_path=cfg_yaml))
    r = c.post(
        "/settings/llm/update",
        data={"provider": "openai"},
        headers={"Origin": "https://evil.example"},  # no Basic Auth
    )
    assert r.status_code == 403  # CSRF wins over auth
