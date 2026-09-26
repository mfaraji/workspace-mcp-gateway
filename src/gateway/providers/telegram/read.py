"""Telegram read tools."""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy.orm import Session

from gateway.providers.base import CallContext, RiskLevel, ToolSpec
from gateway.providers.telegram.client import PROVIDER
from gateway.providers.telegram.service import list_destinations


class ListDestinationsInput(BaseModel):
    pass


def list_verified_destinations(
    _args: ListDestinationsInput, ctx: CallContext, session: Session
) -> dict:
    destinations = list_destinations(session, ctx.user_id)
    return {
        "destinations": [
            {
                "alias": item.alias,
                "title": item.title,
                "username": item.username,
                "enabled": item.enabled,
            }
            for item in destinations
            if item.enabled
        ]
    }


def register(registry) -> None:
    registry.add(
        ToolSpec(
            name="telegram_list_destinations",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "List the verified Telegram channel aliases available to the current user. "
                "Use an alias from this result with telegram_send_message."
            ),
            input_model=ListDestinationsInput,
            handler=list_verified_destinations,
        )
    )
