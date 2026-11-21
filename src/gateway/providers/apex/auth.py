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


def _list(ctx: CallContext, *, wealth_only: bool) -> list[dict]:
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
                "List the Apex workspaces you belong to (id, name, kind, capabilities). "
                "Pass the id as workspace_id to every other apex_* tool. Invoicing, "
                "timesheet and client tools need a BUSINESS workspace."
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
                "List the Apex workspaces that hold wealth data (PERSONAL workspaces). "
                "Pass the id as workspace_id to every other apex_wealth_* tool."
            ),
            input_model=NoInput,
            handler=list_wealth_workspaces,
        )
    )
