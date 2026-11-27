"""OAuth resource metadata, token validation and tool declarations."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from gateway.app import create_app
from gateway.config import Settings
from gateway.db.models import OAuthIdentityLink, User
from gateway.identity.models import IdentityError
from gateway.identity.resolver import get_or_create_user, resolve_identity
from gateway.mcp.context import _send_401
from gateway.mcp.server import build_mcp, product_tool_filter
from gateway.oauth import clerk
from gateway.oauth.clerk_routes import protected_resource_metadata


def _settings(**overrides) -> Settings:
    data = dict(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
        base_url="https://mcp.example.test",
        google_client_id="cid",
        google_client_secret="secret",
        token_encryption_key="x" * 43 + "=",
        gateway_shared_secret="shared-secret",
        trusted_open_webui_origin="https://webui.example.test",
        session_secret="sess",
        clerk_issuer="https://clerk.example.test",
    )
    data.update(overrides)
    return Settings(**data)


@pytest.mark.asyncio
async def test_resource_metadata_on_all_and_product_mounts(monkeypatch):
    settings = _settings()
    monkeypatch.setattr("gateway.oauth.clerk_routes.get_settings", lambda: settings)
    all_tools = await protected_resource_metadata()
    calendar = await protected_resource_metadata("calendar")
    missing = await protected_resource_metadata("nope")
    all_payload = json.loads(all_tools.body)
    calendar_payload = json.loads(calendar.body)
    assert all_payload["resource"] == "https://mcp.example.test/mcp"
    assert calendar_payload["resource"] == "https://mcp.example.test/mcp/calendar"
    assert calendar_payload["authorization_servers"] == ["https://clerk.example.test"]
    assert missing.status_code == 404
    monkeypatch.setattr("gateway.app.get_settings", lambda: settings)
    app = create_app()
    # Ask the mounted app rather than inspecting app.routes: newer FastAPI keeps included
    # routers as single route objects.
    with TestClient(app) as client:
        response = client.get("/.well-known/oauth-protected-resource/mcp/calendar")
    assert response.status_code == 200
    assert response.json()["resource"] == "https://mcp.example.test/mcp/calendar"


@pytest.mark.asyncio
async def test_tools_advertise_oauth_when_enabled(monkeypatch):
    mcp = build_mcp(_settings(), product_tool_filter("telegram"))
    from gateway.identity.models import AuthenticatedUser
    from gateway.mcp.context import _current_user

    @contextmanager
    def scope():
        yield object()

    monkeypatch.setattr("gateway.db.engine.session_scope", scope)
    monkeypatch.setattr(
        "gateway.identity.resolver.get_or_create_user", lambda *_: SimpleNamespace(id="u")
    )
    monkeypatch.setattr(
        "gateway.access.policy.allowed_tools",
        lambda *_: {"telegram_list_destinations", "telegram_send_message"},
    )
    token = _current_user.set(
        AuthenticatedUser("u", source="oauth", clerk_subject="s", oauth_client_id="c")
    )
    try:
        tools = await mcp.list_tools()
    finally:
        _current_user.reset(token)
    assert {tool.name for tool in tools} == {
        "system_get_current_time",
        "telegram_list_destinations",
        "telegram_send_message",
    }
    assert all(
        tool.model_dump()["securitySchemes"] == [{"type": "oauth2", "scopes": ["mcp:tools"]}]
        for tool in tools
    )


def test_verifies_signature_resource_scope_and_expiry(monkeypatch):
    settings = _settings()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(clerk, "_issuer_endpoint", lambda *_: "https://clerk.example.test/jwks")
    monkeypatch.setattr(
        clerk,
        "_jwk_client",
        lambda *_: SimpleNamespace(
            get_signing_key_from_jwt=lambda _: SimpleNamespace(key=key.public_key())
        ),
    )

    def make_token(**claims):
        payload = {
            "iss": settings.clerk_issuer,
            "sub": "user_123",
            "aud": "https://mcp.example.test/mcp/telegram",
            "exp": datetime.now(UTC) + timedelta(minutes=10),
            "scope": "openid mcp:tools",
        }
        payload.update(claims)
        return jwt.encode(payload, key, algorithm="RS256")

    resource = "https://mcp.example.test/mcp/telegram"
    assert clerk.verify_access_token(make_token(), settings, resource) == "user_123"
    for token in (
        make_token(aud="https://evil.example.test/mcp/telegram"),
        make_token(scope="openid"),
        make_token(exp=datetime.now(UTC) - timedelta(minutes=2)),
        make_token(iss="https://evil.example.test"),
    ):
        with pytest.raises(IdentityError):
            clerk.verify_access_token(token, settings, resource)
    unbound_claims = jwt.decode(make_token(), options={"verify_signature": False})
    unbound_claims.pop("aud")
    with pytest.raises(IdentityError):
        clerk.verify_access_token(
            jwt.encode(unbound_claims, key, algorithm="RS256"), settings, resource
        )


@pytest.mark.asyncio
async def test_oauth_disabled_metadata_is_not_advertised(monkeypatch):
    settings = _settings(clerk_issuer=None)
    monkeypatch.setattr("gateway.oauth.clerk_routes.get_settings", lambda: settings)
    response = await protected_resource_metadata()
    assert response.status_code == 404


def test_clerk_subject_maps_to_same_user_across_oauth_clients(monkeypatch):
    settings = _settings()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    User.__table__.create(engine)
    OAuthIdentityLink.__table__.create(engine)
    monkeypatch.setattr(
        clerk,
        "verify_access_claims",
        lambda *_a, **_k: {"sub": "clerk-user-1", "client_id": "client"},
    )

    with Session(engine) as session:
        chatgpt = resolve_identity(
            {"Authorization": "Bearer chatgpt-token"},
            settings,
            session,
            oauth_resource="https://mcp.example.test/mcp",
        )
        cli = resolve_identity(
            {"Authorization": "Bearer cli-token"},
            settings,
            session,
            oauth_resource="https://mcp.example.test/mcp/telegram",
        )
        assert chatgpt.external_user_id == cli.external_user_id
        assert chatgpt.source == cli.source == "oauth"
        get_or_create_user(session, chatgpt)
        session.commit()
        assert get_or_create_user(session, cli).id == session.scalar(select(User.id))

    with Session(engine) as session:
        other_issuer = resolve_identity(
            {"Authorization": "Bearer chatgpt-token"},
            _settings(clerk_issuer="https://different.example.test"),
            session,
            oauth_resource="https://mcp.example.test/mcp",
        )
        assert other_issuer.external_user_id != chatgpt.external_user_id


@pytest.mark.asyncio
async def test_unauthorized_challenge_points_to_product_metadata():
    messages = []

    async def send(message):
        messages.append(message)

    await _send_401(send, "invalid OAuth token", _settings(), "/mcp/telegram")
    assert messages[0]["status"] == 401
    headers = dict(messages[0]["headers"])
    assert b'scope="mcp:tools"' in headers[b"www-authenticate"]
    assert (
        b"https://mcp.example.test/.well-known/oauth-protected-resource/mcp/telegram"
        in (headers[b"www-authenticate"])
    )
