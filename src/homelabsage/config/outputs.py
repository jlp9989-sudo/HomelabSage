"""Notification-output configuration: Notion, Telegram, Discord, Ntfy, Gotify."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class NotionOutputConfig(BaseModel):
    enabled: bool = False
    api_key: str = Field(
        "",
        description=(
            "Internal Integration Secret. Create one at "
            "https://www.notion.so/profile/integrations, then SHARE the target "
            "database with the integration (the secret alone is not enough)."
        ),
    )
    database_id: str = Field(
        "",
        description=(
            "32-char hex id from the database URL. Open the database in Notion, "
            "click ⋯ → Copy link; the id is the 32 chars after the last `/` and "
            "before the `?`."
        ),
    )
    write_policy: Literal["always", "only_action_required"] = Field(
        "always",
        description="`always` writes every analyzed update; `only_action_required` skips info-level ones.",
    )


class TelegramOutputConfig(BaseModel):
    enabled: bool = False
    bot_token: str = Field(
        "",
        description="Token from @BotFather. Talk to him in Telegram, /newbot, paste the token he gives you.",
    )
    chat_id: str = Field(
        "",
        description=(
            "Numeric chat id. For personal chat: talk to @userinfobot and use the `Id` it returns. "
            "For groups: add the bot, send `/start@yourbot`, then call `getUpdates`."
        ),
    )
    min_severity: Literal["critical", "high", "medium", "info"] = Field(
        "high",
        description="Only push updates at or above this severity. Use `critical` to get CVEs only.",
    )


class DiscordOutputConfig(BaseModel):
    enabled: bool = False
    webhook_url: str = Field(
        "",
        description=(
            "Channel webhook URL. In Discord: Server Settings → Integrations "
            "→ Webhooks → New Webhook → Copy URL. Anyone with this URL can "
            "post to the channel, so treat it as a secret."
        ),
    )
    username: str = Field(
        "HomelabSage",
        description="Overrides the webhook's default display name on each message.",
    )
    avatar_url: str = Field(
        "",
        description="Optional avatar URL displayed next to each message.",
    )
    min_severity: Literal["critical", "high", "medium", "info"] = Field(
        "high",
        description="Only push updates at or above this severity.",
    )


class NtfyOutputConfig(BaseModel):
    enabled: bool = False
    server_url: str = Field(
        "https://ntfy.sh",
        description=(
            "Base URL of the ntfy server. Use `https://ntfy.sh` for the "
            "public instance or your own self-hosted URL."
        ),
    )
    topic: str = Field(
        "",
        description=(
            "Topic name. Subscribers will follow `<server_url>/<topic>`. Use "
            "a random hard-to-guess string for anything sensitive — topics "
            "are effectively shared secrets."
        ),
    )
    auth_token: str = Field(
        "",
        description=(
            "Optional bearer token for self-hosted ntfy with access control. "
            "Leave empty for unauthenticated topics (the ntfy.sh default)."
        ),
    )
    min_severity: Literal["critical", "high", "medium", "info"] = Field(
        "high",
        description="Only push updates at or above this severity.",
    )


class GotifyOutputConfig(BaseModel):
    enabled: bool = False
    server_url: str = Field(
        "",
        description="Base URL of the Gotify server, e.g. `https://gotify.example.com`.",
    )
    token: str = Field(
        "",
        description=(
            "App token from Gotify. In the Gotify UI: APPS → Create Application → "
            "copy the generated token. One token per application/inbox."
        ),
    )
    min_severity: Literal["critical", "high", "medium", "info"] = Field(
        "high",
        description="Only push updates at or above this severity.",
    )
    priority_overrides: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Optional per-severity priority overrides (Gotify 1-10 scale). "
            "Example: `{\"critical\": 10}` to force full-screen alerts."
        ),
    )


class BatchingConfig(BaseModel):
    """Severity-aware notification batching.

    When enabled, push outputs accumulate items below `below_severity` and
    send a single rollup per scan rather than one ping each. Critical /
    High continue to fire immediately. Useful when `min_severity` is set
    to `info` or `medium` — without batching the user gets spammed with
    one push per touch.
    """

    enabled: bool = False
    below_severity: Literal["critical", "high", "medium", "info"] = Field(
        "high",
        description=(
            "Items STRICTLY BELOW this severity get batched. Default 'high' "
            "means medium + info are batched; high + critical fire immediately."
        ),
    )
    min_count: int = Field(
        2,
        description="Minimum number of batched items to send a rollup. Fewer "
        "are dispatched individually as today.",
    )


class OutputsConfig(BaseModel):
    notion: NotionOutputConfig = Field(default_factory=NotionOutputConfig)
    telegram: TelegramOutputConfig = Field(default_factory=TelegramOutputConfig)
    discord: DiscordOutputConfig = Field(default_factory=DiscordOutputConfig)
    ntfy: NtfyOutputConfig = Field(default_factory=NtfyOutputConfig)
    gotify: GotifyOutputConfig = Field(default_factory=GotifyOutputConfig)
    batching: BatchingConfig = Field(default_factory=BatchingConfig)
