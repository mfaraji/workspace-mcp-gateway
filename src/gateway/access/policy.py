"""Shared discovery and execution authorization policy."""

from __future__ import annotations

from uuid import UUID

from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from gateway.config import Settings
from gateway.connectors.base import AccessMode
from gateway.connectors.registry import by_spec
from gateway.db.models import ClientGrant
from gateway.identity.models import AuthenticatedUser
from gateway.oauth.clerk import issuer_url
from gateway.providers.base import ToolSpec


def setup_url(settings: Settings, auth: AuthenticatedUser) -> str:
    """Bind the setup link to its OAuth user, client and connector resource."""
    payload = {
        "sub": auth.clerk_subject,
        "client": auth.oauth_client_id,
        "resource": auth.oauth_resource or f"{settings.base_url.rstrip('/')}/mcp",
    }
    ticket = URLSafeTimedSerializer(settings.session_secret, salt="client-setup").dumps(payload)
    return f"{settings.base_url.rstrip('/')}/access?ticket={ticket}"


def grant_for(
    session: Session, user_id: UUID, settings: Settings, client_id: str | None
) -> ClientGrant | None:
    return session.scalar(
        select(ClientGrant).where(
            ClientGrant.user_id == user_id,
            ClientGrant.issuer == issuer_url(settings),
            ClientGrant.client_id == client_id,
        )
    )


def allowed_tools(
    session: Session, user_id: UUID, settings: Settings, auth: AuthenticatedUser
) -> set[str]:
    if auth.source != "oauth" or not auth.oauth_client_id:
        return set()
    grant = grant_for(session, user_id, settings, auth.oauth_client_id)
    return set(grant.tools) if grant and not grant.revoked else set()


def is_shared(spec: ToolSpec) -> bool:
    """Never infer shared access from missing upstream credentials."""
    connector = by_spec(spec)
    return connector is not None and connector.access_mode is AccessMode.AUTHENTICATED_SHARED


def tool_allowed(spec: ToolSpec, auth: AuthenticatedUser, granted: set[str]) -> bool:
    """Common decision after OAuth middleware validates token scope and audience."""
    if spec.name == "system_get_current_time":
        return True
    if auth.source != "oauth" or not auth.oauth_client_id:
        return False
    return is_shared(spec) or spec.name in granted


def personal_tool_names(catalog: list[dict]) -> set[str]:
    """Only personal tools can be selected or revoked through grant APIs."""
    return {
        t["name"]
        for c in catalog
        if c.get("access_mode", "personal") == AccessMode.PERSONAL
        for t in c["tools"]
    }


def enforce(
    session: Session, user_id: UUID, settings: Settings, auth: AuthenticatedUser, spec: ToolSpec
) -> None:
    from gateway.providers.registry import ToolError

    if tool_allowed(spec, auth, allowed_tools(session, user_id, settings, auth)):
        return
    if auth.source != "oauth" or not auth.oauth_client_id:
        raise ToolError("permission_denied", "OAuth authentication is required")
    grant = grant_for(session, user_id, settings, auth.oauth_client_id)
    if grant is None:
        raise ToolError(
            "access_setup_required", f"Choose client permissions: {setup_url(settings, auth)}"
        )
    if grant.revoked or spec.name not in grant.tools:
        raise ToolError("permission_denied", "This client has no permission to call this tool")
