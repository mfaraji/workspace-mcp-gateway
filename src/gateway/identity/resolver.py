"""Identity resolution and the request trust boundary.

This module is the single chokepoint for deciding *who* is making a request.
There are two front doors, resolved here and nowhere else:

* **Bearer token** (``Authorization: Bearer wmcp_...``) — a self-contained
  per-user secret used by native MCP clients (Cursor, Claude Desktop). Works from
  any network path, including the public reverse proxy.
* **Trusted header identity** (``X-Open-WebUI-*``) — used by the on-host Open
  WebUI deployment. Only honored when the request also presents the
  ``X-Gateway-Auth`` shared secret, which the public reverse proxy strips. The
  caller-controlled ``Origin`` header is *not* trusted for this.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from gateway.config import Settings
from gateway.crypto.tokens import hash_token
from gateway.db.engine import session_scope
from gateway.db.models import ClientToken, User
from gateway.identity.models import AuthenticatedUser, IdentityError

HEADER_USER_ID = "x-open-webui-user-id"
HEADER_USER_EMAIL = "x-open-webui-user-email"
HEADER_USER_NAME = "x-open-webui-user-name"
HEADER_GATEWAY_AUTH = "x-gateway-auth"
HEADER_AUTHORIZATION = "authorization"


def _normalize(headers: Mapping[str, str]) -> dict[str, str]:
    """Lower-case header keys for case-insensitive lookup."""
    return {k.lower(): v for k, v in headers.items()}


def _gateway_secret_ok(headers: Mapping[str, str], settings: Settings) -> bool:
    """Return True if the request proves it comes from the trusted Open WebUI.

    The proof is the ``X-Gateway-Auth`` shared secret, compared in constant time.
    The public reverse proxy strips this header, so a public caller can never
    present it. ``DEV_TRUST_ALL_ORIGINS`` bypasses the check for local dev only.
    """
    if settings.dev_trust_all_origins:
        return True

    presented = headers.get(HEADER_GATEWAY_AUTH, "")
    expected = settings.gateway_shared_secret
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented, expected)


def _extract_bearer(headers: Mapping[str, str]) -> str | None:
    """Return the bearer token from the Authorization header, if present."""
    value = headers.get(HEADER_AUTHORIZATION, "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def _resolve_bearer(token: str, session: Session) -> AuthenticatedUser:
    """Resolve a native-client bearer token to its user, or raise.

    Looks up the un-revoked ``ClientToken`` by hash and bumps ``last_used_at``.
    """
    row = session.scalar(
        select(ClientToken).where(
            ClientToken.token_hash == hash_token(token),
            ClientToken.revoked_at.is_(None),
        )
    )
    if row is None:
        raise IdentityError("invalid or revoked bearer token")

    row.last_used_at = datetime.now(UTC)
    user = row.user
    return AuthenticatedUser(
        external_user_id=user.external_user_id,
        email=user.email,
        display_name=user.display_name,
        source="token",
    )


def resolve_identity(
    headers: Mapping[str, str],
    settings: Settings,
    session: Session | None = None,
) -> AuthenticatedUser:
    """Resolve the authenticated user from request headers, or raise.

    Dispatches between two front doors:

    1. ``Authorization: Bearer <token>`` — a native-client token, valid from any
       network path. Requires a DB session; one is opened if not supplied.
    2. ``X-Open-WebUI-*`` identity — trusted only when the ``X-Gateway-Auth``
       shared secret is presented.

    Raises:
        IdentityError: if the request is not authorized or carries no user id.
    """
    norm = _normalize(headers)

    bearer = _extract_bearer(norm)
    if bearer is not None:
        if session is not None:
            return _resolve_bearer(bearer, session)
        with session_scope() as owned:
            return _resolve_bearer(bearer, owned)

    if not _gateway_secret_ok(norm, settings):
        raise IdentityError("request not authorized for header-based identity")

    external_user_id = (norm.get(HEADER_USER_ID) or "").strip()
    if not external_user_id:
        raise IdentityError("missing X-Open-WebUI-User-Id")

    email = (norm.get(HEADER_USER_EMAIL) or "").strip() or None
    display_name = (norm.get(HEADER_USER_NAME) or "").strip() or None
    return AuthenticatedUser(
        external_user_id=external_user_id, email=email, display_name=display_name
    )


def get_or_create_user(session: Session, auth: AuthenticatedUser) -> User:
    """Upsert a ``User`` by external id, refreshing email/display name."""
    user = session.scalar(
        select(User).where(User.external_user_id == auth.external_user_id)
    )
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
