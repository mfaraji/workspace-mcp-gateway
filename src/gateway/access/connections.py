"""Browser page listing every connector and whether the signed-in user has connected it."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from gateway.access.routes import browser_user, owner
from gateway.config import Settings, get_settings
from gateway.connectors.apikey import get_api_key_connection
from gateway.connectors.base import AccessMode, Bundle, Connector
from gateway.connectors.registry import BUNDLES, CONNECTORS, bundle_members
from gateway.connectors.upstream import ApiKeyStrategy, OAuthStrategy
from gateway.db.engine import session_scope
from gateway.identity.models import AuthenticatedUser, IdentityError
from gateway.identity.resolver import get_or_create_user
from gateway.providers.apex.client import (
    WEALTH_PROVIDER,
    ApexUpstream,
    is_wealth_workspace,
    user_workspaces,
)
from gateway.providers.google.connections import list_connections
from gateway.providers.telegram.service import get_connection as get_telegram_connection

router = APIRouter()
_DIR = Path(__file__).parent


def oauth_status(upstream: OAuthStrategy, session, user_id) -> dict:
    """Accounts connected to one OAuth product and the status derived from them."""
    accounts = [
        {
            "id": str(c.id),
            "email": c.provider_email,
            "is_default": bool(c.is_default and c.status == "active"),
            "status": (
                "connected"
                if c.status == "active"
                and not set(upstream.required_scopes) - set(c.scopes or [])
                else "reauth_required"
            ),
            "last_used_at": c.last_used_at.isoformat() if c.last_used_at else None,
        }
        for c in list_connections(session, user_id, upstream.upstream_provider, upstream.product)
    ]
    if not accounts:
        return {"status": "not_connected", "account": None, "action": "oauth", "accounts": []}
    default = next((a for a in accounts if a["is_default"]), accounts[0])
    usable = any(a["status"] == "connected" for a in accounts)
    return {
        "status": "connected" if usable else "reauth_required",
        "account": default["email"],
        "action": "oauth",
        "accounts": accounts,
    }


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
        return oauth_status(upstream, session, user_id)

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

    if isinstance(upstream, ApexUpstream):
        # Nothing is stored: signing in is the connection. Show what Apex reports.
        workspaces = user_workspaces(settings, auth.clerk_subject, auth.oauth_client_id)
        if workspaces is None:
            return {"status": "not_connected", "account": "No Apex account", "action": None}
        if upstream.upstream_provider == WEALTH_PROVIDER:
            workspaces = [w for w in workspaces if is_wealth_workspace(w)]
        count = len(workspaces)
        return {
            "status": "connected",
            "account": f"{count} workspace{'s' if count != 1 else ''}",
            "action": None,
        }

    if isinstance(upstream, ApiKeyStrategy):
        if get_api_key_connection(session, user_id, upstream.upstream_provider) is not None:
            return {"status": "connected", "account": "API key", "action": "api_key"}
        return {"status": "not_connected", "account": None, "action": "api_key"}

    return base


def bundle_status(bundle: Bundle, session, user_id) -> dict:
    """Status of a bundle: connected if any member product has a usable account."""
    members = [c for c in bundle_members(bundle) if isinstance(c.upstream, OAuthStrategy)]
    statuses = [oauth_status(c.upstream, session, user_id) for c in members]
    emails = sorted({a["email"] for s in statuses for a in s["accounts"] if a["email"]})
    if not any(s["accounts"] for s in statuses):
        return {"status": "not_connected", "account": None, "action": "oauth", "accounts": []}
    all_ok = all(s["status"] == "connected" for s in statuses)
    return {
        "status": "connected" if all_ok else "reauth_required",
        "account": ", ".join(emails) or None,
        "action": "oauth",
        "accounts": [],
    }


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
        # Overarching endpoints that serve several connectors' tools in one registration.
        items += [
            {
                "slug": b.slug,
                "name": f"{b.display_name} (all tools)",
                "provider": b.upstream_provider,
                "scope": "user",
                "bundle": True,
                "oauth_product": "all",
                "mcp_url": f"{root}/mcp/{b.slug}",
                "tool_count": sum(tool_counts.get(c.slug, 0) for c in bundle_members(b)),
                **bundle_status(b, session, user.id),
            }
            for b in BUNDLES
        ]
    # One-time message queued by the OAuth callback (e.g. Gmail label instructions).
    return {
        "csrf": request.session["csrf"],
        "connectors": items,
        "notice": request.session.pop("notice", None),
    }
