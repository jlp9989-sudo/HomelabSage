"""Shared test fixtures + helpers.

Each test module previously hand-wrote its own `cfg_dir` fixture inlining
the baseline YAML. The non-trivial ones (settings_html, wizard,
llm_profiles, routes_settings_test) carry test-specific shape and stay in
their own file. The thin ones (settings, settings_v030_blocks) consolidate
to `make_baseline_config_yaml()` here so adding a future test against the
same shape doesn't grow another 15-line literal.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from homelabsage import web
from homelabsage.config import load_config


def make_baseline_config_yaml(
    tmp_path: Path,
    *,
    extra: str = "",
) -> Path:
    """Write a minimal `config.yaml` into `tmp_path` and return the dir.

    Defaults: scheduler disabled, web auth off, port 0 (TestClient never
    binds), SQLite path under `tmp_path`. Pass `extra` to append additional
    YAML at the bottom (e.g. extra sources/outputs the test needs).
    """
    body = (
        "llm:\n"
        "  provider: openai\n"
        "  model: stub\n"
        "  endpoint: http://stub\n"
        "scheduler:\n"
        "  enabled: false\n"
        "storage:\n"
        f"  database_path: {tmp_path}/state.sqlite\n"
        "web:\n"
        "  enabled: true\n"
        "  host: 127.0.0.1\n"
        "  port: 0\n"
        "  auth:\n"
        "    enabled: false\n"
    )
    if extra:
        body += extra if extra.endswith("\n") else extra + "\n"
    (tmp_path / "config.yaml").write_text(body)
    return tmp_path


def make_test_client(cfg_dir: Path) -> TestClient:
    """Wire up `create_app` against `cfg_dir/config.yaml` and return a client."""
    cfg_path = cfg_dir / "config.yaml"
    cfg = load_config(cfg_path)
    app = web.create_app(cfg, cfg_path=cfg_path)
    return TestClient(app)
