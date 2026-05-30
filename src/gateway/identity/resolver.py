"""Identity resolution and the header trust boundary.

This module is the single chokepoint for deciding *who* is making a request.
Plain ``X-Open-WebUI-*`` headers are only trusted when the request demonstrably
originates from the trusted Open WebUI deployment; otherwise we refuse to
identify the caller. A future "prefer verified MCP/OAuth identity" branch belongs
here and nowhere else.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from gateway.config import Settings
from gateway.db.models import User
from gateway.identity.models import AuthenticatedUser, IdentityError

HEADER_USER_ID = "x-open-webui-user-id"
HEADER_USER_EMAIL = "x-open-webui-user-email"
HEADER_USER_NAME = "x-open-webui-user-name"


def _normalize(headers: Mapping[str, str]) -> dict[str, str]:
    """Lower-case header keys for case-insensitive lookup."""
    return {k.lower(): v for k, v in headers.items()}


def _origin_is_trusted(headers: Mapping[str, str], settings: Settings) -> bool:
    """Return True if the request originates from the trusted Open WebUI origin.

    In production the gateway runs behind a reverse proxy that (a) strips any
    client-supplied ``X-Open-WebUI-*`` and ``X-Forwarded-*`` headers and (b) sets
    a trustworthy ``Origin`` / forwarded host. ``DEV_TRUST_ALL_ORIGINS`` bypasses
    the check for local development only.
    """
    if settings.dev_trust_all_origins:
        return True

    trusted = settings.trusted_open_webui_origin.rstrip("/")
    origin = headers.get("origin", "").rstrip("/")
    if origin and origin == trusted:
        return True

    # Fall back to the proxy-asserted forwarded host + scheme.
    fwd_host = headers.get("x-forwarded-host", "")
    fwd_proto = headers.get("x-forwarded-proto", "")
    if fwd_host and fwd_proto:
        reconstructed = f"{fwd_proto}://{fwd_host}".rstrip("/")
        if reconstructed == trusted:
            return True

    return False


def resolve_identity(headers: Mapping[str, str], settings: Settings) -> AuthenticatedUser:
    """Resolve the authenticated user from request headers, or raise.

    Raises:
        IdentityError: if the origin is untrusted or no user id is present.
    """
    norm = _normalize(headers)

    if not _origin_is_trusted(norm, settings):
        raise IdentityError("request origin is not trusted for header-based identity")

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
