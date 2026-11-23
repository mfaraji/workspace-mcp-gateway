"""Interactive OAuth login and CSRF-protected, owner-scoped grant management."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, Field
from sqlalchemy import select

from gateway.access.policy import (
    grant_for,
    is_shared,
    permitted_catalog,
    personal_tool_names,
    tier_overrides,
)
from gateway.config import get_settings
from gateway.connectors.registry import connector_slugs_for
from gateway.db.engine import session_scope
from gateway.db.models import ClientGrant, User, UserToolSetting
from gateway.identity.models import AuthenticatedUser, IdentityError
from gateway.identity.resolver import _resolve_oauth, get_or_create_user
from gateway.oauth.clerk import authorization_endpoint, issuer_url, token_endpoint

router = APIRouter()


def browser_user(request: Request) -> AuthenticatedUser:
    data = request.scope.get("session", {}).get("identity", {})
    if data.get("expires", 0) < time.time() or data.get("issuer") != issuer_url(get_settings()):
        raise IdentityError("Browser sign-in required")
    return AuthenticatedUser(
        external_user_id=data["external"],
        source="oauth",
        clerk_subject=data["sub"],
        oauth_client_id=data["client"],
    )


def request_user(request: Request) -> AuthenticatedUser:
    """Private APIs accept OAuth; cookie-authenticated writes require CSRF."""
    auth = browser_user(request)
    if request.method not in {"GET", "HEAD"}:
        csrf(request)
    return auth


def csrf(request: Request) -> None:
    expected = request.session.get("csrf", "")
    if not expected or not hmac.compare_digest(request.headers.get("x-csrf-token", ""), expected):
        raise HTTPException(403, "Invalid CSRF token")


@router.get("/access")
def page(request: Request, ticket: str = ""):
    settings = get_settings()
    if ticket:
        try:
            target = URLSafeTimedSerializer(settings.session_secret, salt="client-setup").loads(
                ticket, max_age=3600
            )
            if not isinstance(target, dict) or not target.get("sub") or not target.get("client"):
                raise ValueError
            if "resource" in target:
                resource_connector(target["resource"])
        except (BadSignature, ValueError):
            raise HTTPException(
                400, "Invalid or old setup link. Reopen a fresh link from the MCP client."
            ) from None
        request.session["target"] = target
        return RedirectResponse("/access", status_code=303)
    try:
        auth = browser_user(request)
    except IdentityError:
        return RedirectResponse("/access/login", status_code=303)
    target = request.session.get("target")
    if target and target.get("sub") != auth.clerk_subject:
        raise HTTPException(403, "Sign in with the same account as the MCP client")
    # Render the styled recovery message for browser sessions with a pre-scope link.
    if not target or target.get("resource"):
        scoped_catalog(request, auth)
    request.session.setdefault("csrf", secrets.token_urlsafe(32))
    return HTMLResponse(
        Path(__file__).with_name("page.html").read_text(),
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'"
            ),
        },
    )


@router.get("/access/login")
def login(request: Request):
    settings = get_settings()
    if not settings.clerk_browser_client_id or not settings.clerk_browser_client_secret:
        raise HTTPException(503, "Browser OAuth client is not configured")
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    request.session["login"] = {"state": state, "verifier": verifier, "expires": time.time() + 600}
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.clerk_browser_client_id,
            "redirect_uri": f"{settings.base_url.rstrip('/')}/access/callback",
            "scope": "openid profile mcp:tools",
            "resource": f"{settings.base_url.rstrip('/')}/mcp",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return RedirectResponse(f"{authorization_endpoint(settings)}?{query}", status_code=303)


@router.get("/access/callback")
def callback(request: Request, state: str = "", code: str = ""):
    intent = request.session.pop("login", {})
    if (
        not state
        or not code
        or intent.get("expires", 0) < time.time()
        or not hmac.compare_digest(state, intent.get("state", ""))
    ):
        raise HTTPException(400, "Invalid OAuth state")
    settings = get_settings()
    resource = f"{settings.base_url.rstrip('/')}/mcp"
    try:
        response = httpx.post(
            token_endpoint(settings),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{settings.base_url.rstrip('/')}/access/callback",
                "client_id": settings.clerk_browser_client_id,
                "code_verifier": intent["verifier"],
                "resource": resource,
            },
            auth=(settings.clerk_browser_client_id, settings.clerk_browser_client_secret),
            timeout=10,
        )
        response.raise_for_status()
        with session_scope() as session:
            auth = _resolve_oauth(response.json()["access_token"], settings, resource, session)
            get_or_create_user(session, auth)
        if auth.oauth_client_id != settings.clerk_browser_client_id:
            raise IdentityError("Unexpected OAuth client")
    except (httpx.HTTPError, ValueError, KeyError, IdentityError):
        raise HTTPException(400, "OAuth login failed") from None
    target = request.session.get("target")
    return_to = request.session.get("return_to", "/access")
    request.session.clear()
    if target:
        request.session["target"] = target
    request.session["identity"] = {
        "issuer": issuer_url(settings),
        "sub": auth.clerk_subject,
        "client": auth.oauth_client_id,
        "external": auth.external_user_id,
        "expires": time.time() + 3600,
    }
    request.session["csrf"] = secrets.token_urlsafe(32)
    return RedirectResponse(return_to, status_code=303)


@router.get("/access/page.css")
def stylesheet():
    from starlette.responses import Response

    return Response(
        Path(__file__).with_name("page.css").read_text(),
        media_type="text/css",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/access/page.js")
def script():
    from starlette.responses import Response

    return Response(
        Path(__file__).with_name("page.js").read_text(),
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )


def owner(request: Request) -> AuthenticatedUser:
    try:
        return browser_user(request)
    except IdentityError:
        raise HTTPException(401, "Browser sign-in required") from None


def resource_connector(resource: str | None) -> str | None:
    """Accept only an exact gateway resource signed into the setup ticket."""
    root = f"{get_settings().base_url.rstrip('/')}/mcp"
    if resource == root:
        return None
    if isinstance(resource, str) and resource.startswith(root + "/"):
        slug = resource[len(root) + 1 :]
        if connector_slugs_for(slug) is not None:
            return slug
    raise ValueError("Reopen a fresh permissions link from the MCP client")


def catalog_for_resource(catalog: list[dict], slug: str | None) -> list[dict]:
    """Catalog entries served at one resource: every connector, one, or a bundle's members."""
    if slug is None:
        return catalog
    members = connector_slugs_for(slug) or set()
    return [c for c in catalog if c["slug"] in members]


