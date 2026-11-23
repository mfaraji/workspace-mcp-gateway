"""Shared discovery and execution authorization policy."""

from __future__ import annotations

from uuid import UUID

from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from gateway.config import Settings
from gateway.connectors.base import AccessMode
from gateway.connectors.registry import by_spec
from gateway.db.models import ClientGrant, UserToolSetting
from gateway.identity.models import AuthenticatedUser
from gateway.oauth.clerk import issuer_url
from gateway.providers.base import RiskTier, ToolSpec


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


def tier_overrides(session: Session, user_id: UUID) -> dict[str, RiskTier]:
    """The user's stored low/medium overrides, keyed by tool name (all OAuth clients)."""
    rows = session.execute(
        select(UserToolSetting.tool_name, UserToolSetting.tier).where(
            UserToolSetting.user_id == user_id
        )
    )
    out: dict[str, RiskTier] = {}
    for name, tier in rows:
        try:
            out[name] = RiskTier(tier)
        except ValueError:  # unknown stored value: ignore, fall back to the default
            continue
    return out


def effective_tier(session: Session, user_id: UUID, spec: ToolSpec) -> RiskTier | None:
    """The tier gating ``spec`` for this user: their override, else the tool default.

    Read tools have no tier (``None``).
    """
    default = spec.default_tier
    if default is None:
        return None
    row = session.scalar(
        select(UserToolSetting.tier).where(
            UserToolSetting.user_id == user_id, UserToolSetting.tool_name == spec.name
        )
    )
    try:
        return RiskTier(row) if row else default
    except ValueError:
        return default


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


def upstream_permits(settings: Settings, auth: AuthenticatedUser, spec: ToolSpec) -> bool:
    """Whether the connector's upstream (e.g. Apex) permits this caller to use ``spec``.

    Connectors without a ``permits`` hook are unrestricted here; the client grant still
    applies. For Apex this is the user's workspaces + capabilities, so a granted tool
    that Apex would refuse is hidden from discovery and forbidden at call time.
    """
    connector = by_spec(spec)
    permits = getattr(getattr(connector, "upstream", None), "permits", None)
    if permits is None:
        return True
    return bool(permits(settings, auth.clerk_subject, auth.oauth_client_id, spec))


def subject_permits(settings: Settings, subject: str | None, spec: ToolSpec) -> bool:
    """Like :func:`upstream_permits` for browser flows that only know the Clerk subject."""
    connector = by_spec(spec)
    permits = getattr(getattr(connector, "upstream", None), "permits", None)
    if permits is None:
        return True
    return bool(permits(settings, subject, None, spec))


def permitted_catalog(
    settings: Settings,
    subject: str | None,
    catalog: list[dict],
    specs: dict[str, ToolSpec],
) -> list[dict]:
    """Drop tools the upstream (Apex) does not permit this Clerk subject to use."""
    return [
        {
            **c,
            "tools": [
                t
                for t in c["tools"]
                if t["name"] not in specs or subject_permits(settings, subject, specs[t["name"]])
            ],
        }
        for c in catalog
    ]


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
        if not upstream_permits(settings, auth, spec):
            raise ToolError("forbidden", "Apex does not permit this tool for your account")
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
