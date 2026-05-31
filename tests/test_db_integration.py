"""DB-backed integration tests.

Skipped automatically when the configured database is unreachable, so the unit
suite still runs in environments without Postgres. When the local ``workspace_mcp``
database exists and migrations are applied, these verify the core data-isolation
and at-rest-encryption guarantees from the spec.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete

from gateway.config import get_settings
from gateway.crypto.tokens import get_cipher, hash_token
from gateway.db.engine import check_database, session_scope
from gateway.db.models import ClientToken
from gateway.identity.models import AuthenticatedUser, IdentityError
from gateway.identity.resolver import get_or_create_user, resolve_identity
from gateway.providers.google.connections import (
    StoredCredentials,
    get_active_connection,
    upsert_connection,
)

pytestmark = pytest.mark.skipif(
    not check_database(), reason="database not reachable; run migrations first"
)


def test_get_or_create_user_is_idempotent():
    with session_scope() as session:
        a1 = get_or_create_user(session, AuthenticatedUser("itest-user-1", "u1@x.com", "U1"))
        id1 = a1.id
    with session_scope() as session:
        a2 = get_or_create_user(session, AuthenticatedUser("itest-user-1", "u1@x.com", "U1"))
        assert a2.id == id1


def test_two_users_are_isolated_and_tokens_encrypted():
    with session_scope() as session:
        alice = get_or_create_user(session, AuthenticatedUser("itest-alice"))
        bob = get_or_create_user(session, AuthenticatedUser("itest-bob"))

        upsert_connection(
            session, user=alice, provider_account_id="alice-google",
            provider_email="alice@gmail.com", scopes=["scope"],
            creds=StoredCredentials("alice-access", "alice-refresh", None),
        )
        upsert_connection(
            session, user=bob, provider_account_id="bob-google",
            provider_email="bob@gmail.com", scopes=["scope"],
            creds=StoredCredentials("bob-access", "bob-refresh", None),
        )

        alice_conn = get_active_connection(session, alice.id)
        bob_conn = get_active_connection(session, bob.id)

        # Isolation: each user sees only their own connection/account.
        assert alice_conn.provider_account_id == "alice-google"
        assert bob_conn.provider_account_id == "bob-google"

        # At rest: tokens are ciphertext, not plaintext.
        assert alice_conn.token.encrypted_access_token != b"alice-access"
        assert b"alice-access" not in alice_conn.token.encrypted_access_token
        # And decrypt round-trips to the original.
        cipher = get_cipher()
        assert cipher.decrypt(alice_conn.token.encrypted_access_token) == "alice-access"
        assert cipher.decrypt(bob_conn.token.encrypted_refresh_token) == "bob-refresh"


def _mint(session, external_user_id: str, token: str, *, revoked: bool = False) -> None:
    user = get_or_create_user(session, AuthenticatedUser(external_user_id))
    # Idempotent across re-runs: drop any prior token with this hash.
    session.execute(delete(ClientToken).where(ClientToken.token_hash == hash_token(token)))
    session.add(
        ClientToken(
            user_id=user.id,
            name="test",
            token_hash=hash_token(token),
            token_prefix=token[:12],
            revoked_at=datetime.now(UTC) if revoked else None,
        )
    )
    session.flush()


def test_bearer_token_resolves_from_untrusted_origin():
    """A valid bearer token authenticates with no gateway secret / origin."""
    settings = get_settings()
    with session_scope() as session:
        _mint(session, "itest-token-user", "wmcp_validtoken123")
        auth = resolve_identity(
            {"authorization": "Bearer wmcp_validtoken123"}, settings, session
        )
        assert auth.external_user_id == "itest-token-user"
        assert auth.source == "token"


def test_revoked_bearer_token_rejected():
    settings = get_settings()
    with session_scope() as session:
        _mint(session, "itest-token-revoked", "wmcp_revoked123", revoked=True)
        with pytest.raises(IdentityError):
            resolve_identity(
                {"authorization": "Bearer wmcp_revoked123"}, settings, session
            )


def test_unknown_bearer_token_rejected():
    settings = get_settings()
    with session_scope() as session:
        with pytest.raises(IdentityError):
            resolve_identity(
                {"authorization": "Bearer wmcp_doesnotexist"}, settings, session
            )
