"""Persistence and lifecycle for Google provider connections and tokens.

Responsibilities:
- upsert a connection + encrypted tokens after the OAuth callback,
- load decrypted credentials and refresh them on expiry (with a row lock so
  concurrent tool calls for the same user don't race the token write),
- preserve an existing refresh token when Google declines to return a new one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from gateway.config import Settings
from gateway.crypto.tokens import get_cipher
from gateway.db.models import ProviderConnection, ProviderToken, User

PROVIDER = "google"
_EXPIRY_SKEW = timedelta(seconds=60)


class ReauthRequired(Exception):
    """Raised when a connection's tokens are unusable and re-consent is needed."""


@dataclass
class StoredCredentials:
    """Plaintext token material handed to/from the OAuth layer."""

    access_token: str
    refresh_token: str | None
    expires_at: datetime | None


def upsert_connection(
    session: Session,
    *,
    user: User,
    provider_account_id: str,
    provider_email: str | None,
    scopes: list[str],
    creds: StoredCredentials,
) -> ProviderConnection:
    """Create or update a connection and its encrypted tokens.

    If Google does not return a refresh token on re-consent, the previously
    stored refresh token is preserved rather than overwritten with null.
    """
    cipher = get_cipher()

    conn = session.scalar(
        select(ProviderConnection).where(
            ProviderConnection.user_id == user.id,
            ProviderConnection.provider == PROVIDER,
            ProviderConnection.provider_account_id == provider_account_id,
        )
    )
    if conn is None:
        conn = ProviderConnection(
            user_id=user.id,
            provider=PROVIDER,
            provider_account_id=provider_account_id,
        )
        session.add(conn)

    conn.provider_email = provider_email
    conn.scopes = scopes
    conn.status = "active"
    session.flush()

    token = conn.token
    if token is None:
        token = ProviderToken(connection_id=conn.id)
        session.add(token)

    token.encrypted_access_token = cipher.encrypt(creds.access_token)
    if creds.refresh_token:
        token.encrypted_refresh_token = cipher.encrypt(creds.refresh_token)
    # else: keep the existing encrypted_refresh_token (re-consent without one).
    token.expires_at = creds.expires_at
    session.flush()
    return conn


def get_active_connection(
    session: Session, user_id, provider: str = PROVIDER
) -> ProviderConnection | None:
    """Return the user's active connection for a provider, if any."""
    return session.scalar(
        select(ProviderConnection).where(
            ProviderConnection.user_id == user_id,
            ProviderConnection.provider == provider,
            ProviderConnection.status == "active",
        )
    )


def disconnect(session: Session, user_id, provider: str = PROVIDER) -> bool:
    """Revoke a connection: mark it revoked and clear stored tokens."""
    conn = session.scalar(
        select(ProviderConnection).where(
            ProviderConnection.user_id == user_id,
            ProviderConnection.provider == provider,
        )
    )
    if conn is None:
        return False
    conn.status = "revoked"
    if conn.token is not None:
        session.delete(conn.token)
    return True


def _needs_refresh(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return False
    return datetime.now(UTC) >= (expires_at - _EXPIRY_SKEW)


def load_credentials(session: Session, conn: ProviderConnection, settings: Settings):
    """Return refreshed google ``Credentials`` for a connection.

    Locks the token row (``SELECT ... FOR UPDATE``) before the read-check-refresh
    -write so concurrent calls for the same user serialize on the refresh.

    Raises:
        ReauthRequired: if no tokens are stored or the refresh fails.
    """
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    cipher = get_cipher()

    token = session.scalar(
        select(ProviderToken).where(ProviderToken.connection_id == conn.id).with_for_update()
    )
    if token is None or not token.encrypted_refresh_token:
        raise ReauthRequired("no stored refresh token for connection")

    creds = Credentials(
        token=cipher.decrypt(token.encrypted_access_token),
        refresh_token=cipher.decrypt(token.encrypted_refresh_token),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=conn.scopes,
    )

    if creds.expired or _needs_refresh(token.expires_at):
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            conn.status = "error"
            session.flush()
            raise ReauthRequired("token refresh failed; re-authorization required") from exc

        token.encrypted_access_token = cipher.encrypt(creds.token)
        if creds.refresh_token:
            token.encrypted_refresh_token = cipher.encrypt(creds.refresh_token)
        token.expires_at = (
            creds.expiry.replace(tzinfo=UTC) if creds.expiry else None
        )
        session.flush()

    return creds
