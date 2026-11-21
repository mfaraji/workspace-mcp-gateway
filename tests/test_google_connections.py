"""Unit tests for per-product, multi-account Google connections."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from gateway.providers.google import connections


class _ScalarResult:
    def __init__(self, rows):
        self.rows = rows

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


class _Session:
    def __init__(self, *, active, scalar_values):
        self.active = active if isinstance(active, list) else [active] if active else []
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
        is_default=True,
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


def test_second_account_for_a_product_is_allowed_and_not_default(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4())
    first = SimpleNamespace(provider_account_id="google-a", is_default=True, status="active")
    session = _Session(active=first, scalar_values=[user, None])
    monkeypatch.setattr(connections, "get_cipher", lambda: _Cipher())

    second = connections.upsert_connection(
        session,
        user=user,
        provider_account_id="google-b",
        provider_email="b@example.com",
        scopes=["drive"],
        creds=connections.StoredCredentials("access", "refresh", None),
        product="drive",
    )

    assert second.product == "drive"
    assert second.is_default is False
    assert first.is_default is True


def test_first_account_for_a_product_becomes_default(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4())
    session = _Session(active=[], scalar_values=[user, None])
    monkeypatch.setattr(connections, "get_cipher", lambda: _Cipher())

    conn = connections.upsert_connection(
        session,
        user=user,
        provider_account_id="google-a",
        provider_email="a@example.com",
        scopes=["tasks"],
        creds=connections.StoredCredentials("access", "refresh", None),
        product="tasks",
    )

    assert conn.is_default is True


def test_productless_provider_keeps_single_account_rule():
    user = SimpleNamespace(id=uuid.uuid4())
    active = SimpleNamespace(provider_account_id="acct-a", is_default=False, status="active")
    session = _Session(active=active, scalar_values=[user])

    with pytest.raises(connections.AccountConflict):
        connections.upsert_connection(
            session,
            user=user,
            provider_account_id="acct-b",
            provider_email="b@example.com",
            scopes=[],
            creds=connections.StoredCredentials("access", "refresh", None),
            provider="other",
        )


def test_resolve_connection_matches_email_case_insensitively(monkeypatch):
    a = SimpleNamespace(
        provider_email="A@Example.com", provider_account_id="sa", status="active", is_default=True
    )
    b = SimpleNamespace(
        provider_email="b@example.com", provider_account_id="sb", status="active", is_default=False
    )
    monkeypatch.setattr(connections, "list_connections", lambda *_a: [a, b])

    assert connections.resolve_connection(None, "u", "google", "gmail", "B@EXAMPLE.com") is b
    assert connections.resolve_connection(None, "u", "google", "gmail", None) is a
    assert connections.resolve_connection(None, "u", "google", "gmail", "c@example.com") is None
