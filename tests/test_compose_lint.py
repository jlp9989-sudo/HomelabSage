"""Tests for the compose-file linter."""

from __future__ import annotations

from pathlib import Path

from homelabsage.compose_lint import (
    _DEFAULT_HEALTHCHECK_EXEMPT,
    _image_uses_latest,
    lint_compose_file,
    lint_paths,
)


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# ─── helpers ────────────────────────────────────────────────────────


def test_image_latest_detector():
    assert _image_uses_latest("foo:latest")
    assert _image_uses_latest("foo")                 # implicit latest
    assert _image_uses_latest("library/nginx")       # implicit latest
    assert not _image_uses_latest("foo:1.2.3")
    assert not _image_uses_latest("foo@sha256:abc")


# ─── per-rule ───────────────────────────────────────────────────────


def test_links_deprecated(tmp_path: Path):
    body = """\
services:
  web:
    image: foo:1.0
    links:
      - db
"""
    findings = lint_compose_file(_write(tmp_path, "docker-compose.yml", body))
    rules = {f.rule for f in findings}
    assert "links_deprecated" in rules


def test_latest_tag(tmp_path: Path):
    body = """\
services:
  web:
    image: foo:latest
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "true"]
"""
    findings = lint_compose_file(_write(tmp_path, "compose.yml", body))
    rules = {f.rule for f in findings}
    assert "latest_tag" in rules


def test_no_healthcheck(tmp_path: Path):
    body = """\
services:
  web:
    image: foo:1.0
    restart: always
"""
    findings = lint_compose_file(_write(tmp_path, "compose.yml", body))
    rules = {f.rule for f in findings}
    assert "no_healthcheck" in rules


def test_healthcheck_exempt_service_skipped(tmp_path: Path):
    body = """\
services:
  init:
    image: foo:1.0
    restart: always
"""
    findings = lint_compose_file(_write(tmp_path, "compose.yml", body))
    rules = {f.rule for f in findings}
    assert "no_healthcheck" not in rules


def test_no_restart(tmp_path: Path):
    body = """\
services:
  web:
    image: foo:1.0
    healthcheck:
      test: ["CMD", "true"]
"""
    findings = lint_compose_file(_write(tmp_path, "compose.yml", body))
    rules = {f.rule for f in findings}
    assert "no_restart" in rules


def test_privileged_no_reason(tmp_path: Path):
    body = """\
services:
  web:
    image: foo:1.0
    restart: always
    privileged: true
    healthcheck:
      test: ["CMD", "true"]
"""
    findings = lint_compose_file(_write(tmp_path, "compose.yml", body))
    rules = {f.rule for f in findings}
    assert "privileged_no_reason" in rules


def test_privileged_with_cap_add_silenced(tmp_path: Path):
    body = """\
services:
  web:
    image: foo:1.0
    restart: always
    privileged: true
    cap_add:
      - NET_ADMIN
    healthcheck:
      test: ["CMD", "true"]
"""
    findings = lint_compose_file(_write(tmp_path, "compose.yml", body))
    rules = {f.rule for f in findings}
    assert "privileged_no_reason" not in rules


def test_host_network(tmp_path: Path):
    body = """\
services:
  plex:
    image: foo:1.0
    restart: always
    network_mode: host
    healthcheck:
      test: ["CMD", "true"]
"""
    findings = lint_compose_file(_write(tmp_path, "compose.yml", body))
    rules = {f.rule for f in findings}
    assert "host_network" in rules


def test_bind_no_user_flagged(tmp_path: Path):
    # v0.10.4 (I7): the bind_no_user rule is now scoped to actual LSIO
    # images. Use lscr.io/linuxserver/... so it fires.
    body = """\
services:
  app:
    image: lscr.io/linuxserver/sonarr:latest
    restart: always
    healthcheck:
      test: ["CMD", "true"]
    volumes:
      - /mnt/user/appdata/foo:/config
"""
    findings = lint_compose_file(_write(tmp_path, "compose.yml", body))
    rules = {f.rule for f in findings}
    assert "bind_no_user" in rules


def test_bind_with_user_silenced(tmp_path: Path):
    body = """\
services:
  app:
    image: foo:1.0
    restart: always
    user: "1000:1000"
    healthcheck:
      test: ["CMD", "true"]
    volumes:
      - /mnt/user/appdata/foo:/config
"""
    findings = lint_compose_file(_write(tmp_path, "compose.yml", body))
    rules = {f.rule for f in findings}
    assert "bind_no_user" not in rules


# ─── path orchestration ─────────────────────────────────────────────


def test_lint_paths_walks_subdirs(tmp_path: Path):
    sub1 = tmp_path / "stack1"
    sub1.mkdir()
    (sub1 / "compose.yml").write_text(
        "services:\n  a:\n    image: foo:latest\n", encoding="utf-8",
    )
    sub2 = tmp_path / "stack2"
    sub2.mkdir()
    (sub2 / "docker-compose.yml").write_text(
        "services:\n  b:\n    image: foo:1.0\n    links: [c]\n", encoding="utf-8",
    )
    report = lint_paths([tmp_path])
    rules = {f.rule for f in report.findings}
    assert "latest_tag" in rules
    assert "links_deprecated" in rules


def test_lint_paths_handles_malformed_yaml(tmp_path: Path):
    _write(tmp_path, "compose.yml", "::: not valid yaml :::")
    report = lint_paths([tmp_path])
    assert report.findings == []


def test_lint_compose_file_no_services_key(tmp_path: Path):
    p = _write(tmp_path, "compose.yml", "version: '3'\n")
    assert lint_compose_file(p) == []


def test_report_aggregates():
    from homelabsage.compose_lint import ComposeLintFinding, ComposeLintReport
    rep = ComposeLintReport(findings=[
        ComposeLintFinding("medium", "links_deprecated",
                           Path("/tmp/x"), "a", "x"),
        ComposeLintFinding("medium", "latest_tag",
                           Path("/tmp/x"), "b", "y"),
        ComposeLintFinding("info", "no_healthcheck",
                           Path("/tmp/x"), "c", "z"),
    ])
    assert rep.by_severity() == {"medium": 2, "info": 1}
    assert rep.by_rule() == {
        "links_deprecated": 1, "latest_tag": 1, "no_healthcheck": 1,
    }


def test_default_exempt_is_frozenset():
    assert isinstance(_DEFAULT_HEALTHCHECK_EXEMPT, frozenset)
    assert "tailscale" in _DEFAULT_HEALTHCHECK_EXEMPT
