"""Telegram connection, destination validation, and delivery helpers."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from gateway.connectors.apikey import store_api_key
from gateway.db.models import ProviderConnection, TelegramDestination
from gateway.providers.base import CallContext
from gateway.providers.registry import ToolError
from gateway.providers.telegram.client import PROVIDER, TELEGRAM_API_KEY, telegram_request

_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


def get_connection(session: Session, user_id: uuid.UUID) -> ProviderConnection | None:
    return session.scalar(
        select(ProviderConnection).where(
            ProviderConnection.user_id == user_id,
            ProviderConnection.provider == PROVIDER,
            ProviderConnection.status == "active",
        )
    )


def connect_bot(session: Session, user_id: uuid.UUID, bot_token: str, settings) -> dict:
    profile = telegram_request(settings, bot_token.strip(), "getMe")
    bot = profile.get("result") if isinstance(profile, dict) else None
    if not isinstance(bot, dict) or not bot.get("id"):
        raise ToolError("provider_error", "Telegram returned an invalid bot profile.")

    previous = get_connection(session, user_id)
    previous_bot_id = (
        str((previous.connection_metadata or {}).get("bot_id")) if previous is not None else None
    )
    conn = store_api_key(session, user_id, PROVIDER, bot_token.strip())
    conn.connection_metadata = {
        "bot_id": str(bot["id"]),
        "username": bot.get("username"),
        "display_name": bot.get("first_name"),
    }
    if previous_bot_id and previous_bot_id != str(bot["id"]):
        for destination in conn.telegram_destinations:
            destination.enabled = False
    session.flush()
    return conn.connection_metadata


def disconnect_bot(session: Session, user_id: uuid.UUID) -> bool:
    conn = get_connection(session, user_id)
    if conn is None:
        return False
    session.delete(conn)
    session.flush()
    return True


def list_destinations(session: Session, user_id: uuid.UUID) -> list[TelegramDestination]:
    conn = get_connection(session, user_id)
    if conn is None:
        return []
    return list(
        session.scalars(
            select(TelegramDestination)
            .where(TelegramDestination.connection_id == conn.id)
            .order_by(TelegramDestination.alias)
        )
    )


def verify_destination(
    session: Session, ctx: CallContext, alias: str, target: str, settings
) -> TelegramDestination:
    alias = alias.strip().lower()
    if not _ALIAS_RE.fullmatch(alias):
        raise ToolError(
            "invalid_input",
            "Destination alias must start with a letter and contain 2-64 lowercase "
            "letters, numbers, _ or -.",
        )
    token = TELEGRAM_API_KEY.get_credentials(session, ctx, settings)
    profile = telegram_request(settings, token, "getMe")
    bot_id = str(profile["result"]["id"])
    chat_response = telegram_request(settings, token, "getChat", json={"chat_id": target.strip()})
    chat = chat_response.get("result", {})
    if chat.get("type") != "channel":
        raise ToolError("invalid_input", "Only Telegram channels are supported.")
    member_response = telegram_request(
        settings,
        token,
        "getChatMember",
        json={"chat_id": chat["id"], "user_id": bot_id},
    )
    member = member_response.get("result", {})
    can_post = member.get("status") == "creator" or (
        member.get("status") == "administrator" and member.get("can_post_messages") is True
    )
    if not can_post:
        raise ToolError(
            "forbidden", "The bot must be a channel administrator with permission to post messages."
        )

    conn = get_connection(session, ctx.user_id)
    if conn is None:
        raise ToolError("not_connected", "Connect Telegram in Settings before adding a channel.")
    existing = session.scalar(
        select(TelegramDestination).where(
            TelegramDestination.connection_id == conn.id,
            TelegramDestination.alias == alias,
        )
    )
    if existing is None:
        existing = TelegramDestination(connection_id=conn.id, alias=alias)
        session.add(existing)
    existing.chat_id = str(chat["id"])
    existing.title = str(chat.get("title") or alias)
    existing.username = chat.get("username")
    existing.enabled = True
    existing.verified_at = datetime.now(UTC)
    session.flush()
    return existing


def remove_destination(session: Session, user_id: uuid.UUID, alias: str) -> bool:
    conn = get_connection(session, user_id)
    if conn is None:
        return False
    destination = session.scalar(
        select(TelegramDestination).where(
            TelegramDestination.connection_id == conn.id,
            TelegramDestination.alias == alias.strip().lower(),
        )
    )
    if destination is None:
        return False
    session.delete(destination)
    session.flush()
    return True


def execution_key(ctx: CallContext, destination_id: uuid.UUID, text: str) -> tuple[str, str]:
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    execution_id = ctx.message_id or ctx.automation_id or ctx.chat_id or ctx.request_id
    raw = f"{ctx.external_user_id}\0{execution_id}\0{destination_id}\0{content_hash}"
    return hashlib.sha256(raw.encode()).hexdigest(), content_hash