def scoped_catalog(request: Request, auth: AuthenticatedUser, client_id: str | None = None):
    target = request.session.get("target")
    catalog = permitted_catalog(
        get_settings(),
        auth.clerk_subject,
        request.app.state.access_catalog,
        request.app.state.access_specs,
    )
    if not target:
        return catalog
    if target.get("sub") != auth.clerk_subject:
        raise HTTPException(403, "Sign in with the same account as the MCP client")
    if client_id is not None and target.get("client") != client_id:
        raise HTTPException(403, "This setup link belongs to another client")
    try:
        slug = resource_connector(target.get("resource"))
    except ValueError:
        raise HTTPException(400, "Reopen a fresh permissions link from the MCP client") from None
    return catalog_for_resource(catalog, slug)


@router.get("/api/access")
def catalog(request: Request):
    auth = owner(request)
    settings = get_settings()
    visible = scoped_catalog(request, auth)
    names = personal_tool_names(visible)
    target = request.session.get("target")
    with session_scope() as session:
        user = get_or_create_user(session, auth)
        grants = session.scalars(
            select(ClientGrant).where(
                ClientGrant.user_id == user.id, ClientGrant.issuer == issuer_url(settings)
            )
        ).all()
        entries = [
            {
                "client_id": g.client_id,
                "tools": [t for t in g.tools if t in names],
                "revoked": g.revoked,
            }
            for g in grants
            if not target or g.client_id == target["client"]
        ]
        risk = {
            name: tier.value
            for name, tier in tier_overrides(session, user.id).items()
            if name in names
        }
    return {
        "csrf": request.session["csrf"],
        "target": target["client"] if target else None,
        "grants": entries,
        "risk": risk,
        "connectors": visible,
        "scoped": bool(target and resource_connector(target.get("resource"))),
    }


