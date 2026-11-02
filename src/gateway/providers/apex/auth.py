"""Apex connection tools: per-user API key intake and act-as workspace selection.

Apex has no OAuth of its own, so a user connects either by pasting a personal
API key (see :class:`~gateway.connectors.upstream.ApiKeyStrategy`) or, when the
gateway has an Apex system key and the caller is a Clerk user, by acting as
that user in a chosen default workspace (see
:class:`~gateway.providers.apex.client.ApexAuthStrategy`).
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gateway.config import get_settings
from gateway.connectors.apikey import set_default_workspace, store_api_key
from gateway.providers.apex.client import (
    APEX_API_KEY,
    APEX_WEALTH_API_KEY,
    PROVIDER,
    WEALTH_PROVIDER,
    ApexAuthStrategy,
    apex_call,
)
from gateway.providers.base import CallContext, RiskLevel, ToolSpec


class SetApiKeyInput(BaseModel):
    api_key: str = Field(
        ..., min_length=1, description="Apex workspace API key (starts with 'pk_live_')."
    )


class NoInput(BaseModel):
    pass


class SetWorkspaceInput(BaseModel):
    workspace_id: str = Field(
        ..., min_length=1, description="Workspace id from the list_workspaces tool."
    )


def _is_wealth_workspace(workspace: dict) -> bool:
    capabilities = [str(c).lower() for c in workspace.get("capabilities") or []]
    return workspace.get("kind") == "PERSONAL" or any("wealth" in c for c in capabilities)


def _fetch_workspaces(
    strategy: ApexAuthStrategy, ctx: CallContext, session: Session, *, wealth_only: bool
) -> list[dict]:
    """GET /api/workspaces without a workspace header (so it works before one is set)."""
    from gateway.providers.registry import ToolError

    settings = get_settings()
    creds = strategy.get_credentials(session, ctx, settings, with_workspace=False)
    data = apex_call(settings, creds, "GET", "/api/workspaces")
    if not isinstance(data, list):
        raise ToolError("provider_error", "apex returned an unexpected workspaces response")
    workspaces = [
        {
            "id": w.get("id"),
            "name": w.get("name"),
            "kind": w.get("kind"),
            "capabilities": w.get("capabilities") or [],
        }
        for w in data
        if isinstance(w, dict)
    ]
    if wealth_only:
        workspaces = [w for w in workspaces if _is_wealth_workspace(w)]
    return workspaces


def _set_workspace(
    strategy: ApexAuthStrategy,
    provider: str,
    args: SetWorkspaceInput,
    ctx: CallContext,
    session: Session,
    *,
    wealth_only: bool,
) -> dict:
    from gateway.providers.registry import ToolError

    workspaces = _fetch_workspaces(strategy, ctx, session, wealth_only=wealth_only)
    chosen = next((w for w in workspaces if w["id"] == args.workspace_id), None)
    if chosen is None:
        raise ToolError(
            "invalid_input",
            f"workspace_id is not one of your available workspaces; call "
            f"{strategy.list_workspaces_tool} for valid ids.",
        )
    set_default_workspace(session, ctx.user_id, provider, chosen["id"], chosen["name"] or "")
    return {
        "status": "workspace_set",
        "workspace_id": chosen["id"],
        "name": chosen["name"],
        "kind": chosen["kind"],
    }


def list_workspaces(_args: NoInput, ctx: CallContext, session: Session) -> list[dict]:
    return _fetch_workspaces(APEX_API_KEY, ctx, session, wealth_only=False)


def set_workspace(args: SetWorkspaceInput, ctx: CallContext, session: Session) -> dict:
    return _set_workspace(APEX_API_KEY, PROVIDER, args, ctx, session, wealth_only=False)


def list_wealth_workspaces(_args: NoInput, ctx: CallContext, session: Session) -> list[dict]:
    return _fetch_workspaces(APEX_WEALTH_API_KEY, ctx, session, wealth_only=True)


def set_wealth_workspace(args: SetWorkspaceInput, ctx: CallContext, session: Session) -> dict:
    return _set_workspace(
        APEX_WEALTH_API_KEY, WEALTH_PROVIDER, args, ctx, session, wealth_only=True
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
    registry.add(
        ToolSpec(
            name="apex_list_workspaces",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "List the Apex workspaces you belong to (id, name, kind, capabilities). "
                "Works without a default workspace; use it to pick one for apex_set_workspace."
            ),
            input_model=NoInput,
            handler=list_workspaces,
        )
    )
    registry.add(
        ToolSpec(
            name="apex_set_workspace",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "Choose the default Apex workspace that apex_* tools act in when you are "
                "signed in with Clerk (no API key). workspace_id must come from "
                "apex_list_workspaces."
            ),
            input_model=SetWorkspaceInput,
            handler=set_workspace,
        )
    )


def set_wealth_api_key(args: SetApiKeyInput, ctx: CallContext, session: Session) -> dict:
    store_api_key(session, ctx.user_id, WEALTH_PROVIDER, args.api_key)
    return {"status": "connected", "provider": WEALTH_PROVIDER}


def register_wealth(registry) -> None:
    """Register the Apex Wealth API key intake tool with the tool registry."""
    registry.add(
        ToolSpec(
            name="apex_wealth_set_api_key",
            provider=WEALTH_PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "Store your personal Apex API key for a PERSONAL workspace so apex_wealth_* "
                "tools act as you. This is separate from the invoicing key set by "
                "apex_set_api_key. The key is encrypted at rest and never returned by any tool."
            ),
            input_model=SetApiKeyInput,
            handler=set_wealth_api_key,
        )
    )
    registry.add(
        ToolSpec(
            name="apex_wealth_list_workspaces",
            provider=WEALTH_PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "List your Apex workspaces that hold wealth data (PERSONAL or with the "
                "wealth capability). Use it to pick one for apex_wealth_set_workspace."
            ),
            input_model=NoInput,
            handler=list_wealth_workspaces,
        )
    )
    registry.add(
        ToolSpec(
            name="apex_wealth_set_workspace",
            provider=WEALTH_PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "Choose the default Apex workspace that apex_wealth_* tools act in when you "
                "are signed in with Clerk (no API key). workspace_id must come from "
                "apex_wealth_list_workspaces. Separate from apex_set_workspace."
            ),
            input_model=SetWorkspaceInput,
            handler=set_wealth_workspace,
        )
    )
