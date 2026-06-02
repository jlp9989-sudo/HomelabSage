"""Tests for the Watchtower migration helper."""

from __future__ import annotations

from homelabsage.watchtower_migrate import detect


class _Image:
    def __init__(self, tags):
        self.tags = tags


class _Container:
    def __init__(self, name, *, image_tag, env=None, cmd=None, labels=None):
        self.name = name
        self.image = _Image([image_tag] if image_tag else [])
        self.attrs = {
            "Config": {
                "Env": [f"{k}={v}" for k, v in (env or {}).items()],
                "Cmd": cmd or [],
                "Labels": labels or {},
            }
        }


def test_detect_returns_none_when_no_watchtower():
    out = detect([
        _Container("plex", image_tag="lscr.io/linuxserver/plex:1.41"),
        _Container("nginx", image_tag="nginx:latest"),
    ])
    assert out is None


def test_detect_finds_containrrr_watchtower():
    containers = [
        _Container("watchtower", image_tag="containrrr/watchtower:latest",
                   env={"WATCHTOWER_SCHEDULE": "0 0 4 * * *",
                        "WATCHTOWER_CLEANUP": "true"}),
        _Container("plex", image_tag="lscr.io/linuxserver/plex:1.41"),
        _Container("nginx", image_tag="nginx:1.27"),
    ]
    report = detect(containers)
    assert report is not None
    assert report.watchtower_container == "watchtower"
    assert "containrrr/watchtower" in report.watchtower_image
    assert report.env["WATCHTOWER_SCHEDULE"] == "0 0 4 * * *"
    assert report.env["WATCHTOWER_CLEANUP"] == "true"
    # Two managed containers (excludes watchtower itself)
    managed_names = [v.name for v in report.managed_containers]
    assert sorted(managed_names) == ["nginx", "plex"]


def test_detect_finds_nicholas_fedor_fork():
    """The active fork keeps the same com.centurylinklabs.* label namespace."""
    containers = [
        _Container("wt", image_tag="nicholas-fedor/watchtower:latest"),
    ]
    report = detect(containers)
    assert report is not None


def test_detect_label_enable_mode_excludes_opt_outs():
    """When Watchtower runs `--label-enable`, only containers with
    `watchtower.enable=true` get included; everything else is exclude."""
    containers = [
        _Container(
            "watchtower", image_tag="containrrr/watchtower",
            env={"WATCHTOWER_LABEL_ENABLE": "true"},
        ),
        _Container(
            "auto_upd", image_tag="x:1",
            labels={"com.centurylinklabs.watchtower.enable": "true"},
        ),
        _Container(
            "no_label", image_tag="y:1",
        ),
    ]
    report = detect(containers)
    by_name = {v.name: v for v in report.managed_containers}
    assert "include" in by_name["auto_upd"].recommendation.lower()
    assert "leave out" in by_name["no_label"].recommendation.lower()


def test_detect_explicit_enable_false_is_excluded():
    containers = [
        _Container("watchtower", image_tag="containrrr/watchtower"),
        _Container(
            "skipme", image_tag="x:1",
            labels={"com.centurylinklabs.watchtower.enable": "false"},
        ),
    ]
    report = detect(containers)
    skipme = next(v for v in report.managed_containers if v.name == "skipme")
    assert "leave out" in skipme.recommendation.lower()


def test_detect_monitor_only_label_gives_analyse_only_recommendation():
    containers = [
        _Container("watchtower", image_tag="containrrr/watchtower"),
        _Container(
            "obs", image_tag="x:1",
            labels={"com.centurylinklabs.watchtower.monitor-only": "true"},
        ),
    ]
    report = detect(containers)
    obs = next(v for v in report.managed_containers if v.name == "obs")
    assert "analyse only" in obs.recommendation.lower()


def test_report_markdown_includes_per_container_plan():
    containers = [
        _Container("watchtower", image_tag="containrrr/watchtower",
                   env={"WATCHTOWER_SCHEDULE": "@daily"}),
        _Container("plex", image_tag="lscr.io/linuxserver/plex:1.41"),
    ]
    md = detect(containers).to_markdown()
    assert "# Watchtower migration plan" in md
    assert "WATCHTOWER_SCHEDULE" in md
    assert "plex" in md
    assert "Per-container plan" in md


def test_report_markdown_redacts_secret_envs():
    containers = [
        _Container("watchtower", image_tag="containrrr/watchtower",
                   env={"WATCHTOWER_NOTIFICATION_URL": "https://hooks.slack.com/T/B/SECRET"}),
    ]
    md = detect(containers).to_markdown()
    assert "SECRET" not in md
    assert "<redacted>" in md
