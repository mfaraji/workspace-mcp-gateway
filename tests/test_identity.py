"""Tests for the identity trust boundary and token encryption."""

from __future__ import annotations

import pytest

from gateway.config import Settings
from gateway.crypto.tokens import TokenCipher
from gateway.identity.models import IdentityError
from gateway.identity.resolver import resolve_identity


def _settings(**overrides) -> Settings:
    base = dict(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
        base_url="http://localhost:8000",
        google_client_id="cid",
        google_client_secret="secret",
        token_encryption_key="x" * 43 + "=",  # shape-only; not used here
        trusted_open_webui_origin="https://openwebui.internal",
        session_secret="sess",
        dev_trust_all_origins=False,
    )
    base.update(overrides)
    return Settings(**base)


def test_rejects_request_without_identity_header():
    settings = _settings()
    with pytest.raises(IdentityError):
        resolve_identity({"origin": "https://openwebui.internal"}, settings)


def test_rejects_untrusted_origin_even_with_header():
    settings = _settings()
    with pytest.raises(IdentityError):
        resolve_identity(
            {"origin": "https://evil.example", "x-open-webui-user-id": "alice"},
            settings,
        )


def test_accepts_trusted_origin_with_header():
    settings = _settings()
    auth = resolve_identity(
        {
            "Origin": "https://openwebui.internal",
            "X-Open-WebUI-User-Id": "alice",
            "X-Open-WebUI-User-Email": "alice@example.com",
            "X-Open-WebUI-User-Name": "Alice",
        },
        settings,
    )
    assert auth.external_user_id == "alice"
    assert auth.email == "alice@example.com"
    assert auth.display_name == "Alice"


def test_accepts_via_forwarded_host():
    settings = _settings()
    auth = resolve_identity(
        {
            "x-forwarded-proto": "https",
            "x-forwarded-host": "openwebui.internal",
            "x-open-webui-user-id": "bob",
        },
        settings,
    )
    assert auth.external_user_id == "bob"


def test_dev_trust_all_origins_bypasses_origin_check():
    settings = _settings(dev_trust_all_origins=True)
    auth = resolve_identity({"x-open-webui-user-id": "carol"}, settings)
    assert auth.external_user_id == "carol"


def test_token_cipher_roundtrip():
    from cryptography.fernet import Fernet

    cipher = TokenCipher(Fernet.generate_key().decode())
    ct = cipher.encrypt("super-secret-token")
    assert isinstance(ct, bytes)
    assert b"super-secret-token" not in ct  # actually encrypted
    assert cipher.decrypt(ct) == "super-secret-token"