class GrantBody(BaseModel):
    tools: list[str] = Field(max_length=1000)


@router.put("/api/access/{client_id}")
def save(client_id: str, body: GrantBody, request: Request):
    auth = owner(request)
    csrf(request)
    settings = get_settings()
    known = personal_tool_names(scoped_catalog(request, auth, client_id))
    if not known:
        raise HTTPException(400, "Shared access is managed through OAuth authorization")
    if not set(body.tools) <= known:
        raise HTTPException(400, "Unknown tool")
    with session_scope() as session:
        user = get_or_create_user(session, auth)
        # Serialize concurrent saves of the same owner's grants.
        from gateway.db.models import User

        session.scalar(select(User).where(User.id == user.id).with_for_update())
        grant = grant_for(session, user.id, settings, client_id)
        target = request.session.get("target", {})
        if grant is None:
            if target.get("client") != client_id or target.get("sub") != auth.clerk_subject:
                raise HTTPException(403, "Open the setup link from this client first")
            grant = ClientGrant(user_id=user.id, issuer=issuer_url(settings), client_id=client_id)
            session.add(grant)
        # A product-specific edit must not erase another connector's grant.
        previous = set(grant.tools or []) if not grant.revoked else set()
        grant.tools, grant.revoked = sorted((previous - known) | set(body.tools)), False
    return {"saved": True}


class TierBody(BaseModel):
    tool: str = Field(max_length=128)
    tier: Literal["low", "medium"]


@router.put("/api/tool-risk")
def set_tool_risk(body: TierBody, request: Request):
    """Set how one mutating tool is gated for this user (all OAuth clients).

    ``low`` runs directly; ``medium`` makes the model ask the user in chat first.
    Setting a tool back to its default removes the override.
    """
    auth = owner(request)
    csrf(request)
    spec = request.app.state.access_specs.get(body.tool)
    if spec is None or spec.default_tier is None or is_shared(spec):
        raise HTTPException(400, "Unknown tool")
    with session_scope() as session:
        user = get_or_create_user(session, auth)
        session.scalar(select(User).where(User.id == user.id).with_for_update())
        row = session.scalar(
            select(UserToolSetting).where(
                UserToolSetting.user_id == user.id, UserToolSetting.tool_name == body.tool
            )
        )
        if body.tier == spec.default_tier.value:
            if row is not None:
                session.delete(row)
        elif row is None:
            session.add(UserToolSetting(user_id=user.id, tool_name=body.tool, tier=body.tier))
        else:
            row.tier = body.tier
    return {"saved": True, "tool": body.tool, "tier": body.tier}


@router.delete("/api/access/{client_id}")
def revoke(client_id: str, request: Request):
    auth = owner(request)
    csrf(request)
    known = personal_tool_names(scoped_catalog(request, auth, client_id))
    if not known:
        raise HTTPException(400, "Shared access is managed through OAuth authorization")
    with session_scope() as session:
        user = get_or_create_user(session, auth)
        from gateway.db.models import User

        session.scalar(select(User).where(User.id == user.id).with_for_update())
        grant = grant_for(session, user.id, get_settings(), client_id)
        if grant is None:
            raise HTTPException(404, "Grant not found")
        remaining = set(grant.tools or []) - known if not grant.revoked else set()
        grant.tools, grant.revoked = sorted(remaining), not remaining
    return {"revoked": True}
