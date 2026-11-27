"""Apex workspace discovery tools.

There is no key intake or default workspace: the user signs in with Clerk OAuth and
passes ``workspace_id`` on every workspace-scoped call. These tools list the
workspaces Apex says the caller belongs to (id, name, kind, capabilities).
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy.orm import Session

from gateway.config import get_settings
from gateway.providers.apex.client import (
    PROVIDER,
    WEALTH_PROVIDER,
    is_wealth_workspace,
    user_workspaces,
)
from gateway.providers.base import CallContext, RiskLevel, ToolSpec


class NoInput(BaseModel):
    pass


def _list(ctx: CallContext, *, wealth_only: bool = False) -> list[dict]:
    from gateway.providers.registry import ToolError

    workspaces = user_workspaces(get_settings(), ctx.clerk_subject, ctx.oauth_client_id)
    if workspaces is None:
        raise ToolError("forbidden", "Apex does not recognize your account or is unreachable")
    if wealth_only:
        workspaces = [w for w in workspaces if is_wealth_workspace(w)]
    return workspaces


def list_workspaces(_args: NoInput, ctx: CallContext, session: Session) -> list[dict]:
    return _list(ctx, wealth_only=False)


def list_wealth_workspaces(_args: NoInput, ctx: CallContext, session: Session) -> list[dict]:
    return _list(ctx, wealth_only=True)


def register(registry) -> None:
    """Register the Apex workspace listing tool with the tool registry."""
    registry.add(
        ToolSpec(
            name="apex_list_workspaces",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "Apex Finance workspaces: list the Apex workspaces you belong to (id, name, "
                "kind PERSONAL, BUSINESS or RENTAL, capabilities). Pass the id as workspace_id "
                "to every other Apex Finance tool (apex_*, including apex_transactions_*). "
                "Invoicing, timesheet and client tools need a BUSINESS workspace; receipts "
                "and expenses need one with receipt review. Spending questions work on any "
                "kind: personal spending is PERSONAL, company expenses are BUSINESS, rental "
                "income and property costs are RENTAL. Pick the workspace the request is "
                "about; if it is unclear, ask the user rather than guessing."
            ),
            input_model=NoInput,
            handler=list_workspaces,
        )
    )


def register_wealth(registry) -> None:
    """Register the Apex Wealth workspace listing tool with the tool registry."""
    registry.add(
        ToolSpec(
            name="apex_wealth_list_workspaces",
            provider=WEALTH_PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "Personal wealth: list the Apex workspaces that hold wealth data (PERSONAL "
                "workspaces). Pass the id as workspace_id to every other apex_wealth_* tool. "
                "If more than one fits and it is unclear which the user means, ask them "
                "rather than guessing."
            ),
            input_model=NoInput,
            handler=list_wealth_workspaces,
        )
    )

