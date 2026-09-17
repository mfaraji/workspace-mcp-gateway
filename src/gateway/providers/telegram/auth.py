"""Telegram per-user bot token intake tool.

Telegram has no OAuth for bots, so a user connects by pasting a bot token from
@BotFather — see :class:`~gateway.connectors.upstream.ApiKeyStrategy`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gateway.connectors.apikey import store_api_key
from gateway.providers.base import CallContext, RiskLevel, ToolSpec
from gateway.providers.telegram.client import PROVIDER


class SetApiKeyInput(BaseModel):
    api_key: str = Field(
        ...,
        min_length=1,
        description="Bot token from @BotFather (e.g. 123456789:AAH...). Add the bot as an "
        "admin of the target channel with permission to post messages.",
    )


def set_api_key(args: SetApiKeyInput, ctx: CallContext, session: Session) -> dict:
    store_api_key(session, ctx.user_id, PROVIDER, args.api_key)
    return {"status": "connected", "provider": PROVIDER}


def register(registry) -> None:
    """Register the Telegram bot token intake tool with the tool registry."""
    registry.add(
        ToolSpec(
            name="telegram_set_api_key",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "Store your Telegram bot token so telegram_* tools can post as that bot. "
                "Create a bot with @BotFather and add it as an administrator of the channel "
                "you want to message. The token is encrypted at rest and never returned by "
                "any tool."
            ),
            input_model=SetApiKeyInput,
            handler=set_api_key,
        )
    )
