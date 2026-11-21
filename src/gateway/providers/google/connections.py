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

# Re-exported for backward compatibility: existing code imports these
# exceptions from this module; the canonical definitions now live in
# gateway.connectors.errors so gateway.providers.registry doesn't need to
# import from a specific provider package.
from gateway.connectors.errors import AccountConflict, ReauthRequired  # noqa: F401
from gateway.crypto.tokens import get_cipher
from gateway.db.models import ProviderConnection, ProviderToken, User

PROVIDER = "google"
_EXPIRY_SKEW = timedelta(seconds=60)


@dataclass
class StoredCredentials:
    """Plaintext token material handed to/from the OAuth layer."""

    access_token: str
    refresh_token: str | None
    expires_at: datetime | None


def _lock_user(session: Session, user_id) -> None:
    """Serialize connection changes per user (concurrent callbacks, default switches)."""
    session.scalar(select(User).where(User.id == user_id).with_for_update())


def upsert_connection(
    session: Session,
    *,
    user: User,
    provider_account_id: str,
    provider_email: str | None,
    scopes: list[str],
    creds: StoredCredentials,
    provider: str = PROVIDER,
    product: str = "",
) -> ProviderConnection:
    """Create or update one account's connection (for one product) and its tokens.

    Google connections are per product (``product`` set) and a product may hold several
    accounts; the first active account of a product becomes its default. Providers
    without products (``product == ""``) keep the single-account rule. If the upstream
    does not return a refresh token on re-consent, the previously stored refresh token is
    preserved rather than overwritten with null.
    """
    _lock_user(session, user.id)

    active = session.scalars(
        select(ProviderConnection).where(
            ProviderConnection.user_id == user.id,
            ProviderConnection.provider == provider,
            ProviderConnection.product == product,
            ProviderConnection.status == "active",
        )
    ).all()
    if not product and any(a.provider_account_id != provider_account_id for a in active):
        raise AccountConflict(
            "A different account is already connected. Disconnect it before "
            "connecting another account."
        )

    cipher = get_cipher()

    conn = session.scalar(
        select(ProviderConnection).where(
            ProviderConnection.user_id == user.id,
            ProviderConnection.provider == provider,
            ProviderConnection.product == product,
            ProviderConnection.provider_account_id == provider_account_id,
        )
    )
    if conn is None:
        conn = ProviderConnection(
            user_id=user.id,
            provider=provider,
            product=product,
            provider_account_id=provider_account_id,
        )
        session.add(conn)

    conn.provider_email = provider_email
    # Incremental OAuth may return only the scopes involved in this consent
    # response. Never discard scopes previously granted to the same account.
    conn.scopes = sorted(set(conn.scopes or []) | set(scopes))
    conn.status = "active"
    # Exactly one default per product among active accounts.
    conn.is_default = not any(a.is_default and a is not conn for a in active)
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


def list_connections(
    session: Session, user_id, provider: str = PROVIDER, product: str = ""
) -> list[ProviderConnection]:
    """The user's active/errored connections for a product, default account first."""
    rows = session.scalars(
        select(ProviderConnection)
        .where(
            ProviderConnection.user_id == user_id,
            ProviderConnection.provider == provider,
            ProviderConnection.product == product,
            ProviderConnection.status.in_(("active", "error")),
        )
        .order_by(
            ProviderConnection.is_default.desc(),
            ProviderConnection.created_at,
            ProviderConnection.provider_email,
        )
    ).all()
    return list(rows)


def get_active_connection(
    session: Session, user_id, provider: str = PROVIDER, product: str = ""
) -> ProviderConnection | None:
    """Return the user's default active connection for a provider/product, if any."""
    for conn in list_connections(session, user_id, provider, product):
        if conn.status == "active":
            return conn
    return None


def resolve_connection(
    session: Session,
    user_id,
    provider: str = PROVIDER,
    product: str = "",
    account: str | None = None,
) -> ProviderConnection | None:
    """Pick the connection for a call: the account named by ``account``, else the default.

    ``account`` matches the account email (case-insensitive) or the stable provider
    account id. Returns ``None`` if nothing matches.
    """
    if not account:
        return get_active_connection(session, user_id, provider, product)
    wanted = account.strip().lower()
    for conn in list_connections(session, user_id, provider, product):
        if conn.status != "active":
            continue
        if (conn.provider_email or "").lower() == wanted or conn.provider_account_id == account:
            return conn
    return None


def _owned_connection(session: Session, user_id, connection_id) -> ProviderConnection | None:
    conn = session.get(ProviderConnection, connection_id)
    if conn is None or conn.user_id != user_id:
        return None
    return conn


def set_default(session: Session, user_id, connection_id) -> bool:
    """Make a connection the default for its product. False if not found/not active."""
    _lock_user(session, user_id)
    conn = _owned_connection(session, user_id, connection_id)
    if conn is None or conn.status != "active":
        return False
    others = session.scalars(
        select(ProviderConnection).where(
            ProviderConnection.user_id == user_id,
            ProviderConnection.provider == conn.provider,
            ProviderConnection.product == conn.product,
            ProviderConnection.is_default.is_(True),
            ProviderConnection.id != conn.id,
        )
    ).all()
    for other in others:
        other.is_default = False
    session.flush()  # clear the old default before setting the new one (partial unique index)
    conn.is_default = True
    return True


def disconnect_connection(session: Session, user_id, connection_id) -> bool:
    """Revoke one account's connection and promote another account to default if needed."""
    _lock_user(session, user_id)
    conn = _owned_connection(session, user_id, connection_id)
    if conn is None or conn.status == "revoked":
        return False
    was_default = conn.is_default
    conn.status = "revoked"
    conn.is_default = False
    if conn.token is not None:
        session.delete(conn.token)
    session.flush()
    if was_default:
        remaining = [
            c
            for c in list_connections(session, user_id, conn.provider, conn.product)
            if c.status == "active"
        ]
        if remaining:
            remaining[0].is_default = True
    return True


def disconnect(session: Session, user_id, provider: str = PROVIDER, product: str = "") -> bool:
    """Revoke every active connection for a provider/product (non-Google providers)."""
    removed = False
    for conn in list_connections(session, user_id, provider, product):
        removed = disconnect_connection(session, user_id, conn.id) or removed
    return removed


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
