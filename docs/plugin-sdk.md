# Plugin SDK

A "plugin" in HomelabSage = **one source of updates**. Docker containers, Home Assistant Core, Fedora dnf, llama.cpp releases, a Hugging Face model revision, an RSS feed of forum announcements — anything that has a notion of "version available" can be a plugin.

A plugin's only job is to emit a list of `Update` items. The engine does everything else: LLM analysis, dedup, persistence, routing to outputs.

This is by design — it means the LLM-driven analysis applies uniformly across sources, and adding a new source doesn't drag along its own analysis logic.

---

## The contract

```python
# src/homelabsage/plugins/__init__.py

class Plugin(ABC):
    id: str = ""                                  # stable; used as Update.source

    @abstractmethod
    async def scan(self) -> list[Update]:
        """Return all updates available right now (idempotent)."""
```

Two rules:

1. **`scan()` is async.** Even if your data source is sync, wrap blocking work in `asyncio.to_thread`. The engine runs plugins sequentially, but other plugins shouldn't be blocked.
2. **`scan()` is idempotent.** Calling it twice without any external change must return the same list. Dedup is handled by the engine via stable id `source:subject:new_version`, so don't try to track "what I already returned" yourself.

---

## The `Update` model

```python
# src/homelabsage/models.py

class Update(BaseModel):
    source: str                      # = your plugin's id
    subject: str                     # what is being updated ("mealie", "core", "kernel"…)
    current_version: str             # what the user currently runs
    new_version: str                 # what's available upstream
    release_url: str | None = None   # link to changelog / release page
    release_notes: str | None = None # raw markdown — the LLM reads this
    context: dict[str, Any] = {}     # any extra fields you want to pass to the LLM
```

Notes:

- `subject` should be **stable across versions**. If today's subject is `"mealie"` and tomorrow's is `"Mealie"`, the engine sees two different things.
- `release_notes` is what the LLM actually reads. Fetch the changelog when you can; if the upstream releases page has none, leave it `None` and the LLM will rely on `release_url` + `context`.
- `context` is a free-form dict, serialised verbatim into the prompt. Use it for things the LLM should know about *this specific instance* — install method, image variant, environment flags, etc. Keep it small (<1 KB).
- There's a magic key `context["_note_keywords"]`. If your plugin can produce extra keywords beyond `subject` (e.g. the docker plugin adds the image's short name), put them here. The `NotesProvider` will match notes against `subject + _note_keywords`.

---

## Minimal example

A plugin that watches a single RSS feed for new entries:

```python
# src/homelabsage/plugins/rss_demo.py
from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET

import httpx

from ..models import Update
from . import Plugin


class RSSDemoPlugin(Plugin):
    id = "rss_demo"

    def __init__(self, feed_url: str):
        self.feed_url = feed_url

    async def scan(self) -> list[Update]:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(self.feed_url)
            r.raise_for_status()
        root = await asyncio.to_thread(ET.fromstring, r.text)

        updates: list[Update] = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc = (item.findtext("description") or "").strip()
            if not title:
                continue
            updates.append(
                Update(
                    source=self.id,
                    subject=title,
                    current_version="",          # RSS has no "current" — leave empty
                    new_version=link or title,   # link is unique per item
                    release_url=link,
                    release_notes=desc,
                )
            )
        return updates
```

That's the whole plugin. ~30 lines.

---

## Wiring it in

Plugins are registered in `engine.build_plugins()` (gate on `config.sources.<id>.enabled`):

```python
# src/homelabsage/engine.py
def build_plugins(cfg: Config) -> list[Plugin]:
    plugins: list[Plugin] = []
    if cfg.sources.docker.enabled:
        plugins.append(DockerPlugin(cfg.sources.docker))
    if cfg.sources.rss_demo.enabled:
        plugins.append(RSSDemoPlugin(cfg.sources.rss_demo.feed_url))
    return plugins
```

And declared in `config.py` so the YAML parser knows about it:

```python
# src/homelabsage/config.py
class RSSDemoConfig(BaseModel):
    enabled: bool = False
    feed_url: str = ""

class SourcesConfig(BaseModel):
    docker: DockerSourceConfig = DockerSourceConfig()
    rss_demo: RSSDemoConfig = RSSDemoConfig()
    # …
```

Users now write:

```yaml
sources:
  rss_demo:
    enabled: true
    feed_url: https://blog.example.com/feed
```

No code outside these three points changes.

---

## Real-world reference: the Docker plugin

`src/homelabsage/plugins/docker.py` is the most production-tested plugin and demonstrates the patterns you'll likely need.

