# HomelabSage

AI-powered homelab analyzer, update tracker and improvement advisor.

Watches your stack (Docker containers, Home Assistant, Linux packages, firmware, news feeds, RSS) and uses a local LLM to tell you, for each update:

- Whether there are **breaking changes** that affect *your* current config.
- Whether parts of your **setup are obsolete** because the new version brings them built-in.
- Whether there are **new features relevant to your homelab**.
- A short, structured summary so you don't have to read raw release notes.

Pluggable: each "source" (Docker, HA, Fedora, llama.cpp, ROCm, HF models, RSS feeds, hardware/firmware) is an independent plugin. The local LLM (Ollama-compatible API — Ollama, llama.cpp server, LM Studio, etc.) is the brain.

Status: pre-alpha, in active development. Not yet ready for general use.

## License

MIT — see [LICENSE](LICENSE).
