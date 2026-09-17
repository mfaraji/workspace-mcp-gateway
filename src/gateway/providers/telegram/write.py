"""Telegram mutating tools: send messages to channels."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gateway.config import Settings, get_settings
from gateway.providers.base import CallContext, RiskLevel, ToolSpec
from gateway.providers.telegram.client import PROVIDER, TELEGRAM_API_KEY, telegram_request

ParseMode = Literal["Markdown", "MarkdownV2", "HTML"]


class SendMessageInput(BaseModel):
    chat_id: str = Field(
        ...,
        description="Target channel: @username for public channels or numeric id (e.g. -100...).",
    )
    text: str = Field(..., min_length=1, description="Message text to send.")
    parse_mode: ParseMode | None = Field(
        None, description="Optional text formatting: Markdown, MarkdownV2, or HTML."
    )
    disable_notification: bool = Field(
        False, description="Send silently without notifying subscribers."
    )


def send_message(args: SendMessageInput, ctx: CallContext, session: Session) -> Any:
    body: dict[str, Any] = {
        "chat_id": args.chat_id,
        "text": args.text,
        "disable_notification": args.disable_notification,
    }
    if args.parse_mode is not None:
        body["parse_mode"] = args.parse_mode

    settings = get_settings()
    bot_token = TELEGRAM_API_KEY.get_credentials(session, ctx, settings)
    return telegram_request(settings, bot_token, "sendMessage", json=body)


def _send_message_preview(args: SendMessageInput) -> str:
    snippet = args.text[:120] + ("..." if len(args.text) > 120 else "")
    return f'Send Telegram message to {args.chat_id}: "{snippet}"'


def register(registry, settings: Settings) -> None:
    """Register Telegram send-message tool with the tool registry."""
    require_confirmation = settings.telegram_require_confirmation
    risk = RiskLevel.MUTATING if require_confirmation else RiskLevel.READ

    registry.add(
        ToolSpec(
            name="telegram_send_message",
            provider=PROVIDER,
            risk=risk,
            description=(
                "Post a text message to a Telegram channel (or chat) using your connected bot. "
                "The bot must be an administrator of the channel with permission to post. "
                "Use @channelusername or a numeric chat_id. "
                + (
                    "Requires confirmation before sending."
                    if require_confirmation
                    else "Sends immediately."
                )
            ),
            input_model=SendMessageInput,
            handler=send_message,
            preview_builder=_send_message_preview if require_confirmation else None,
        )
    )
