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
