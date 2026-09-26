"""Telegram mutating tools: send messages to channels."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gateway.config import Settings, get_settings
from gateway.db.models import TelegramDelivery, TelegramDestination
from gateway.providers.base import CallContext, RiskLevel, ToolSpec
from gateway.providers.registry import ToolError
from gateway.providers.telegram.client import (
    PROVIDER,
    TELEGRAM_API_KEY,
    TelegramProviderError,
    telegram_request,
)
from gateway.providers.telegram.service import execution_key, get_connection

ParseMode = Literal["Markdown", "MarkdownV2", "HTML"]


class SendMessageInput(BaseModel):
    destination: str = Field(
        ...,
        min_length=2,
        max_length=64,
        description="Verified destination alias from telegram_list_destinations.",
    )
    text: str = Field(..., min_length=1, max_length=4096, description="Message text to send.")
    parse_mode: ParseMode | None = Field(
        None, description="Optional text formatting: Markdown, MarkdownV2, or HTML."
    )
    disable_notification: bool = Field(
        False, description="Send silently without notifying subscribers."
    )


def send_message(args: SendMessageInput, ctx: CallContext, session: Session) -> Any:
    conn = get_connection(session, ctx.user_id)
    if conn is None:
        raise ToolError("not_connected", "Connect Telegram in Settings before publishing.")
    destination = session.scalar(
        select(TelegramDestination).where(
            TelegramDestination.connection_id == conn.id,
            TelegramDestination.alias == args.destination.strip().lower(),
            TelegramDestination.enabled.is_(True),
        )
    )
    if destination is None:
        raise ToolError("forbidden", "Telegram destination is not verified or enabled.")

    delivery_key, content_hash = execution_key(ctx, destination.id, args.text)
    existing = session.scalar(
        select(TelegramDelivery).where(TelegramDelivery.execution_key == delivery_key)
    )
    if existing is not None:
        if existing.status == "sent":
            return {
                "status": "sent",
                "duplicate": True,
                "destination": destination.alias,
                "message_id": existing.telegram_message_id,
            }
        raise ToolError(
            "delivery_in_progress" if existing.status == "pending" else "delivery_unknown",
            "This Telegram publication was already attempted and will not be retried "
            "automatically.",
        )

    delivery = TelegramDelivery(
        user_id=ctx.user_id,
        destination_id=destination.id,
        execution_key=delivery_key,
        content_hash=content_hash,
        status="pending",
    )
    session.add(delivery)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ToolError(
            "delivery_in_progress", "This Telegram publication is already running."
        ) from exc

    body: dict[str, Any] = {
        "chat_id": destination.chat_id,
        "text": args.text,
        "disable_notification": args.disable_notification,
    }
    if args.parse_mode is not None:
        body["parse_mode"] = args.parse_mode

    settings = get_settings()
    bot_token = TELEGRAM_API_KEY.get_credentials(session, ctx, settings)
    try:
        result = telegram_request(settings, bot_token, "sendMessage", json=body)
    except TelegramProviderError as exc:
        delivery.status = "indeterminate" if exc.delivery_unknown else "failed"
        delivery.error_code = exc.error_code
        session.commit()
        raise

    telegram_message = result.get("result", {}) if isinstance(result, dict) else {}
    delivery.status = "sent"
    delivery.telegram_message_id = str(telegram_message.get("message_id", "")) or None
    session.commit()
    return {
        "status": "sent",
        "duplicate": False,
        "destination": destination.alias,
        "message_id": delivery.telegram_message_id,
        "sent_at": datetime.now(UTC).isoformat(),
    }


def _send_message_preview(args: SendMessageInput) -> str:
    snippet = args.text[:120] + ("..." if len(args.text) > 120 else "")
    return f'Send Telegram message to {args.destination}: "{snippet}"'


def register(registry, settings: Settings) -> None:
    """Register Telegram send-message tool with the tool registry."""
    registry.add(
        ToolSpec(
            name="telegram_send_message",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            description=(
                "Post a text message to one verified Telegram channel alias using the current "
                "user's connected bot. Publishing is immediate and idempotent for the current run."
            ),
            input_model=SendMessageInput,
            handler=send_message,
            preview_builder=None,
            requires_confirmation=False,
        )
    )
