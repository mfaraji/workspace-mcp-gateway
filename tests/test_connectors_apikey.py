"""Tests for per-user API key storage: intake, ``ApiKeyStrategy``, and the route."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from gateway.config import Settings
from gateway.connectors import apikey, upstream
from gateway.providers.base import CallContext
from gateway.providers.registry import ToolError


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
        base_url="https://mcp.ashpazi.shop",
        google_client_id="cid",
        google_client_secret="secret",
        token_encryption_key="x" * 43 + "=",
        gateway_shared_secret="shared-secret-value",
        trusted_open_webui_origin="https://openwebui.internal",
        session_secret="sess",
    )


class _Cipher:
    def encrypt(self, value: str) -> bytes:
        return f"encrypted:{value}".encode()

    def decrypt(self, value: bytes) -> str:
        return value.decode().removeprefix("encrypted:")


class _Session:
    def __init__(self, *, existing, scalar_values=None):
        self.existing = existing
        self.scalar_values = iter(scalar_values if scalar_values is not None else [existing])
        self.added = []

    def scalar(self, _statement):
        return next(self.scalar_values)

    def add(self, value):
        self.added.append(value)

    def flush(self):
        return None


def _ctx(user_id=None) -> CallContext:
    return CallContext(user_id=user_id or uuid.uuid4(), external_user_id="alice", request_id="r1")


def _request(headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": raw_headers})


def _body(response) -> dict:
    return json.loads(response.body)


# --- store_api_key ----------------------------------------------------------


def test_store_api_key_creates_connection_and_token(monkeypatch):
    monkeypatch.setattr(apikey, "get_cipher", lambda: _Cipher())
    user_id = uuid.uuid4()
    session = _Session(existing=None)

    conn = apikey.store_api_key(session, user_id, "apex", "pk_live_new")

    assert conn.user_id == user_id
    assert conn.provider == "apex"
    assert conn.provider_account_id == "apikey"
    assert conn.status == "active"
    assert conn in session.added
    token = next(obj for obj in session.added if obj is not conn)
    assert token.encrypted_access_token == b"encrypted:pk_live_new"
    assert token.expires_at is None


def test_store_api_key_overwrites_existing_key(monkeypatch):
    monkeypatch.setattr(apikey, "get_cipher", lambda: _Cipher())
    existing_token = SimpleNamespace(encrypted_access_token=b"encrypted:old", expires_at=None)
    existing_conn = SimpleNamespace(id=uuid.uuid4(), status="active", token=existing_token)
    session = _Session(existing=existing_conn)

    conn = apikey.store_api_key(session, uuid.uuid4(), "apex", "pk_live_rotated")

    assert conn is existing_conn
    assert existing_token.encrypted_access_token == b"encrypted:pk_live_rotated"
    # No new ProviderConnection/ProviderToken rows for an update.
    assert session.added == []


# --- ApiKeyStrategy -----------------------------------------------------------


def _strategy(**overrides):
    defaults = dict(
        upstream_provider="apex",
        display_name="Apex",
        set_key_tool="apex_set_api_key",
    )
    defaults.update(overrides)
    return upstream.ApiKeyStrategy(**defaults)


def test_get_credentials_returns_stored_key_when_connected(monkeypatch):
    monkeypatch.setattr(upstream, "get_cipher", lambda: _Cipher())
    token = SimpleNamespace(encrypted_access_token=b"encrypted:pk_live_personal")
    conn = SimpleNamespace(token=token)
    monkeypatch.setattr(upstream, "get_active_connection", lambda _s, _u, _p: conn)

    strategy = _strategy(fallback_key=lambda _settings: "pk_live_fallback")
    result = strategy.get_credentials(None, _ctx(), _settings())

    assert result == "pk_live_personal"


def test_get_credentials_falls_back_when_not_connected(monkeypatch):
    monkeypatch.setattr(upstream, "get_active_connection", lambda _s, _u, _p: None)

    strategy = _strategy(fallback_key=lambda settings: settings.apex_api_key)
    result = strategy.get_credentials(None, _ctx(), _settings())

    assert result == "pk_live_fallback"


def test_get_credentials_raises_not_connected_with_no_fallback(monkeypatch):
    monkeypatch.setattr(upstream, "get_active_connection", lambda _s, _u, _p: None)

    strategy = _strategy(fallback_key=None)

    with pytest.raises(ToolError) as exc_info:
        strategy.get_credentials(None, _ctx(), _settings())

    assert exc_info.value.error_code == "not_connected"
    assert "apex_set_api_key" in str(exc_info.value)


def test_connect_url_is_none_for_api_key_strategy():
    strategy = _strategy()
    assert strategy.connect_url(_settings(), "alice") is None
