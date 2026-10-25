"""OAuth resource metadata, token validation and tool declarations."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from gateway.app import create_app
from gateway.config import Settings
from gateway.db.models import OAuthIdentityLink, OAuthLinkIntent, User
from gateway.identity.models import IdentityError
from gateway.identity.resolver import get_or_create_user, resolve_identity
from gateway.mcp.context import _send_401
from gateway.mcp.server import build_mcp, product_tool_filter
from gateway.oauth import clerk, clerk_routes
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
    paths = {route.path for route in app.routes}
    assert "/.well-known/oauth-protected-resource/mcp/{product}" in paths


@pytest.mark.asyncio
async def test_tools_advertise_oauth_when_enabled():
    mcp = build_mcp(_settings(), product_tool_filter("telegram"))
    tools = await mcp.list_tools()
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


def test_link_flow_is_single_use_and_unlink_revokes_mapping(monkeypatch):
    settings = _settings(clerk_link_client_id="link-client", clerk_link_client_secret="secret")
    engine = create_engine("sqlite+pysqlite:///:memory:")
    User.__table__.create(engine)
    OAuthIdentityLink.__table__.create(engine)
    OAuthLinkIntent.__table__.create(engine)

    @contextmanager
    def test_session_scope():
        with Session(engine) as session:
            yield session
            session.commit()

    monkeypatch.setattr(clerk_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(clerk_routes, "session_scope", test_session_scope)
    monkeypatch.setattr(
        clerk_routes,
        "authorization_endpoint",
        lambda _: "https://clerk.example.test/oauth/authorize",
    )
    monkeypatch.setattr(
        clerk_routes, "token_endpoint", lambda _: "https://clerk.example.test/oauth/token"
    )
    monkeypatch.setattr(
        clerk_routes, "verify_access_token", lambda *_args, **_kwargs: "clerk-user-1"
    )
    monkeypatch.setattr(
        clerk_routes.httpx,
        "post",
        lambda *_args, **_kwargs: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"access_token": "test-token"},
        ),
    )
    request = SimpleNamespace(
        headers={"x-gateway-auth": "shared-secret", "x-openwebui-user-id": "alice"}
    )

    started = clerk_routes.link_start(request)
    url = json.loads(started.body)["url"]
    query = parse_qs(urlparse(url).query)
    state = query["state"][0]
    assert query["resource"] == ["https://mcp.example.test/mcp"]
    assert query["code_challenge_method"] == ["S256"]
    assert clerk_routes.link_callback("code-1", state).status_code == 200
    assert json.loads(clerk_routes.link_status(request).body)["linked"] is True
    assert clerk_routes.link_callback("code-1", state).status_code == 400
    with Session(engine) as session:
        link = session.scalar(select(OAuthIdentityLink))
        assert link.subject == "clerk-user-1"
        assert link.user.external_user_id == "alice"
        monkeypatch.setattr(clerk, "verify_access_token", lambda *_args, **_kwargs: "clerk-user-1")
        auth = resolve_identity(
            {"Authorization": "Bearer oauth-token"},
            settings,
            session,
            oauth_resource="https://mcp.example.test/mcp",
        )
        assert auth.external_user_id == "alice"
        assert auth.source == "oauth"
    assert json.loads(clerk_routes.link_remove(request).body)["linked"] is False
    assert json.loads(clerk_routes.link_status(request).body)["linked"] is False
    with Session(engine) as session:
        unlinked = resolve_identity(
            {"Authorization": "Bearer oauth-token"},
            settings,
            session,
            oauth_resource="https://mcp.example.test/mcp",
        )
        assert unlinked.external_user_id.startswith("oauth:")
        assert unlinked.external_user_id != "alice"
        get_or_create_user(session, unlinked)
        session.commit()
    with Session(engine) as session:
        assert session.scalar(select(User).where(User.external_user_id == "alice")) is not None
        direct_user = session.scalar(
            select(User).where(User.external_user_id == unlinked.external_user_id)
        )
        assert direct_user is not None
    pending_url = json.loads(clerk_routes.link_start(request).body)["url"]
    pending_state = parse_qs(urlparse(pending_url).query)["state"][0]
    clerk_routes.link_remove(request)
    assert clerk_routes.link_callback("code-2", pending_state).status_code == 400


def test_link_start_rejects_untrusted_public_request():
    request = SimpleNamespace(headers={"x-openwebui-user-id": "alice"})
    assert clerk_routes.link_start(request).status_code == 401


def test_clerk_subject_maps_to_same_user_across_oauth_clients(monkeypatch):
    settings = _settings()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    User.__table__.create(engine)
    OAuthIdentityLink.__table__.create(engine)
    monkeypatch.setattr(clerk, "verify_access_token", lambda *_args, **_kwargs: "clerk-user-1")

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
