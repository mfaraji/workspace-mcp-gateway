"""Browser page listing every connector and whether the signed-in user has connected it."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from gateway.access.routes import browser_user, owner
from gateway.config import Settings, get_settings
from gateway.connectors.apikey import get_api_key_connection, get_default_workspace
from gateway.connectors.base import AccessMode, Connector
from gateway.connectors.registry import CONNECTORS
from gateway.connectors.upstream import ApiKeyStrategy, OAuthStrategy
from gateway.db.engine import session_scope
from gateway.identity.models import AuthenticatedUser, IdentityError
from gateway.identity.resolver import get_or_create_user
from gateway.providers.google.connections import get_active_connection
from gateway.providers.telegram.service import get_connection as get_telegram_connection

router = APIRouter()
_DIR = Path(__file__).parent


def connection_status(
    connector: Connector, session, user_id, auth: AuthenticatedUser, settings: Settings
) -> dict:
    """Status/account/action for one connector. Never includes tokens or keys."""
    upstream = connector.upstream
    base = {"status": "available", "account": None, "action": None}

    if connector.access_mode is AccessMode.AUTHENTICATED_SHARED:
        return base
    if connector.slug == "recipes":
        base["status"] = "available" if settings.capella_configured else "not_configured"
        return base

    if isinstance(upstream, OAuthStrategy):
        conn = get_active_connection(session, user_id, upstream.upstream_provider)
        if conn is None:
            return {"status": "not_connected", "account": None, "action": "oauth"}
        missing = set(upstream.required_scopes) - set(conn.scopes or [])
        return {
            "status": "reauth_required" if missing else "connected",
            "account": conn.provider_email,
            "action": "oauth",
        }

    if connector.slug == "telegram":
        conn = get_telegram_connection(session, user_id)
        if conn is None:
            return {"status": "not_connected", "account": None, "action": "bot_token"}
        username = (conn.connection_metadata or {}).get("username")
        return {
            "status": "connected",
            "account": f"@{username}" if username else None,
            "action": "bot_token",
        }

    if isinstance(upstream, ApiKeyStrategy):
        provider = upstream.upstream_provider
        if get_api_key_connection(session, user_id, provider) is not None:
            return {"status": "connected", "account": "API key", "action": "api_key"}
        if settings.apex_gateway_api_key and auth.clerk_subject:
            workspace = get_default_workspace(session, user_id, provider)
            if workspace is None:
                return {"status": "workspace_not_set", "account": None, "action": "api_key"}
            return {
                "status": "connected",
                "account": workspace["workspace_name"] or workspace["workspace_id"],
                "action": "api_key",
            }
        return {"status": "not_connected", "account": None, "action": "api_key"}

    return base


@router.get("/connections")
def page(request: Request):
    try:
        browser_user(request)
    except IdentityError:
        request.session["return_to"] = "/connections"
        return RedirectResponse("/access/login", status_code=303)
    return HTMLResponse(
        (_DIR / "connections.html").read_text(),
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'"
            ),
        },
    )


@router.get("/connections/page.js")
def script():
    return Response(
        (_DIR / "connections.js").read_text(),
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/api/connections")
def connections(request: Request):
    auth = owner(request)
    settings = get_settings()
    if not request.session.get("csrf"):
        raise HTTPException(401, "Browser sign-in required")
    tool_counts = {c["slug"]: len(c["tools"]) for c in request.app.state.access_catalog}
    root = settings.base_url.rstrip("/")
    with session_scope() as session:
        user = get_or_create_user(session, auth)
        items = [
            {
                "slug": c.slug,
                "name": c.display_name,
                "provider": c.upstream_provider,
                "scope": "universal"
                if c.access_mode is AccessMode.AUTHENTICATED_SHARED
                else "user",
                "mcp_url": f"{root}/mcp/{c.slug}",
                "tool_count": tool_counts.get(c.slug, 0),
                **connection_status(c, session, user.id, auth, settings),
            }
            for c in CONNECTORS
        ]
    # One-time message queued by the OAuth callback (e.g. Gmail label instructions).
    return {
        "csrf": request.session["csrf"],
        "connectors": items,
        "notice": request.session.pop("notice", None),
    }
