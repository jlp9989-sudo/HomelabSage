"""Pre-LLM secret-leak guard — redact API keys, tokens, SSH keys and
passwords from anything we're about to send to a remote LLM.

Why this exists *separately* from `redact.py`:

  - `redact.py` is the export sanitiser — it runs at the user's request
    via `homelabsage export --redact`, can rewrite IPs/hostnames into
    stable aliases, and is happy to be noisy. The user reads the output.
  - This module runs *silently* on every LLM call. It only redacts what
    is clearly a credential (high-confidence patterns). Over-redacting
    here would degrade analysis quality — if the LLM sees `<redacted>`
    where it expected a real value, its verdict gets noisy.

The guard composes from `redact.SECRET_KEY_MARKERS` + `_VALUE_PATTERNS`
plus a small extra set of high-confidence whole-line patterns (SSH
private key headers, multi-line PEM blocks, AWS access keys, etc).

It is on by default for *cloud* providers (`openai`, `groq`, `gemini`,
`openrouter`, `anthropic`) and off by default for `ollama` / `disabled`
local providers — sending secrets to your own GPU isn't a leak, and
local users may legitimately want full-fidelity prompts in
`analysis_explainers`. Users can override per-provider.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .redact import SECRET_KEY_MARKERS, _looks_like_credential

PLACEHOLDER = "<redacted-by-homelabsage>"

# Extra high-confidence patterns matched against arbitrary text (not just
# dict values). Lines matching these are wholesale replaced with the
# placeholder, regardless of context.
_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # SSH private key headers (begin/end + entire body)
    re.compile(r"-----BEGIN (?:OPENSSH|RSA|EC|DSA|PGP) PRIVATE KEY-----"),
    re.compile(r"-----END (?:OPENSSH|RSA|EC|DSA|PGP) PRIVATE KEY-----"),
    # AWS access keys
    re.compile(r"AKIA[0-9A-Z]{16}"),
    # AWS secret access keys are 40 base64 chars — be conservative and
    # only flag when preceded by 'secret_access_key' on the same line.
    re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*[\"']?([A-Za-z0-9/+]{40})[\"']?"),
    # Slack incoming-webhook URLs
    re.compile(r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]{20,}"),
    # Discord webhook URLs
    re.compile(r"https://discord\.com/api/webhooks/\d{15,}/[A-Za-z0-9_-]{50,}"),
    # Telegram bot tokens
    re.compile(r"\b\d{9,11}:[A-Za-z0-9_-]{30,}\b"),
    # GitHub fine-grained PATs (separate from gh[ps]_ classic)
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
)

# Inline patterns: anywhere in a string. We replace only the match.
_INLINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgh[ps]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9_.-]{20,}\b"),
)

# `KEY=value` / `KEY: value` shaped lines whose key matches a secret marker.
# We use a non-greedy capture for the value so we don't eat the rest of a
# multi-pair line, and a `[^\n]` value to keep it on one line.
_KV_LINE_RE = re.compile(
    r"(?i)\b([A-Z][A-Z0-9_]{1,}(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|AUTH|CREDENTIAL|PRIVATE|SESSION|SALT|SIGNING_KEY|CLIENT_SECRET))\s*[:=]\s*([^\s\n][^\n]*)"
)


# Providers where the guard is on by default. The intuition: local
# providers run on hardware the user owns; cloud providers are external
# parties whose retention policies are out of the user's control.
_CLOUD_PROVIDERS: frozenset[str] = frozenset({
    "openai", "groq", "gemini", "openrouter", "anthropic",
})


@dataclass
class GuardReport:
    """What was redacted on this call. Useful for `analysis_explainers`
    so the user can see "we redacted 3 secrets before sending"."""

    redacted_count: int = 0
    matched_categories: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.matched_categories is None:
            self.matched_categories = []

    def note(self, category: str) -> None:
        self.redacted_count += 1
        if category not in self.matched_categories:
            self.matched_categories.append(category)


def should_guard(provider: str | None, *, override: bool | None = None) -> bool:
    """Decide whether to run the guard.

    `override=True` forces on, `override=False` forces off, `None` falls
    back to the provider-default (cloud=on, local=off).
    """
    if override is not None:
        return override
    if not provider:
        return True
    return provider.lower() in _CLOUD_PROVIDERS


def _redact_kv_line(text: str, report: GuardReport) -> str:
    def _sub(m: re.Match[str]) -> str:
        report.note("kv_line")
        return f"{m.group(1)}={PLACEHOLDER}"

    return _KV_LINE_RE.sub(_sub, text)


def _redact_inline(text: str, report: GuardReport) -> str:
    out = text
    for pat in _INLINE_PATTERNS:
        def _sub(m: re.Match[str], _pat=pat) -> str:
            report.note(f"inline:{_pat.pattern[:24]}")
            return PLACEHOLDER

        out = pat.sub(_sub, out)
    return out


def _redact_line_block(text: str, report: GuardReport) -> str:
    """Whole-block patterns (PEM keys span multiple lines).

    Strategy: if any of the block headers matches, replace from the
    header to the matching footer with a single placeholder line. We
    keep the line break shape so downstream prompt token counts don't
    blow up.
    """
    out = text
    # PEM blocks — match the whole block lazily across lines.
    def _pem_sub(_m: re.Match[str]) -> str:
        report.note("pem_block")
        return PLACEHOLDER

    out = re.sub(
        r"-----BEGIN (?:OPENSSH|RSA|EC|DSA|PGP) PRIVATE KEY-----.*?-----END (?:OPENSSH|RSA|EC|DSA|PGP) PRIVATE KEY-----",
        _pem_sub,
        out,
        flags=re.DOTALL,
    )
    for pat in _LINE_PATTERNS:
        # Skip the PEM headers — already handled above as a block.
        if "PRIVATE KEY" in pat.pattern:
            continue

        def _sub(m: re.Match[str], _pat=pat) -> str:
            report.note(f"line:{_pat.pattern[:24]}")
            return PLACEHOLDER

        out = pat.sub(_sub, out)
    return out


def redact_text(text: str, *, report: GuardReport | None = None) -> tuple[str, GuardReport]:
    """Run every redaction pass against a free-form string.

    Returns (redacted_text, report). `report` is created if not provided
    so the caller can choose whether to thread it across multiple texts.
    """
    rep = report or GuardReport()
    if not text:
        return text, rep
    out = _redact_line_block(text, rep)
    out = _redact_kv_line(out, rep)
    out = _redact_inline(out, rep)
    return out, rep


def redact_context(ctx: dict, *, report: GuardReport | None = None) -> tuple[dict, GuardReport]:
    """Walk a JSON-shaped context dict in-place-ish (returns a new dict).

    Dict keys matching `SECRET_KEY_MARKERS` get their values replaced
    with the placeholder. String values get the text passes. Lists are
    walked. Anything else is returned as-is.
    """
    rep = report or GuardReport()

    def _walk(value):
        if isinstance(value, dict):
            out: dict = {}
            for k, v in value.items():
                if isinstance(k, str) and any(m in k.lower() for m in SECRET_KEY_MARKERS):
                    rep.note(f"key:{k}")
                    out[k] = PLACEHOLDER
                else:
                    out[k] = _walk(v)
            return out
        if isinstance(value, list):
            return [_walk(v) for v in value]
        if isinstance(value, str):
            if _looks_like_credential(value):
                rep.note("credential_value")
                return PLACEHOLDER
            redacted, _ = redact_text(value, report=rep)
            return redacted
        return value

    return _walk(ctx), rep
