"""Data models shared by core, plugins and outputs."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Severity(StrEnum):
    """Severity assigned by the LLM to an update."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    INFO = "info"

    @property
    def order(self) -> int:
        return {"info": 0, "medium": 1, "high": 2, "critical": 3}[self.value]


class UpdateStatus(StrEnum):
    """Tracking state of a detected update in the local DB."""

    NEW = "new"
    ANALYZED = "analyzed"
    APPLIED = "applied"
    DISMISSED = "dismissed"
    FAILED = "failed"


class Update(BaseModel):
    """A single available update detected by a plugin.

    `source` is the plugin id (e.g. "docker", "homeassistant").
    `subject` is what is being updated (e.g. "OpenClaw" container,
    "core" for HA Core, "hacs:integrations/foo" for HACS, etc).
    """

    source: str
    subject: str
    current_version: str
    new_version: str
    release_url: str | None = None
    release_notes: str | None = None
    # Free-form context the plugin wants to forward to the LLM
    # (config snippets, install method, etc).
    context: dict[str, Any] = Field(default_factory=dict)


class Analysis(BaseModel):
    """LLM analysis of an Update.

    All fields are produced by the LLM following a strict JSON schema.
    """

    severity: Severity
    summary: str
    breaking_changes: list[str] = Field(default_factory=list)
    config_obsolete: list[str] = Field(default_factory=list)
    new_features_relevant: list[str] = Field(default_factory=list)
    action_required: bool = False
    recommended_action: str | None = None


class AnalyzedUpdate(BaseModel):
    """An Update plus its Analysis, ready to be persisted and routed to outputs."""

    update: Update
    analysis: Analysis | None = None
    status: UpdateStatus = UpdateStatus.NEW
    detected_at: datetime = Field(default_factory=datetime.utcnow)
    analyzed_at: datetime | None = None

    @property
    def id(self) -> str:
        """Stable identifier across runs: source + subject + new_version."""
        return f"{self.update.source}:{self.update.subject}:{self.update.new_version}"
