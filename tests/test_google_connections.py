"""Unit tests for the single-account Google connection invariant."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from gateway.providers.google import connections


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class _Session:
    def __init__(self, *, active, scalar_values):
        self.active = active
        self.scalar_values = iter(scalar_values)
        self.added = []

    def scalar(self, _statement):
        return next(self.scalar_values)

    def scalars(self, _statement):
        return _ScalarResult(self.active)

    def add(self, value):
        self.added.append(value)

    def flush(self):
        return None


class _Cipher:
    def encrypt(self, value):
        return f"encrypted:{value}".encode()


def test_reauthorization_extends_existing_account_scopes(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4())
    token = SimpleNamespace(
        encrypted_access_token=b"old",
        encrypted_refresh_token=b"keep-me",
        expires_at=None,
    )
    conn = SimpleNamespace(
        id=uuid.uuid4(),
        provider_account_id="google-a",
        provider_email="old@example.com",
        scopes=["calendar"],
        status="active",
        token=token,
    )
    session = _Session(active=conn, scalar_values=[user, conn])
    monkeypatch.setattr(connections, "get_cipher", lambda: _Cipher())

    result = connections.upsert_connection(
        session,
        user=user,
        provider_account_id="google-a",
        provider_email="new@example.com",
        scopes=["drive"],
        creds=connections.StoredCredentials("new-access", None, None),
    )

    assert result.scopes == ["calendar", "drive"]
    assert result.provider_email == "new@example.com"
    assert token.encrypted_access_token == b"encrypted:new-access"
    assert token.encrypted_refresh_token == b"keep-me"


def test_different_google_account_conflicts_while_one_is_active():
    user = SimpleNamespace(id=uuid.uuid4())
    active = SimpleNamespace(provider_account_id="google-a")
    session = _Session(active=active, scalar_values=[user])

    with pytest.raises(connections.AccountConflict, match="different Google account"):
        connections.upsert_connection(
            session,
            user=user,
            provider_account_id="google-b",
            provider_email="b@example.com",
            scopes=["drive"],
            creds=connections.StoredCredentials("access", "refresh", None),
        )
