"""Helpers shared by all Google product tools: account selection and account listing."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gateway.providers.base import CallContext, RiskLevel, ToolSpec
from gateway.providers.google.connections import list_connections


class GoogleAccountInput(BaseModel):
    """Base for every Google tool input: optionally pick which connected account to use."""

    account: str | None = Field(
        None,
        description=(
            "Email of the connected Google account to use for this product. Omit to use "
            "the default account; call the product's list_accounts tool to see them."
        ),
    )


class ListAccountsInput(BaseModel):
    """No parameters: lists the accounts connected to this product."""


def make_list_accounts_spec(
    *, name: str, provider: str, product: str, display_name: str
) -> ToolSpec:
    """Build the read-only ``*_list_accounts`` tool for one Google product."""

    def handler(_: ListAccountsInput, ctx: CallContext, session: Session) -> dict[str, Any]:
        rows = list_connections(session, ctx.user_id, provider, product)
        return {
            "accounts": [
                {
                    "email": c.provider_email,
                    "is_default": bool(c.is_default and c.status == "active"),
                    "status": "connected" if c.status == "active" else "reauth_required",
                }
                for c in rows
            ]
        }

    return ToolSpec(
        name=name,
        provider=provider,
        risk=RiskLevel.READ,
        description=(
            f"List the Google accounts connected to {display_name}. Pass one account's "
            "email as `account` on other tools to use it; omit `account` for the default."
        ),
        input_model=ListAccountsInput,
        handler=handler,
    )
