"""OAuth protected-resource metadata and self-service Clerk identity linking."""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from gateway.config import get_settings
from gateway.connectors.registry import by_slug
from gateway.db.engine import session_scope
from gateway.db.models import OAuthIdentityLink, OAuthLinkIntent, User
from gateway.identity.models import IdentityError
from gateway.identity.resolver import get_or_create_user, resolve_identity
from gateway.logging import get_logger
from gateway.oauth.clerk import (
    authorization_endpoint,
    issuer_url,
    token_endpoint,
    verify_access_token,
)

router = APIRouter()
logger = get_logger(__name__)
_LINK_LIFETIME = timedelta(minutes=10)


def _resource(product: str | None = None) -> str:
    settings = get_settings()
    return f"{settings.base_url.rstrip('/')}/mcp" + (f"/{product}" if product else "")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _trusted_user(request: Request):
    """Only the authenticated Open WebUI backend can initiate/revoke links."""
    settings = get_settings()
    headers = {key.lower(): value for key, value in request.headers.items()}
    if not headers.get("x-gateway-auth"):
        raise IdentityError("trusted Open WebUI authentication required")
    # Never accept a bearer token here, even if a proxy accidentally forwards one.
    headers.pop("authorization", None)
    return resolve_identity(headers, settings)


def _claims_hint(token: str | None) -> str:
    """Describe a rejected token's non-secret claims for diagnosis; never the token."""
    if not token:
        return ""
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return " (access token is not a JWT)"
    hint = {key: claims.get(key) for key in ("iss", "aud", "scope", "azp", "client_id")}
    return f" (claims: {hint})"


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
@router.get("/.well-known/oauth-protected-resource/mcp/{product}")
async def protected_resource_metadata(product: str | None = None) -> JSONResponse:
    """RFC 9728 discovery for the all-tools and product-specific MCP resources."""
    settings = get_settings()
    if product is not None and by_slug(product) is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    try:
        issuer = issuer_url(settings)
    except IdentityError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(
        {
            "resource": _resource(product),
            "authorization_servers": [issuer],
            "scopes_supported": ["mcp:tools"],
            "bearer_methods_supported": ["header"],
        },
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/api/oauth/link/status")
def link_status(request: Request) -> JSONResponse:
    try:
        auth = _trusted_user(request)
        issuer = issuer_url(get_settings())
    except IdentityError as exc:
        return JSONResponse({"error": "unauthorized", "detail": str(exc)}, status_code=401)
    with session_scope() as session:
        user = get_or_create_user(session, auth)
        linked = (
            session.scalar(
                select(OAuthIdentityLink.id).where(
                    OAuthIdentityLink.user_id == user.id, OAuthIdentityLink.issuer == issuer
                )
            )
            is not None
        )
    return JSONResponse({"linked": linked}, headers={"Cache-Control": "no-store"})


@router.post("/api/oauth/link/start")
def link_start(request: Request) -> JSONResponse:
    """Return a one-time Clerk authorization URL for this Open WebUI user."""
    settings = get_settings()
    try:
        auth = _trusted_user(request)
        issuer_url(settings)
        if not settings.clerk_link_client_id or not settings.clerk_link_client_secret:
            raise IdentityError("OAuth account linking is not configured")
        endpoint = authorization_endpoint(settings)
    except IdentityError as exc:
        return JSONResponse({"error": "unauthorized", "detail": str(exc)}, status_code=401)

    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    with session_scope() as session:
        user = get_or_create_user(session, auth)
        session.execute(
            delete(OAuthLinkIntent).where(OAuthLinkIntent.expires_at < datetime.now(UTC))
        )
        session.add(
            OAuthLinkIntent(
                user_id=user.id,
                state_hash=_digest(state),
                code_verifier=verifier,
                expires_at=datetime.now(UTC) + _LINK_LIFETIME,
            )
        )
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.clerk_link_client_id,
            "redirect_uri": settings.clerk_link_redirect_uri,
            "scope": settings.clerk_link_scope,
            "resource": _resource(),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return JSONResponse({"url": f"{endpoint}?{query}"}, headers={"Cache-Control": "no-store"})


@router.get("/oauth/clerk/link/callback")
def link_callback(code: str = "", state: str = "", error: str = "") -> JSONResponse:
    """Exchange one code, verify its resource-bound token, and bind the subject."""
    if error or not code or not state or len(state) > 256 or len(code) > 4096:
        return JSONResponse({"error": "oauth_failed"}, status_code=400)
    settings = get_settings()
    now = datetime.now(UTC)
    with session_scope() as session:
        intent = session.scalar(
            select(OAuthLinkIntent)
            .where(OAuthLinkIntent.state_hash == _digest(state))
            .with_for_update()
        )
        expires_at = intent.expires_at if intent else None
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if intent is None or intent.used_at or expires_at < now:
            return JSONResponse({"error": "invalid_state"}, status_code=400)
        intent.used_at = now
        intent_id = intent.id
        user_id = intent.user_id
        verifier = intent.code_verifier

    token = None
    try:
        endpoint = token_endpoint(settings)
        response = httpx.post(
            endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.clerk_link_redirect_uri,
                "client_id": settings.clerk_link_client_id,
                "code_verifier": verifier,
                "resource": _resource(),
            },
            auth=(settings.clerk_link_client_id or "", settings.clerk_link_client_secret or ""),
            timeout=10,
        )
        response.raise_for_status()
        token = response.json()["access_token"]
        subject = verify_access_token(token, settings, _resource(), required_scope=None)
        issuer = issuer_url(settings)
    except (httpx.HTTPError, KeyError, ValueError, IdentityError) as exc:
        logger.warning("clerk link failed: %s: %s%s", type(exc).__name__, exc, _claims_hint(token))
        return JSONResponse({"error": "oauth_failed"}, status_code=400)

    try:
        with session_scope() as session:
            session.scalar(select(User).where(User.id == user_id).with_for_update())
            intent = session.get(OAuthLinkIntent, intent_id)
            if intent is None or intent.used_at is None:
                return JSONResponse({"error": "invalid_state"}, status_code=400)
            existing_subject = session.scalar(
                select(OAuthIdentityLink)
                .where(
                    OAuthIdentityLink.issuer == issuer,
                    OAuthIdentityLink.subject == subject,
                )
                .with_for_update()
            )
            if existing_subject and existing_subject.user_id != user_id:
                return JSONResponse({"error": "identity_already_linked"}, status_code=409)
            session.execute(
                delete(OAuthIdentityLink).where(
                    OAuthIdentityLink.user_id == user_id, OAuthIdentityLink.issuer == issuer
                )
            )
            session.add(OAuthIdentityLink(user_id=user_id, issuer=issuer, subject=subject))
    except IntegrityError:
        return JSONResponse({"error": "identity_already_linked"}, status_code=409)
    return JSONResponse({"linked": True}, headers={"Cache-Control": "no-store"})


@router.delete("/api/oauth/link")
def link_remove(request: Request) -> JSONResponse:
    try:
        auth = _trusted_user(request)
        issuer = issuer_url(get_settings())
    except IdentityError as exc:
        return JSONResponse({"error": "unauthorized", "detail": str(exc)}, status_code=401)
    with session_scope() as session:
        user = get_or_create_user(session, auth)
        session.scalar(select(User).where(User.id == user.id).with_for_update())
        session.execute(delete(OAuthLinkIntent).where(OAuthLinkIntent.user_id == user.id))
        session.execute(
            delete(OAuthIdentityLink).where(
                OAuthIdentityLink.user_id == user.id, OAuthIdentityLink.issuer == issuer
            )
        )
    return JSONResponse({"linked": False}, headers={"Cache-Control": "no-store"})
