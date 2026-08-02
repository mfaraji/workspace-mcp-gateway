"""Apex per-user API key intake tool.

Apex has no OAuth of its own, so a user connects by pasting a personal API
key rather than following a redirect flow — see
:class:`~gateway.connectors.upstream.ApiKeyStrategy`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gateway.connectors.apikey import store_api_key
from gateway.providers.apex.client import PROVIDER
from gateway.providers.base import CallContext, RiskLevel, ToolSpec


class SetApiKeyInput(BaseModel):
    api_key: str = Field(
        ..., min_length=1, description="Apex workspace API key (starts with 'pk_live_')."
    )


def set_api_key(args: SetApiKeyInput, ctx: CallContext, session: Session) -> dict:
    store_api_key(session, ctx.user_id, PROVIDER, args.api_key)
    return {"status": "connected", "provider": PROVIDER}


def register(registry) -> None:
    """Register the Apex API key intake tool with the tool registry."""
    registry.add(
        ToolSpec(
            name="apex_set_api_key",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "Store your personal Apex workspace API key so apex_* tools act as you. "
                "Get a key from your Apex workspace admin. The key is encrypted at rest and "
                "never returned by any tool."
            ),
            input_model=SetApiKeyInput,
            handler=set_api_key,
        )
    )
