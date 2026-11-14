"""OAuth-only identity resolution with stable issuer/subject ownership."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from gateway.config import Settings
from gateway.db.engine import session_scope
from gateway.db.models import OAuthIdentityLink, User
from gateway.identity.models import AuthenticatedUser, IdentityError


def _extract_bearer(headers: Mapping[str, str]) -> str | None:
    """Return the bearer token from the Authorization header, if present."""
    value = headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def _resolve_oauth(
    token: str, settings: Settings, resource: str, session: Session
) -> AuthenticatedUser:
    """Map a Clerk subject to a stable gateway user, honoring explicit links."""
    from gateway.oauth.clerk import issuer_url, verify_access_claims

    claims = verify_access_claims(token, settings, resource)
    subject = claims["sub"]
    client_id = claims.get("client_id") or claims.get("azp")
    if not isinstance(client_id, str) or not client_id or len(client_id) > 255:
        raise IdentityError("missing verified OAuth client identifier")
    if claims.get("client_id") and claims.get("azp") and claims["client_id"] != claims["azp"]:
        raise IdentityError("ambiguous OAuth client identifier")
    issuer = issuer_url(settings)
    link = session.scalar(
        select(OAuthIdentityLink).where(
            OAuthIdentityLink.issuer == issuer,
            OAuthIdentityLink.subject == subject,
        )
    )
    if link is None:
        # The immutable issuer and subject, rather than the OAuth client ID or
        # mutable email, determine identity. Keep the legacy external ID short.
        digest = hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest()
        return AuthenticatedUser(
            external_user_id=f"oauth:{digest}",
            source="oauth",
            clerk_subject=subject,
            oauth_client_id=client_id,
            oauth_resource=resource,
        )
    user = link.user
    return AuthenticatedUser(
        external_user_id=user.external_user_id,
        email=user.email,
        display_name=user.display_name,
        source="oauth",
        clerk_subject=subject,
        oauth_client_id=client_id,
        oauth_resource=resource,
    )


def resolve_identity(
    headers: Mapping[str, str],
    settings: Settings,
    session: Session | None = None,
    *,
    oauth_resource: str | None = None,
) -> AuthenticatedUser:
    """Require a verified resource-bound OAuth token; legacy credentials are rejected."""
    norm = {k.lower(): v for k, v in headers.items()}
    bearer = _extract_bearer(norm)
    if not bearer or bearer.startswith("wmcp_"):
        raise IdentityError("OAuth authentication required")
    resource = oauth_resource or f"{settings.base_url.rstrip('/')}/mcp"
    if session is not None:
        return _resolve_oauth(bearer, settings, resource, session)
    with session_scope() as owned:
        return _resolve_oauth(bearer, settings, resource, owned)


def get_or_create_user(session: Session, auth: AuthenticatedUser) -> User:
    """Upsert a ``User`` by external id, refreshing email/display name."""
    user = session.scalar(select(User).where(User.external_user_id == auth.external_user_id))
    if user is None:
        user = User(
            external_user_id=auth.external_user_id,
            email=auth.email,
            display_name=auth.display_name,
        )
        session.add(user)
        session.flush()
        return user

    # Keep contact details fresh without clobbering with empty values.
    if auth.email and auth.email != user.email:
        user.email = auth.email
    if auth.display_name and auth.display_name != user.display_name:
        user.display_name = auth.display_name
    return user
