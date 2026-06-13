# Routing model matrix

Each candidate model routed all 14 golden scenarios through the production guided-decoding JSON-action path (`response_format: json_schema`, not native tool calls). Scored on valid JSON, correct tool, correct arguments, multi-step chaining, hallucination, and confirmation-gating. Local models run on a llama-server (LAN); cloud rows use public free tiers (latency on rate-limited tiers is not representative).

| Model | Pass | JSON | Tool | Args | Chain | Confirm | Halluc | Err | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|
| `local/Qwen3.6-35B-Think` | 92.9% | 100.0% | 92.9% | 100.0% | 100.0% | 92.9% | 0.0% | 0/14 | 2206 | 17920 |
| `local/Qwen3.6-35B-Abl` | 92.9% | 100.0% | 92.9% | 100.0% | 100.0% | 92.9% | 0.0% | 0/14 | 2319 | 3112 |
| `local/Qwen3.6-35B` | 92.9% | 100.0% | 92.9% | 100.0% | 100.0% | 100.0% | 0.0% | 0/14 | 2537 | 15816 |
| `groq/llama-3.3-70b-versatile` | 92.9% | 100.0% | 100.0% | 100.0% | 100.0% | 92.9% | 0.0% | 0/14 | 10682 | 12664 |
| `gemini/gemini-2.5-flash-lite` | 92.9% | 100.0% | 92.9% | 87.5% | 50.0% | 100.0% | 0.0% | 0/14 | 909 | 1241 |
| `gemini/gemini-2.5-flash` | 92.9% | 100.0% | 92.9% | 87.5% | 50.0% | 100.0% | 0.0% | 0/14 | 1931 | 9771 |
| `local/Gemma4-26B-Abl` | 92.9% | 100.0% | 92.9% | 87.5% | 50.0% | 100.0% | 0.0% | 0/14 | 2848 | 22615 |
| `local/GPT-OSS-120B` | 92.9% | 100.0% | 92.9% | 87.5% | 50.0% | 100.0% | 0.0% | 0/14 | 3637 | 143926 |
| `local/Qwen3.6-27B-Think` | 92.9% | 100.0% | 92.9% | 87.5% | 50.0% | 100.0% | 0.0% | 0/14 | 14521 | 35278 |
| `local/Qwen3.6-35B-Heretic-MXFP4` | 85.7% | 100.0% | 85.7% | 87.5% | 50.0% | 100.0% | 0.0% | 0/14 | 2429 | 11333 |
| `local/Qwen3.5-4B-Compact` | 85.7% | 100.0% | 85.7% | 87.5% | 50.0% | 100.0% | 0.0% | 0/14 | 2583 | 6502 |
| `groq/meta-llama/llama-4-scout-17b-16e-instruct` | 71.4% | 100.0% | 85.7% | 87.5% | 50.0% | 85.7% | 0.0% | 0/14 | 477 | 4552 |
| `local/Qwen3.5-122B` | 14.3% | 100.0% | 14.3% | 0.0% | 0.0% | 100.0% | 0.0% | 0/14 | 6519 | 57798 |

## Takeaways

- The dense **Qwen3.6-35B** family (`-Abl`, `-Think`, base) clears both `args 100%` and `chain 100%` — the hard multi-step routing combo — consistently across runs. `Qwen3.6-35B-Abl` is kept as the chat brain.
- `groq/llama-3.3-70b-versatile` matched that top tier in this run, but it is a non-deterministic free tier that varies run-to-run, so the local routing decision does not depend on it.
- Everything else (Gemini Flash / Flash-Lite, GPT-OSS-120B, Gemma4-26B, 27B-Think) plateaus at `chain 50%` / `args 87.5%`.
- **Gemini Flash-Lite** is the fast cloud fallback (sub-second p50).
- `Qwen3.5-122B` is a poor router despite its size; `Qwen3.6-27B-Think` is disqualified on latency.
