"""Tests for the HuggingFace model URL analyser (sub-case d)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from homelabsage import analyse_url as au
from homelabsage.analyse_url import (
    _bytes_per_param,
    _read_system_vram_gib,
    analyse_huggingface_url,
    estimate_vram_gib,
    parse_huggingface_url,
)
from homelabsage.config import Config

# ─── parser ─────────────────────────────────────────────────────────


def test_parse_huggingface_url_basic():
    assert parse_huggingface_url("https://huggingface.co/Qwen/Qwen3.6-35B") == "Qwen/Qwen3.6-35B"


def test_parse_huggingface_url_strips_tail():
    assert parse_huggingface_url(
        "https://huggingface.co/Qwen/Qwen3.6-35B/tree/main"
    ) == "Qwen/Qwen3.6-35B"


def test_parse_huggingface_url_rejects_non_hf():
    assert parse_huggingface_url("https://github.com/foo/bar") is None
    assert parse_huggingface_url("https://huggingface.co/") is None


def test_parse_huggingface_url_rejects_well_known_prefixes():
    """Regression: /datasets, /spaces, /papers etc. are NOT model paths."""
    assert parse_huggingface_url("https://huggingface.co/datasets/imagenet/x") is None
    assert parse_huggingface_url("https://huggingface.co/spaces/gradio/hello") is None
    assert parse_huggingface_url("https://huggingface.co/papers/2305.12345") is None
    assert parse_huggingface_url("https://huggingface.co/blog/post") is None
    assert parse_huggingface_url("https://huggingface.co/docs/x") is None


# ─── bytes/param + estimator ─────────────────────────────────────────


def test_bytes_per_param_detects_q4():
    assert _bytes_per_param(["model.Q4_K_M.gguf"], None) == 0.5


def test_bytes_per_param_detects_mxfp4():
    assert _bytes_per_param(["model.MXFP4.gguf"], None) == 0.6


def test_bytes_per_param_explicit_reported():
    assert _bytes_per_param([], "fp16") == 2.0


def test_bytes_per_param_default_bf16():
    assert _bytes_per_param(["model.safetensors"], None) == 2.0


def test_estimate_vram_gib_smoke():
    # 35B bf16 ≈ 70 GB + 15% = ~80 GiB
    gib = estimate_vram_gib(35.0, 2.0)
    assert 70 < gib < 90


def test_estimate_vram_gib_q4():
    # 35B q4 ≈ 17.5 GB + 15% ≈ 20 GiB
    gib = estimate_vram_gib(35.0, 0.5)
    assert 18 < gib < 22


# ─── system VRAM reader ──────────────────────────────────────────────


def test_read_system_vram_gib_no_notes(tmp_path: Path):
    assert _read_system_vram_gib(None) is None
    assert _read_system_vram_gib(str(tmp_path)) is None  # no system.md


def test_read_system_vram_gib_extracts(tmp_path: Path):
    (tmp_path / "system.md").write_text(
        "# system\n- GPU: RTX 5060 Ti (16 GiB VRAM)\n", encoding="utf-8",
    )
    assert _read_system_vram_gib(str(tmp_path)) == 16.0


def test_read_system_vram_gib_pattern_miss(tmp_path: Path):
    (tmp_path / "system.md").write_text("# system\n- no gpu mentioned\n", encoding="utf-8")
    assert _read_system_vram_gib(str(tmp_path)) is None


# ─── analyser ────────────────────────────────────────────────────────


def _stub_meta(meta: dict | None):
    async def fake(slug: str):
        return meta
    return fake


def test_analyse_huggingface_url_returns_none_when_meta_404(monkeypatch):
    monkeypatch.setattr(au, "_fetch_hf_model_meta", _stub_meta(None))
    cfg = Config()
    out = asyncio.run(analyse_huggingface_url(cfg, "https://huggingface.co/x/y"))
    assert out is None


def test_analyse_huggingface_url_produces_fit_verdict_wont_fit(monkeypatch, tmp_path: Path):
    meta = {
        "safetensors": {"parameters": {"BF16": 35_000_000_000}},
        "siblings": [{"rfilename": "model.safetensors"}],
        "tags": ["text-generation"],
        "library_name": "transformers",
    }
    monkeypatch.setattr(au, "_fetch_hf_model_meta", _stub_meta(meta))
    notes_dir = tmp_path
    (notes_dir / "system.md").write_text(
        "GPU: 16 GiB VRAM\n", encoding="utf-8",
    )
    cfg = Config()
    cfg.notes.notes_dir = str(notes_dir)
    out = asyncio.run(analyse_huggingface_url(cfg, "https://huggingface.co/Qwen/Qwen35B"))
    assert out is not None
    assert out.update.context["fit_verdict"] == "wont_fit"
    assert out.update.subject == "hf:Qwen/Qwen35B"
    assert out.update.context["params_billion"] == 35.0


def test_analyse_huggingface_url_fits_with_quantised(monkeypatch, tmp_path: Path):
    meta = {
        "safetensors": {"parameters": {"BF16": 35_000_000_000}},
        "siblings": [{"rfilename": "model.Q4_K_M.gguf"}],
        "tags": ["gguf"],
        "library_name": "llama.cpp",
    }
    monkeypatch.setattr(au, "_fetch_hf_model_meta", _stub_meta(meta))
    notes_dir = tmp_path
    (notes_dir / "system.md").write_text(
        "GPU: 128 GiB VRAM\n", encoding="utf-8",
    )
    cfg = Config()
    cfg.notes.notes_dir = str(notes_dir)
    out = asyncio.run(analyse_huggingface_url(cfg, "https://huggingface.co/x/y"))
    assert out is not None
    assert out.update.context["fit_verdict"] == "fits"


def test_extract_params_billion_handles_string_marshalled_values(monkeypatch):
    """Regression: HF marshals very large ints as strings sometimes."""
    from homelabsage.analyse_url import _extract_params_billion
    meta = {"safetensors": {"parameters": {"BF16": "35000000000"}}}
    assert _extract_params_billion(meta) == 35.0


def test_dispatcher_routes_hf(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(au, "_fetch_hf_model_meta", _stub_meta({
        "safetensors": {"parameters": {"F16": 7_000_000_000}},
        "siblings": [],
    }))
    cfg = Config()
    out = asyncio.run(au.analyse_url(cfg, "https://huggingface.co/x/y"))
    assert out is not None
    assert out.update.subject.startswith("hf:")
