"""DB-backed integration tests.

Skipped automatically when the configured database is unreachable, so the unit
suite still runs in environments without Postgres. When the local ``workspace_mcp``
database exists and migrations are applied, these verify the core data-isolation
and at-rest-encryption guarantees from the spec.
"""

from __future__ import annotations

import pytest

from gateway.crypto.tokens import get_cipher
from gateway.db.engine import check_database, session_scope
from gateway.identity.models import AuthenticatedUser
from gateway.identity.resolver import get_or_create_user
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
