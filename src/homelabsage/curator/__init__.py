"""Curator — generates per-container Markdown notes for the user's `notes/`.

Public surface re-exported here so callers can keep using
`from homelabsage.curator import Curator, ContainerSnapshot, …` regardless of
the internal split between core / helpers / prompts.
"""

from __future__ import annotations

from .core import ContainerSnapshot, CurateResult, Curator
from .helpers import (
    FOOTER_RE,
    digest_short,
    existing_footer,
    filter_labels,
    format_mounts,
    format_ports,
    is_purpose_fallback,
    redact_env,
    safe_filename,
    strip_fences,
    truncate,
)
from .prompts import (
    DEFAULT_PROMPT_TEMPLATE,
    PROMPT_PLACEHOLDERS,
    SafePromptDict,
)

# Backwards-compat aliases. The pre-refactor module used leading-underscore
# names for helpers; tests and any external code that pinned those imports
# keep working without churn.
_FOOTER_RE = FOOTER_RE
_digest_short = digest_short
_existing_footer = existing_footer
_filter_labels = filter_labels
_format_mounts = format_mounts
_format_ports = format_ports
_redact_env = redact_env
_safe_filename = safe_filename
_strip_fences = strip_fences
_truncate = truncate
_SafePromptDict = SafePromptDict


__all__ = [
    "ContainerSnapshot",
    "CurateResult",
    "Curator",
    "DEFAULT_PROMPT_TEMPLATE",
    "PROMPT_PLACEHOLDERS",
    "is_purpose_fallback",
]