It does roughly:

1. List running containers via the Docker SDK.
2. Skip any matching the user's `skip` regexes.
3. For each container, resolve a GitHub repo three ways, in order:
   - Explicit override in config (`overrides: { mealie: hay-kot/mealie }`)
   - OCI label `org.opencontainers.image.source` on the image
   - GHCR-style heuristic on the image ref (`ghcr.io/owner/repo:tag`)
4. Parse the current tag as semver. **Crucially**, reject anything that doesn't match `^v?\d+(?:\.\d+){1,3}` — that prevents tags like `openvino`, `cuda`, `alpine`, `latest` from being compared as if they were versions. (See `test_docker_plugin.py` for the regression tests.)
5. Hit the GitHub Releases API for the latest release.
6. If the latest semver tag is strictly greater than the current, emit an `Update` with release notes and an OCI-label-derived `context`.

Patterns worth copying:

- **Skip aggressively, not blindly.** Bad data → false positives → user loses trust. Better to miss an update than report a phantom one. The semver gate is the single biggest contributor to plugin quality.
- **Use OCI labels.** They're standardised, free metadata. `org.opencontainers.image.source` is the canonical "where does this image come from" pointer.
- **Forward what you know.** Put image ref, install method, env flags etc. into `context` so the LLM can reason about your specific setup, not the generic upstream changelog.

---

## Testing your plugin

Drop a file under `tests/`:

```python
# tests/test_rss_demo_plugin.py
import pytest
from homelabsage.plugins.rss_demo import RSSDemoPlugin


@pytest.mark.asyncio
async def test_rss_demo_parses_basic_feed(httpx_mock):
    httpx_mock.add_response(
        url="https://example.com/feed",
        text="""<rss><channel>
            <item><title>1.2.3 released</title>
                  <link>https://example.com/1.2.3</link>
                  <description>changelog body</description></item>
        </channel></rss>""",
    )
    plugin = RSSDemoPlugin("https://example.com/feed")
    items = await plugin.scan()
    assert len(items) == 1
    assert items[0].subject == "1.2.3 released"
    assert items[0].release_notes == "changelog body"
```

Run with `pytest -q`. The project uses `asyncio_mode = "auto"`, so `@pytest.mark.asyncio` isn't strictly necessary on every test, but it's good practice for clarity.

Try to cover:

- **Empty / malformed input** — what if the upstream API returns garbage? An exception is fine (the engine catches and logs per-plugin), but the plugin shouldn't crash the whole run.
- **Version comparison edge cases** — pre-releases, `v`-prefixes, non-semver tags. Borrow `test_docker_plugin.py::test_semver_re_*` patterns.
- **Idempotency** — call `scan()` twice on the same fixture; assert identical output.

---

## What plugins should NOT do

- **No analysis.** Don't decide whether the update is "important". That's the LLM's job, informed by user notes.
- **No state.** Don't write files, don't update SQLite. The engine persists `AnalyzedUpdate` rows; you just emit `Update` items.
- **No I/O on import.** Constructors should be cheap; do all network calls in `scan()`. The CLI imports every plugin even when only running `homelabsage list`.
- **No catching `Exception` silently.** Let it bubble. The engine wraps each plugin's `scan()` in a try/except and continues with the next plugin. If you swallow errors, you lose observability.

---

## Future helpers

These are not built yet, but planned:

- `homelabsage.plugins.helpers.semver_gate(current, new)` — the same logic the Docker plugin uses. Will be extracted once a second plugin needs it.
- `homelabsage.plugins.helpers.github_releases(repo, since=...)` — wraps `github.py` and yields the parsed releases. Useful for plugins watching upstream Git repos (llama.cpp, ROCm, etc.).

If you find yourself rewriting these, please open a PR moving them to `plugins/helpers/`.

---

## Submitting

1. Open an issue first if your plugin watches anything *not* already in the planned list (`fedora`, `llamacpp`, `huggingface_models`, `unraid`, `rss_feeds`). Saves duplicate work.
2. Plugin code under `src/homelabsage/plugins/<id>.py`.
3. Config schema in `src/homelabsage/config.py` (one `BaseModel` + one field on `SourcesConfig`).
4. Wire-up in `engine.build_plugins`.
5. Tests under `tests/test_<id>_plugin.py`. CI runs them on every PR.
6. Update `config.example.yaml` with a commented-out section showing the new source.
7. Add a one-line entry under "Plugins" in the README's architecture diagram.

That's it. Plugins are intentionally trivial to add — if yours is taking more than 200 lines, you might be doing too much in the plugin itself.
