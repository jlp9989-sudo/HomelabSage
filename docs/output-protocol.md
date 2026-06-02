# Output protocol

How HomelabSage routes analyzed updates to external systems. Read this if
you're adding a new notification channel or wondering why a particular
push didn't fire.

## The `Output` base class

```python
class Output(ABC):
    id: str = ""              # stable identifier; doubles as the pending_dispatches output_id
    is_push: bool = False     # True if this output produces a user-visible alert
                              # that should be silenced during a parity check

    @abstractmethod
    async def send(self, item: AnalyzedUpdate) -> None:
        ...
```

Each output is one file under `src/homelabsage/outputs/`. The engine
instantiates outputs from `Config.outputs` on every scan (via
`build_outputs`); there's no shared state between scans.

## Contract

Implementations must satisfy these properties. Tests enforce most of
them; review reviewers should check the rest by hand.

### 1. `send()` is best-effort

Failures (network down, wrong token, rate-limit) must be logged and
swallowed. The next output in the chain still gets a chance; a future
scan that produces the same Update will retry naturally.

```python
async def send(self, item):
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(...)
            r.raise_for_status()
    except httpx.HTTPError as e:
        log.error("MyOutput push failed for %s: %s", item.id, e)
```

### 2. Severity gate up front

Every push output respects a `min_severity` config field. Check it
inside `_should_send()` and return early before any HTTP work:

```python
def _should_send(self, item):
    if not self.cfg.enabled or not self.cfg.bot_token:
        return False
    if not item.analysis:
        return False
    return item.analysis.severity.order >= self._min.order
```

### 3. Idempotency

Re-sending the same `AnalyzedUpdate` should NOT create user-visible
duplicates if you can help it. Notion uses `notion_page_id` (cached in
SQLite) to PATCH instead of POST. Push channels typically don't have a
dedup mechanism (Telegram in particular doesn't), so:

- The engine de-dupes by `Update.id` before invoking outputs (analyzed
  items skip the per-item loop entirely on the next scan).
- `homelabsage notify-pending` exists for manual replay; the user is
  expected to keep the window small.

### 4. `is_push` semantics

`is_push = True` means "this output produces a user-visible alert that
would be disruptive mid-incident". The four built-in push channels
(Telegram, Discord, Ntfy, Gotify) all set it. Notion is persistent —
writing a DB row is not disruptive — so it stays `False`.

When the parity gate is enabled and active, the engine:

1. Skips outputs with `is_push = True`.
2. Inserts a row into `pending_dispatches(update_id, output_id, queued_at)`.
3. On the next ungated scan, drains the queue: `output.send(item)` per
   row, then `DELETE` on success.

If `output.send` raises during the drain, the row stays in the queue
and retries next scan. Items whose `analysis` is gone (stale row,
manually purged) get their queue rows removed silently.

### 5. The weekly digest is NOT an Output

The digest is its own dispatcher in `src/homelabsage/digest.py`. It
posts a single rollup to whichever channels the user enabled in
`digest.channels` (auto-detect = "every push output that's `enabled`").

By default the digest IGNORES the parity gate — it's the backstop for
real-time pings that were skipped during the parity window. Users who
want strict silence flip `parity_gate.skip_digest_too = true`; then the
scheduler hook re-checks the gate before firing.

## Config block conventions

Every output ships its config submodel in `src/homelabsage/config/outputs.py`:

```python
class MyOutputConfig(BaseModel):
    enabled: bool = False
    # … channel-specific fields, all with explicit defaults …
    min_severity: Literal["critical", "high", "medium", "info"] = "high"
```

Then add it to `OutputsConfig` at the bottom of the same file:

```python
class OutputsConfig(BaseModel):
    # … existing …
    myoutput: MyOutputConfig = Field(default_factory=MyOutputConfig)
```

The settings UI auto-renders the form. To make it appear in the index
page (`/settings`) and accept connection-test calls, also add it to
`SETTING_BLOCKS` in `src/homelabsage/web/routes_settings.py` and
register a connection test in `routes_settings_test.py`.

## Connection-test endpoints

Each output should have a `/settings/outputs/<name>/test` POST endpoint
that sends a real "this is a test" message via the same path
`send()` uses. The endpoint reads the SAVED config (not the form
values) so the user's "edit → save → test" loop is consistent.

The endpoint returns an HTML fragment for HTMX swap; the helper
function returns `(ok: bool, message: str)` for unit-test friendliness.
Pattern lives in `routes_settings_test.py`.

## Severity-to-priority mapping

Each push output decides how to render severity. The conventions we've
landed on:

| Severity | Telegram | Discord embed | Ntfy priority | Gotify priority |
|----------|----------|---------------|---------------|-----------------|
| critical | 🔴        | red stripe    | 5             | 8               |
| high     | 🟠        | orange stripe | 4             | 6               |
| medium   | 🟡        | yellow stripe | 3             | 4               |
| info     | 🔵        | blue stripe   | 2             | 2               |

The Gotify Output supports `priority_overrides: dict[str, int]` for
users who want to force full-screen on `critical` (set to 10) without
patching code.

## Testing checklist

When you add a new output:

- [ ] `test_outputs_<name>.py` covers: disabled / missing-token /
      below-severity / happy-path / HTTP error swallowed.
- [ ] `test_routes_settings_test.py` covers the connection-test
      endpoint with monkeypatched HTTP.
- [ ] `test_routes_settings_v030_blocks.py` (or its successor) covers
      `GET /api/settings/outputs/<name>` returning the schema +
      defaults.
- [ ] If `is_push = True`, the parity-gate auto-flush already covers
      you (it iterates `engine.outputs` by id); no extra test needed.
