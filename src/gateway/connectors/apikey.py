"""Per-user API key storage for connectors whose upstream has no OAuth."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from gateway.crypto.tokens import get_cipher
from gateway.db.models import ProviderConnection, ProviderToken

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# A user-pasted API key has no OAuth account to identify it by; this fixed
# value fills the (user_id, provider, provider_account_id) unique slot.
_API_KEY_ACCOUNT_ID = "apikey"


def store_api_key(
    session: Session, user_id: uuid.UUID, provider: str, api_key: str
) -> ProviderConnection:
    """Encrypt and store a user-pasted API key as their connection for ``provider``."""
    conn = session.scalar(
        select(ProviderConnection).where(
            ProviderConnection.user_id == user_id,
            ProviderConnection.provider == provider,
            ProviderConnection.provider_account_id == _API_KEY_ACCOUNT_ID,
        )
    )
    if conn is None:
        conn = ProviderConnection(
            user_id=user_id,
            provider=provider,
            provider_account_id=_API_KEY_ACCOUNT_ID,
        )
        session.add(conn)

    conn.status = "active"
    session.flush()

    token = conn.token
    if token is None:
        token = ProviderToken(connection_id=conn.id)
        session.add(token)
    token.encrypted_access_token = get_cipher().encrypt(api_key)
    token.expires_at = None
    session.flush()
    return conn


def get_api_key_connection(
    session: Session, user_id: uuid.UUID, provider: str
) -> ProviderConnection | None:
    """Return the user's active pasted-key connection for ``provider``, if any."""
    return session.scalar(
        select(ProviderConnection).where(
            ProviderConnection.user_id == user_id,
            ProviderConnection.provider == provider,
            ProviderConnection.provider_account_id == _API_KEY_ACCOUNT_ID,
            ProviderConnection.status == "active",
        )
    )


# The default workspace for Clerk act-as lives in a token-less connection row
# next to (never instead of) any pasted-key row; metadata is JSONB, so no
# migration is needed.
_CLERK_ACCOUNT_ID = "clerk"


def _clerk_connection(
    session: Session, user_id: uuid.UUID, provider: str
) -> ProviderConnection | None:
    return session.scalar(
        select(ProviderConnection).where(
            ProviderConnection.user_id == user_id,
            ProviderConnection.provider == provider,
            ProviderConnection.provider_account_id == _CLERK_ACCOUNT_ID,
        )
    )


def get_default_workspace(
    session: Session, user_id: uuid.UUID, provider: str
) -> dict[str, str] | None:
    """Return ``{"workspace_id", "workspace_name"}`` stored for act-as, or ``None``."""
    conn = _clerk_connection(session, user_id, provider)
    if conn is None or conn.status != "active":
        return None
    meta = conn.connection_metadata or {}
    workspace_id = meta.get("workspace_id")
    if not workspace_id:
        return None
    return {"workspace_id": workspace_id, "workspace_name": meta.get("workspace_name") or ""}


def set_default_workspace(
    session: Session,
    user_id: uuid.UUID,
    provider: str,
    workspace_id: str,
    workspace_name: str,
) -> ProviderConnection:
    """Upsert the user's default Apex workspace for act-as (no token is stored)."""
    conn = _clerk_connection(session, user_id, provider)
    if conn is None:
        conn = ProviderConnection(
            user_id=user_id, provider=provider, provider_account_id=_CLERK_ACCOUNT_ID
        )
        session.add(conn)
    conn.status = "active"
    conn.connection_metadata = {"workspace_id": workspace_id, "workspace_name": workspace_name}
    session.flush()
    return conn
