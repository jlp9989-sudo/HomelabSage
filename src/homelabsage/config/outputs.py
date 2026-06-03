"""Notification-output configuration: Notion, Telegram, Discord, Ntfy, Gotify."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class QuietHoursMixin(BaseModel):
    """Mixin field set shared by every push output.

    Inheriting via mixin (not composition) so existing
    `cfg.outputs.telegram.min_severity` access stays flat — the
    settings UI's schema-driven renderer reads the JSON-Schema
    `properties` map which sees the inherited fields.
    """

    quiet_hours: str = Field(
        "",
        description=(
            "Quiet window in `HH:MM-HH:MM` form. During this window, push "
            "notifications from this channel are queued into "
            "`pending_dispatches` (same table the parity gate uses) and "
            "replay on the next ungated scan. Empty disables. Crosses "
            "midnight when start > end (e.g. `23:00-07:00`)."
        ),
    )
    quiet_hours_timezone: str = Field(
        "UTC",
        description="IANA timezone the quiet window is evaluated in.",
        json_schema_extra={"ui_widget": "timezone"},
    )
    quiet_hours_bypass_severity: Literal[
        "", "info", "medium", "high", "critical"
    ] = Field(
        "critical",
        description=(
            "Severity floor that escapes quiet hours. Default `critical`. "
            "Empty string = no escape — even critical messages queue."
        ),
    )


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


class TelegramOutputConfig(QuietHoursMixin):
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


class DiscordOutputConfig(QuietHoursMixin):
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


class NtfyOutputConfig(QuietHoursMixin):
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


class GotifyOutputConfig(QuietHoursMixin):
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


class AppriseOutputConfig(QuietHoursMixin):
    """Universal push wrapper via the apprise PyPI library.

    Apprise speaks 100+ services through a single URL syntax (Pushover,
    Mattermost, MS Teams, Slack, Mailgun, AWS SNS, etc.). The library
    is OPTIONAL — when missing the output degrades to a no-op with a
    one-time WARN log line.
    """

    enabled: bool = False
    urls: list[str] = Field(
        default_factory=list,
        description=(
            "List of apprise notify URLs. Examples: "
            "`pover://USER@TOKEN`, `slack://TOKEN_A/TOKEN_B/TOKEN_C`, "
            "`mailtos://user:pass@gmail.com`. See "
            "https://github.com/caronc/apprise/wiki for the full catalogue."
        ),
    )
    min_severity: Literal["critical", "high", "medium", "info"] = Field(
        "high",
        description="Only push updates at or above this severity.",
    )
    tag_severity: bool = Field(
        True,
        description=(
            "Map severity to apprise `notify_type` (failure/warning/info) "
            "so services that visualize urgency colour the message correctly."
        ),
    )


class SMTPOutputConfig(QuietHoursMixin):
    """Direct SMTP email output. No 3rd-party deps — uses stdlib `smtplib`.

    Most homelab users have an SMTP relay handy (the ISP's, a personal
    Mailgun account, the postfix container the family runs). This output
    is the no-frills "send a plain-text email when something fires" path.
    """

    enabled: bool = False
    host: str = Field("", description="SMTP server hostname.")
    port: int = Field(587, description=(
        "TCP port. 587 → STARTTLS, 465 → implicit SSL, anything else → "
        "cleartext (probably only OK for in-LAN relays)."
    ))
    username: str = Field("", description="Optional SMTP auth username.")
    password: str = Field("", description="Optional SMTP auth password.")
    use_tls: bool = Field(
        False,
        description=(
            "Force STARTTLS upgrade regardless of port. Default behaviour "
            "is: 587 always STARTTLS, 465 always implicit-SSL, other "
            "ports stay cleartext."
        ),
    )
    from_addr: str = Field("", description="Sender address (`From:` header).")
    to_addrs: list[str] = Field(
        default_factory=list,
        description="Recipient addresses. Each gets a separate message.",
    )
    subject_prefix: str = Field(
        "[HomelabSage]",
        description="String prepended to every subject for inbox filtering.",
    )
    timeout: int = Field(30, description="Connection/auth timeout in seconds.")
    min_severity: Literal["critical", "high", "medium", "info"] = Field(
        "high",
        description="Only push updates at or above this severity.",
    )


class WebhookOutputConfig(QuietHoursMixin):
    """Generic JSON-webhook output. Bring-your-own-receiver."""

    enabled: bool = False
    url: str = Field("", description="Endpoint URL. Receives a JSON POST.")
    bearer_token: str = Field(
        "",
        description="Optional. Sent as `Authorization: Bearer <token>`.",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Extra headers merged into every request.",
    )
    min_severity: Literal["critical", "high", "medium", "info"] = Field(
        "high",
        description="Only push updates at or above this severity.",
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
    apprise: AppriseOutputConfig = Field(default_factory=AppriseOutputConfig)
    smtp: SMTPOutputConfig = Field(default_factory=SMTPOutputConfig)
    webhook: WebhookOutputConfig = Field(default_factory=WebhookOutputConfig)
    batching: BatchingConfig = Field(default_factory=BatchingConfig)
