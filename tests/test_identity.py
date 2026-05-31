"""Tests for the identity trust boundary and token encryption."""

from __future__ import annotations

import pytest

from gateway.config import Settings
from gateway.crypto.tokens import TokenCipher
from gateway.identity.models import IdentityError
from gateway.identity.resolver import resolve_identity

SECRET = "shared-secret-value"


def _settings(**overrides) -> Settings:
    base = dict(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
        base_url="http://localhost:8000",
        google_client_id="cid",
        google_client_secret="secret",
        token_encryption_key="x" * 43 + "=",  # shape-only; not used here
        gateway_shared_secret=SECRET,
        trusted_open_webui_origin="https://openwebui.internal",
        session_secret="sess",
        dev_trust_all_origins=False,
    )
    base.update(overrides)
    return Settings(**base)


def test_rejects_request_without_secret_or_header():
    settings = _settings()
    with pytest.raises(IdentityError):
        resolve_identity({"origin": "https://openwebui.internal"}, settings)


def test_rejects_header_without_shared_secret():
    """A spoofable Origin is no longer enough — the secret is required."""
    settings = _settings()
    with pytest.raises(IdentityError):
        resolve_identity(
            {"origin": "https://openwebui.internal", "x-open-webui-user-id": "alice"},
            settings,
        )


def test_rejects_wrong_shared_secret():
    settings = _settings()
    with pytest.raises(IdentityError):
        resolve_identity(
            {"x-gateway-auth": "nope", "x-open-webui-user-id": "alice"},
            settings,
        )


def test_accepts_with_shared_secret():
    settings = _settings()
    auth = resolve_identity(
        {
            "X-Gateway-Auth": SECRET,
            "X-Open-WebUI-User-Id": "alice",
            "X-Open-WebUI-User-Email": "alice@example.com",
            "X-Open-WebUI-User-Name": "Alice",
        },
        settings,
    )
    assert auth.external_user_id == "alice"
    assert auth.email == "alice@example.com"
    assert auth.display_name == "Alice"


def test_accepts_openwebui_oneword_header_spelling():
    """Open WebUI forwards X-OpenWebUI-User-* (one word); accept that too."""
    settings = _settings()
    auth = resolve_identity(
        {
            "X-Gateway-Auth": SECRET,
            "X-OpenWebUI-User-Id": "dave",
            "X-OpenWebUI-User-Email": "dave@example.com",
        },
        settings,
    )
    assert auth.external_user_id == "dave"
    assert auth.email == "dave@example.com"


def test_secret_present_but_missing_user_id():
    settings = _settings()
    with pytest.raises(IdentityError):
        resolve_identity({"x-gateway-auth": SECRET}, settings)


def test_dev_trust_all_origins_bypasses_secret_check():
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
