"""Tests for the `config.user.yaml` overlay layer.

Pure functions; no FastAPI, no Pydantic — just dicts and paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from homelabsage.config_overlay import (
    atomic_write_yaml,
    deep_merge,
    load_overlay,
    overlay_keys,
    remove_dotted,
    set_dotted,
    user_overlay_path,
)

# ─── path convention ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "base, expected_suffix",
    [
        ("config.yaml", "config.user.yaml"),
        ("homelabsage.yaml", "homelabsage.user.yaml"),
        ("foo.yml", "foo.user.yml"),
        ("foo", "foo.user"),  # no suffix → append .user
    ],
)
def test_user_overlay_path_naming(base, expected_suffix, tmp_path):
    base_path = tmp_path / base
    out = user_overlay_path(base_path)
    assert out.name == expected_suffix
    assert out.parent == base_path.parent  # always sibling


# ─── deep_merge ────────────────────────────────────────────────────────────

def test_deep_merge_scalars_overlay_wins():
    base = {"a": 1, "b": "x"}
    overlay = {"b": "y", "c": 3}
    assert deep_merge(base, overlay) == {"a": 1, "b": "y", "c": 3}


def test_deep_merge_recurses_into_dicts():
    base = {"llm": {"model": "Q35", "endpoint": "x"}, "web": {"port": 8000}}
    overlay = {"llm": {"model": "Q4"}, "web": {"host": "1.2.3.4"}}
    out = deep_merge(base, overlay)
    assert out == {
        "llm": {"model": "Q4", "endpoint": "x"},
        "web": {"port": 8000, "host": "1.2.3.4"},
    }


def test_deep_merge_lists_replace_rather_than_concat():
    """Lists in HomelabSage's config (skip regex, mounts) are
    "complete sets" — appending would create surprising semantics."""
    base = {"skip": ["a", "b"]}
    overlay = {"skip": ["c"]}
    assert deep_merge(base, overlay) == {"skip": ["c"]}


def test_deep_merge_does_not_mutate_base():
    base = {"a": {"b": 1}}
    overlay = {"a": {"b": 99}}
    deep_merge(base, overlay)
    assert base == {"a": {"b": 1}}  # untouched


def test_deep_merge_overlay_dict_replaces_non_dict():
    """If base has a scalar where overlay has a dict, overlay wins outright."""
    base = {"a": 1}
    overlay = {"a": {"b": 2}}
    assert deep_merge(base, overlay) == {"a": {"b": 2}}


# ─── overlay_keys ──────────────────────────────────────────────────────────

def test_overlay_keys_flat():
    assert overlay_keys({"a": 1, "b": 2}) == ["a", "b"]


def test_overlay_keys_nested():
    assert overlay_keys(
        {"llm": {"model": "x", "endpoint": "y"}, "scheduler": {"enabled": False}}
    ) == ["llm.endpoint", "llm.model", "scheduler.enabled"]


def test_overlay_keys_empty_dict_is_listed_as_leaf():
    """An empty dict on the overlay is unusual but possible; treat it as a
    leaf override so the UI can show "block is overridden but with no values"."""
    assert overlay_keys({"llm": {}}) == ["llm"]


# ─── load_overlay ──────────────────────────────────────────────────────────

def test_load_overlay_missing_returns_empty(tmp_path):
    assert load_overlay(tmp_path / "missing.yaml") == {}


def test_load_overlay_reads_yaml(tmp_path):
    p = tmp_path / "ov.yaml"
    p.write_text("llm:\n  model: Q4\n")
    assert load_overlay(p) == {"llm": {"model": "Q4"}}


def test_load_overlay_malformed_returns_empty(tmp_path):
    """Better to ignore broken YAML and ship a warning than to crash the app."""
    p = tmp_path / "ov.yaml"
    p.write_text("not: a: valid: yaml: ::")
    assert load_overlay(p) == {}


def test_load_overlay_non_dict_top_level_returns_empty(tmp_path):
    p = tmp_path / "ov.yaml"
    p.write_text("- 1\n- 2\n")
    assert load_overlay(p) == {}


# ─── atomic_write_yaml ─────────────────────────────────────────────────────

def test_atomic_write_creates_file(tmp_path):
    target = tmp_path / "out.yaml"
    atomic_write_yaml(target, {"llm": {"model": "Q4"}})
    assert target.exists()
    assert yaml.safe_load(target.read_text()) == {"llm": {"model": "Q4"}}


def test_atomic_write_sets_restrictive_perms(tmp_path):
    target = tmp_path / "out.yaml"
    atomic_write_yaml(target, {"x": 1})
    mode = target.stat().st_mode & 0o777
    assert mode == 0o600


def test_atomic_write_overwrites_existing(tmp_path):
    target = tmp_path / "out.yaml"
    target.write_text("old: 1\n")
    atomic_write_yaml(target, {"new": 2})
    assert yaml.safe_load(target.read_text()) == {"new": 2}


def test_atomic_write_creates_parent_dir(tmp_path):
    target = tmp_path / "deep" / "nest" / "out.yaml"
    atomic_write_yaml(target, {"x": 1})
    assert target.exists()


def test_atomic_write_does_not_leave_tmp_files(tmp_path):
    target = tmp_path / "out.yaml"
    atomic_write_yaml(target, {"x": 1})
    stragglers = list(tmp_path.glob(".out.yaml.*"))
    assert stragglers == []


def test_atomic_write_falls_back_on_ebusy(tmp_path, monkeypatch):
    """When `path` is bind-mounted as a single file (common with
    `docker run -v host.yaml:/app/...`), `os.replace` raises EBUSY.
    We must fall back to a non-atomic in-place write rather than fail."""
    target = tmp_path / "out.yaml"
    target.write_text("prev: value\n")

    real_replace = __import__("os").replace

    def replace_ebusy(src, dst, *args, **kwargs):
        import errno
        if str(dst) == str(target):
            raise OSError(errno.EBUSY, "Device or resource busy", str(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr("homelabsage.config_overlay.os.replace", replace_ebusy)

    atomic_write_yaml(target, {"new": 2})

    assert yaml.safe_load(target.read_text()) == {"new": 2}
    # And no .tmp straggler left behind even on the fallback path
    stragglers = list(tmp_path.glob(".out.yaml.*"))
    assert stragglers == []


# ─── dotted-path helpers ───────────────────────────────────────────────────

def test_set_dotted_creates_path():
    out = set_dotted({}, "llm.model", "Q4")
    assert out == {"llm": {"model": "Q4"}}


def test_set_dotted_preserves_siblings():
    base = {"llm": {"endpoint": "x"}}
    out = set_dotted(base, "llm.model", "Q4")
    assert out == {"llm": {"endpoint": "x", "model": "Q4"}}
    # base unchanged
    assert base == {"llm": {"endpoint": "x"}}


def test_set_dotted_replaces_non_dict_intermediate():
    """If an intermediate is a scalar, overlay-style set replaces it with a dict."""
    base = {"llm": "not a dict"}
    out = set_dotted(base, "llm.model", "Q4")
    assert out == {"llm": {"model": "Q4"}}


def test_remove_dotted_prunes_empty_parents():
    base = {"llm": {"model": "Q4"}, "scheduler": {"enabled": True}}
    out = remove_dotted(base, "llm.model")
    # The empty {"llm": {}} is pruned — clean reverts leave no orphans.
    assert out == {"scheduler": {"enabled": True}}


def test_remove_dotted_preserves_siblings():
    base = {"llm": {"model": "Q4", "endpoint": "x"}}
    out = remove_dotted(base, "llm.model")
    assert out == {"llm": {"endpoint": "x"}}


def test_remove_dotted_missing_key_is_noop():
    base = {"llm": {"model": "Q4"}}
    out = remove_dotted(base, "scheduler.enabled")
    assert out == base


def test_remove_dotted_does_not_mutate():
    base = {"a": {"b": 1}}
    remove_dotted(base, "a.b")
    assert base == {"a": {"b": 1}}


# ─── integration: load_config picks up the overlay ────────────────────────

def test_load_config_applies_overlay(tmp_path: Path):
    """The full load_config pipeline reads `config.yaml` + `config.user.yaml`
    in the same directory and merges them before Pydantic validation."""
    from homelabsage.config import load_config

    base = tmp_path / "config.yaml"
    base.write_text(
        "llm:\n"
        "  provider: openai\n"
        "  model: Qwen3.6-35B-Abl\n"
        "  endpoint: http://halo:11434\n"
    )
    overlay = tmp_path / "config.user.yaml"
    overlay.write_text("llm:\n  model: Qwen3.5-4B-Compact\n")

    cfg = load_config(base)
    assert cfg.llm.model == "Qwen3.5-4B-Compact"        # overridden
    assert cfg.llm.endpoint == "http://halo:11434"      # untouched
    assert cfg.llm.provider == "openai"                 # untouched


def test_load_config_works_without_overlay(tmp_path: Path):
    """Missing `config.user.yaml` is the default state — must not error."""
    from homelabsage.config import load_config

    base = tmp_path / "config.yaml"
    base.write_text("llm:\n  model: Q4\n")
    cfg = load_config(base)
    assert cfg.llm.model == "Q4"


def test_load_config_ignores_malformed_overlay(tmp_path: Path):
    """A broken overlay shouldn't take the app down — log + ignore."""
    from homelabsage.config import load_config

    base = tmp_path / "config.yaml"
    base.write_text("llm:\n  model: Q4\n")
    (tmp_path / "config.user.yaml").write_text(":::not yaml")
    cfg = load_config(base)
    assert cfg.llm.model == "Q4"
