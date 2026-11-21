"""Gateway-hosted tool consent before Clerk completes a client's OAuth flow.

Clerk retains authorization-code, PKCE, redirect and denial handling. Only a
verified interactive Clerk session may save tool choices; MCP access tokens
cannot approve their own permissions.
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, Field
from sqlalchemy import select

from gateway.access.policy import grant_for, permitted_catalog, personal_tool_names
from gateway.access.routes import catalog_for_resource, resource_connector
from gateway.config import Settings, get_settings
from gateway.db.engine import session_scope
from gateway.db.models import ClientGrant, OAuthIdentityLink, User
from gateway.identity.models import AuthenticatedUser, IdentityError
from gateway.identity.resolver import get_or_create_user
from gateway.oauth.clerk import _issuer_endpoint, _jwk_client, issuer_url

router = APIRouter()
_INTENT_SALT = "oauth-tool-consent"


def browser_subject(request: Request, settings: Settings) -> str:
    """Verify an interactive session, including its exact authorized browser origin."""
    origin = settings.base_url.rstrip("/")
    if request.headers.get("origin") != origin:
        raise HTTPException(403, "Invalid consent origin")
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 16384:
        raise HTTPException(401, "Browser sign-in required")
    try:
        key = _jwk_client(_issuer_endpoint(settings, "jwks_uri")).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256", "ES256"],
            issuer=issuer_url(settings),
            audience=settings.clerk_session_audience,
            options={
                "require": ["iss", "sub", "exp", "sid", "azp"],
                "verify_aud": bool(settings.clerk_session_audience),
            },
            leeway=5,
        )
        if (
            claims["azp"] != origin
            or not isinstance(claims["sid"], str)
            or not claims["sid"].startswith("sess_")
            or not isinstance(claims["sub"], str)
            or not claims["sub"]
            or len(claims["sub"]) > 255
            or "client_id" in claims
            or "scope" in claims
        ):
            raise ValueError
    except (jwt.PyJWTError, ValueError, httpx.HTTPError, IdentityError):
        raise HTTPException(401, "Verified browser session required") from None
    return claims["sub"]


def consent_tools(request: Request, resource: str, subject: str | None = None) -> list[dict]:
    try:
        slug = resource_connector(resource)
    except ValueError:
        raise HTTPException(400, "Invalid connector resource") from None
    catalog = permitted_catalog(
        get_settings(),
        subject,
        request.app.state.access_catalog,
        request.app.state.access_specs,
    )
    return catalog_for_resource(catalog, slug)


_CIMD_SCOPES = {"mcp:tools", "offline_access", "openid", "profile", "email"}
_CIMD_MAX_BYTES = 64 * 1024
_CIMD_TTL = 300.0
_cimd_cache: dict[str, tuple[float, dict]] = {}


def _cimd_document(settings: Settings, client_id: str) -> dict:
    """Fetch a client-ID metadata document from an allow-listed https host (no redirects)."""
    parts = urlsplit(client_id)
    allowed = {h.strip().lower() for h in settings.cimd_allowed_hosts.split(",") if h.strip()}
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
        or parts.fragment
        or parts.hostname.lower() not in allowed
    ):
        raise HTTPException(400, "OAuth client is not registered")
    hit = _cimd_cache.get(client_id)
    if hit and time.monotonic() - hit[0] < _CIMD_TTL:
        return hit[1]
    try:
        with httpx.Client(timeout=5, follow_redirects=False) as client:
            response = client.get(client_id, headers={"Accept": "application/json"})
        response.raise_for_status()
        if len(response.content) > _CIMD_MAX_BYTES:
            raise ValueError("metadata document too large")
        document = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "Could not validate the OAuth client") from None
    if not isinstance(document, dict) or document.get("client_id") != client_id:
        raise HTTPException(400, "OAuth client is not registered")
    _cimd_cache[client_id] = (time.monotonic(), document)
    return document


def _redirect_allowed(redirect_uri: str, registered: list) -> bool:
    """Exact match, or for http loopback redirects any port (RFC 8252 section 7.3)."""
    if redirect_uri in registered:
        return True
    want = urlsplit(redirect_uri)
    host = (want.hostname or "").lower()
    if want.scheme != "http" or host not in {"localhost", "127.0.0.1", "::1"}:
        return False
    for entry in registered:
        have = urlsplit(entry) if isinstance(entry, str) else None
        if (
            have
            and have.scheme == "http"
            and (have.hostname or "").lower() == (want.hostname or "").lower()
            and have.path == want.path
            and not have.query
        ):
            return True
    return False


def _registered_cimd_client(
    settings: Settings, client_id: str, redirect_uri: str, scope: str
) -> None:
    document = _cimd_document(settings, client_id)
    requested = set(scope.split())
    if (
        not _redirect_allowed(redirect_uri, document.get("redirect_uris") or [])
        or "mcp:tools" not in requested
        or not requested <= _CIMD_SCOPES
    ):
        raise HTTPException(400, "Invalid OAuth consent request")


def registered_client(settings: Settings, client_id: str, redirect_uri: str, scope: str) -> None:
    """Independently validate the client, redirect and scopes before recording a grant."""
    if client_id.startswith("https://"):
        return _registered_cimd_client(settings, client_id, redirect_uri, scope)
    if not settings.clerk_backend_api_key:
        raise HTTPException(503, "Consent validation is not configured")
    try:
        with httpx.Client(
            timeout=10,
            follow_redirects=False,
            headers={
                "Authorization": "Bearer " + settings.clerk_backend_api_key.get_secret_value()
            },
        ) as client:
            for offset in range(0, 1000, 100):
                response = client.get(
                    "https://api.clerk.com/v1/oauth_applications",
                    params={"limit": 100, "offset": offset},
                )
                response.raise_for_status()
                entries = response.json()["data"]
                app = next((a for a in entries if a.get("client_id") == client_id), None)
                if app:
                    if (
                        redirect_uri not in app.get("redirect_uris", [])
                        or not set(scope.split()) <= set(app.get("scopes", "").split())
                        or "mcp:tools" not in scope.split()
                        or not app.get("consent_screen_enabled")
                    ):
                        raise HTTPException(400, "Invalid OAuth consent request")
                    return
                if len(entries) < 100:
                    break
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        raise HTTPException(503, "Could not validate the OAuth client") from None
    raise HTTPException(400, "OAuth client is not registered")


def user_for_subject(session, settings: Settings, subject: str) -> User:
    link = session.scalar(
        select(OAuthIdentityLink).where(
            OAuthIdentityLink.issuer == issuer_url(settings), OAuthIdentityLink.subject == subject
        )
    )
    if link:
        return link.user
    # Use the same stable ownership key as resource-bound OAuth authentication.
    import hashlib

    digest = hashlib.sha256(f"{issuer_url(settings)}\0{subject}".encode()).hexdigest()
    return get_or_create_user(
        session,
        AuthenticatedUser(
            external_user_id=f"oauth:{digest}", source="oauth", clerk_subject=subject
        ),
    )


@router.get("/oauth/consent")
def consent_page():
    settings = get_settings()
    issuer = issuer_url(settings)
    if not settings.clerk_publishable_key:
        raise HTTPException(503, "OAuth consent is not configured")
    return HTMLResponse(
        Path(__file__).with_name("consent.html").read_text(),
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Content-Security-Policy": (
                f"default-src 'self'; script-src 'self' {issuer}; connect-src 'self' {issuer}; "
                f"style-src 'self' 'unsafe-inline'; img-src 'self' data: https://img.clerk.com; "
                "worker-src 'self' blob:; "
                "frame-ancestors 'none'; base-uri 'none'"
            ),
        },
    )


@router.get("/oauth/consent/config")
def browser_config(request: Request):
    settings = get_settings()
    issuer = issuer_url(settings)
    if not settings.clerk_publishable_key:
        raise HTTPException(503, "OAuth consent is not configured")
    return JSONResponse(
        {
            "publishable_key": settings.clerk_publishable_key,
            "sdk_url": issuer + "/npm/@clerk/clerk-js@6.42.3/dist/clerk.browser.js",
            "ui_url": issuer + "/npm/@clerk/ui@1.42.4/dist/ui.browser.js",
            "resource_root": settings.base_url.rstrip("/") + "/mcp",
            "shared_resources": [
                settings.base_url.rstrip("/") + "/mcp/" + c["slug"]
                for c in request.app.state.access_catalog
                if c.get("access_mode") == "authenticated_shared"
            ],
            "browser_client_id": settings.clerk_browser_client_id,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/oauth/consent/app.js")
def browser_script():
    return Response(
        Path(__file__).with_name("consent.js").read_text(),
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )


class ConsentContext(BaseModel):
    client_id: str = Field(min_length=1, max_length=255)
    resource: str = Field(min_length=1, max_length=1024)
    redirect_uri: str = Field(min_length=1, max_length=2048)
    scope: str = Field(min_length=1, max_length=4096)


@router.post("/api/consent/context")
def context(body: ConsentContext, request: Request):
    settings = get_settings()
    subject = browser_subject(request, settings)
    connectors = consent_tools(request, body.resource, subject)
    registered_client(settings, body.client_id, body.redirect_uri, body.scope)
    intent = URLSafeTimedSerializer(settings.session_secret, salt=_INTENT_SALT).dumps(
        {
            "sub": subject,
            **body.model_dump(),
        }
    )
    with session_scope() as session:
        user = user_for_subject(session, settings, subject)
        grant = grant_for(session, user.id, settings, body.client_id)
        known = personal_tool_names(connectors)
        tools = sorted(set(grant.tools) & known) if grant and not grant.revoked else []
    return JSONResponse(
        {"intent": intent, "connectors": connectors, "tools": tools},
        headers={"Cache-Control": "no-store"},
    )


class ConsentApproval(BaseModel):
    intent: str = Field(min_length=1, max_length=16384)
    tools: list[str] = Field(max_length=1000)


@router.post("/api/consent/approve")
def approve(body: ConsentApproval, request: Request):
    settings = get_settings()
    subject = browser_subject(request, settings)
    try:
        intent = URLSafeTimedSerializer(settings.session_secret, salt=_INTENT_SALT).loads(
            body.intent, max_age=600
        )
        if intent["sub"] != subject:
            raise ValueError
        validated = ConsentContext(**intent)
    except (BadSignature, ValueError, KeyError, TypeError):
        raise HTTPException(400, "Invalid or expired consent request") from None
    connectors = consent_tools(request, validated.resource, subject)
    known = personal_tool_names(connectors)
    if not set(body.tools) <= known:
        raise HTTPException(400, "Tool is outside the requested connector")
    if not known:
        return {"saved": True}
    with session_scope() as session:
        user = user_for_subject(session, settings, subject)
        session.scalar(select(User).where(User.id == user.id).with_for_update())
        grant = grant_for(session, user.id, settings, validated.client_id)
        if grant is None:
            grant = ClientGrant(
                user_id=user.id,
                issuer=issuer_url(settings),
                client_id=validated.client_id,
                tools=[],
            )
            session.add(grant)
        previous = set(grant.tools or []) if not grant.revoked else set()
        grant.tools, grant.revoked = sorted((previous - known) | set(body.tools)), False
    return {"saved": True}
