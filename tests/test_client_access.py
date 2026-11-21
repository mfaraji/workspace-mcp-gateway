"""Client grants: ownership, discovery, browser trust boundary and direct-call enforcement."""

from __future__ import annotations

import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gateway.access import policy, routes
from gateway.db.models import ClientGrant, User
from gateway.identity.models import AuthenticatedUser, IdentityError
from gateway.mcp.context import _current_user
from gateway.mcp.server import build_mcp, product_tool_filter
from gateway.providers.registry import ToolError
from tests.test_clerk_oauth import _settings


@pytest.fixture
def store(monkeypatch):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    User.__table__.create(engine)
    ClientGrant.__table__.create(engine)

    @contextmanager
    def scope():
        with Session(engine, expire_on_commit=False) as session:
            yield session
            session.commit()

    monkeypatch.setattr(routes, "session_scope", scope)
    monkeypatch.setattr(routes, "get_settings", _settings)
    with scope() as session:
        alice = User(external_user_id="alice")
        bob = User(external_user_id="bob")
        session.add_all([alice, bob])
        session.flush()
    return scope, alice.id, bob.id


def auth(client="client-a", user="alice"):
    return AuthenticatedUser(user, source="oauth", clerk_subject=user, oauth_client_id=client)


def request(method="GET", client="client-a", user="alice", csrf=True):
    settings = _settings()
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/api/access",
            "headers": [(b"x-csrf-token", b"csrf")] if csrf else [],
            "session": {
                "identity": {
                    "issuer": settings.clerk_issuer,
                    "sub": user,
                    "external": user,
                    "client": "browser",
                    "expires": time.time() + 600,
                },
                "csrf": "csrf",
                "target": {"sub": user, "client": client, "resource": f"{settings.base_url}/mcp"},
            },
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    access_catalog=[
                        {"slug": "calendar", "tools": [{"name": "google_calendar_list_events"}]}
                    ]
                )
            ),
        }
    )


def test_grants_isolate_users_clients_and_revocation(store):
    scope, alice, bob = store
    with scope() as session:
        session.add(
            ClientGrant(
                user_id=alice,
                issuer=_settings().clerk_issuer,
                client_id="client-a",
                tools=["google_calendar_list_events"],
            )
        )
    with scope() as session:
        spec = SimpleNamespace(name="google_calendar_list_events")
        policy.enforce(session, alice, _settings(), auth(), spec)
        for owner, identity in [(alice, auth("client-b")), (bob, auth(user="bob"))]:
            with pytest.raises(ToolError, match="Choose client permissions") as exc:
                policy.enforce(session, owner, _settings(), identity, spec)
            assert exc.value.error_code == "access_setup_required"
        with pytest.raises(ToolError) as exc:
            policy.enforce(
                session,
                alice,
                _settings(),
                auth(),
                SimpleNamespace(name="google_calendar_delete_event"),
            )
        assert exc.value.error_code == "permission_denied"
        policy.grant_for(session, alice, _settings(), "client-a").revoked = True
    with scope() as session:
        with pytest.raises(ToolError) as exc:
            policy.enforce(session, alice, _settings(), auth(), spec)
        assert exc.value.error_code == "permission_denied"


def test_save_validate_csrf_revoke_and_do_not_self_grant(store):
    scope, alice, _ = store
    body = routes.GrantBody(tools=["google_calendar_list_events"])
    with pytest.raises(HTTPException) as exc:
        routes.save("client-a", body, request("PUT", csrf=False))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        routes.save("client-b", body, request("PUT"))
    with pytest.raises(HTTPException):
        routes.save("client-a", routes.GrantBody(tools=["unknown"]), request("PUT"))
    routes.save("client-a", body, request("PUT"))
    with scope() as session:
        assert policy.allowed_tools(session, alice, _settings(), auth()) == set(body.tools)
    routes.revoke("client-a", request("DELETE"))
    with scope() as session:
        assert policy.allowed_tools(session, alice, _settings(), auth()) == set()


def test_browser_required_even_with_mcp_token():
    req = Request(
        {
            "type": "http",
            "method": "PUT",
            "headers": [(b"authorization", b"Bearer oauth-token")],
            "session": {},
        }
    )
    with pytest.raises(HTTPException) as exc:
        routes.save("client", routes.GrantBody(tools=[]), req)
    assert exc.value.status_code == 401


def test_setup_link_is_subject_bound_and_cannot_save_by_itself(store):
    url = policy.setup_url(_settings(), auth())
    ticket = url.split("ticket=")[1]
    payload = URLSafeTimedSerializer(_settings().session_secret, salt="client-setup").loads(ticket)
    assert payload == {
        "sub": "alice",
        "client": "client-a",
        "resource": f"{_settings().base_url}/mcp",
    }
    req = request(user="bob")
    routes.page(req, ticket)
    with pytest.raises(HTTPException) as exc:
        routes.page(req)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_discovery_respects_grants_and_mounts_and_refreshes(store, monkeypatch):
    scope, alice, _ = store
    monkeypatch.setattr("gateway.db.engine.session_scope", scope)
    mcp = build_mcp(_settings(), product_tool_filter("calendar"))
    token = _current_user.set(auth())
    try:
        assert {t.name for t in await mcp.list_tools()} == {"system_get_current_time"}
        with scope() as session:
            session.add(
                ClientGrant(
                    user_id=alice,
                    issuer=_settings().clerk_issuer,
                    client_id="client-a",
                    tools=["google_calendar_list_events", "telegram_send_message"],
                )
            )
        assert {t.name for t in await mcp.list_tools()} == {
            "system_get_current_time",
            "google_calendar_list_events",
        }
        routes.revoke("client-a", request("DELETE"))
        assert {t.name for t in await mcp.list_tools()} == {"system_get_current_time"}
    finally:
        _current_user.reset(token)


def test_expired_browser_session_rejected(monkeypatch):
    monkeypatch.setattr(routes, "get_settings", _settings)
    req = request()
    req.session["identity"]["expires"] = 0
    with pytest.raises(IdentityError):
        routes.browser_user(req)


def test_direct_write_denial_precedes_confirmation_and_is_audited(store, monkeypatch):
    from pydantic import BaseModel
    from sqlalchemy import select

    from gateway.db.models import ToolAuditLog
    from gateway.providers import registry
    from gateway.providers.base import RiskLevel, ToolSpec

    scope, _, _ = store
    with scope() as session:
        ToolAuditLog.__table__.create(session.bind)
    monkeypatch.setattr(registry, "session_scope", scope)
    called = []
    spec = ToolSpec(
        name="google_calendar_delete_event",
        provider="google",
        risk=RiskLevel.MUTATING,
        description="Delete",
        input_model=BaseModel,
        handler=lambda *_: called.append(True),
    )
    token = _current_user.set(auth())
    try:
        with pytest.raises(ToolError) as exc:
            registry._run_pipeline(spec, _settings(), {}, "previous-confirmation-token")
        assert exc.value.error_code == "access_setup_required"
    finally:
        _current_user.reset(token)
    assert called == []
    with scope() as session:
        rows = list(session.scalars(select(ToolAuditLog)))
        assert len(rows) == 1
        assert rows[0].error_code == "access_setup_required"


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": "alice"},
        {"sub": "alice", "client_id": 123},
        {"sub": "alice", "client_id": "a", "azp": "b"},
    ],
)
def test_verified_tokens_must_identify_one_client(store, monkeypatch, claims):
    from gateway.identity.resolver import resolve_identity

    monkeypatch.setattr("gateway.oauth.clerk.verify_access_claims", lambda *_: claims)
    scope, _, _ = store
    with scope() as session, pytest.raises(IdentityError):
        resolve_identity({"Authorization": "Bearer jwt"}, _settings(), session)


def test_browser_login_uses_pkce_and_checks_callback_state(monkeypatch):
    from urllib.parse import parse_qs, urlparse

    settings = _settings(clerk_browser_client_id="browser", clerk_browser_client_secret="secret")
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "authorization_endpoint", lambda _: "https://clerk.test/authorize")
    req = request()
    response = routes.login(req)
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [req.session["login"]["state"]]
    assert "code_verifier" not in query
    with pytest.raises(HTTPException) as exc:
        routes.callback(req, state="wrong", code="code")
    assert exc.value.status_code == 400
    assert "login" not in req.session


def telegram_request(method="GET"):
    req = request(method)
    req.session["target"]["resource"] = f"{_settings().base_url}/mcp/telegram"
    req.app.state.access_catalog.append(
        {
            "slug": "telegram",
            "name": "Telegram",
            "tools": [
                {
                    "name": "telegram_list_destinations",
                    "risk": "read",
                    "description": "List destinations",
                },
                {
                    "name": "telegram_send_message",
                    "risk": "mutating",
                    "description": "Send a message",
                },
            ],
        }
    )
    return req


def test_telegram_catalog_only_returns_requested_connector_and_client(store):
    scope, alice, _ = store
    with scope() as session:
        session.add(
            ClientGrant(
                user_id=alice,
                issuer=_settings().clerk_issuer,
                client_id="client-a",
                tools=["google_calendar_list_events", "telegram_list_destinations"],
            )
        )
        session.add(
            ClientGrant(
                user_id=alice,
                issuer=_settings().clerk_issuer,
                client_id="other-client",
                tools=["google_calendar_list_events"],
            )
        )
    result = routes.catalog(telegram_request())
    assert result["scoped"] is True
    assert [c["slug"] for c in result["connectors"]] == ["telegram"]
    assert result["grants"] == [
        {"client_id": "client-a", "tools": ["telegram_list_destinations"], "revoked": False}
    ]


def test_scoped_save_rejects_other_connectors_and_preserves_their_grants(store):
    scope, alice, _ = store
    with scope() as session:
        session.add(
            ClientGrant(
                user_id=alice,
                issuer=_settings().clerk_issuer,
                client_id="client-a",
                tools=["google_calendar_list_events"],
            )
        )
    req = telegram_request("PUT")
    with pytest.raises(HTTPException) as exc:
        routes.save("client-a", routes.GrantBody(tools=["google_calendar_list_events"]), req)
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        routes.save("other-client", routes.GrantBody(tools=[]), req)
    assert exc.value.status_code == 403
    routes.save("client-a", routes.GrantBody(tools=["telegram_list_destinations"]), req)
    with scope() as session:
        assert set(policy.grant_for(session, alice, _settings(), "client-a").tools) == {
            "google_calendar_list_events",
            "telegram_list_destinations",
        }
    routes.revoke("client-a", telegram_request("DELETE"))
    with scope() as session:
        grant = policy.grant_for(session, alice, _settings(), "client-a")
        assert grant.tools == ["google_calendar_list_events"]
        assert not grant.revoked


def test_resource_bound_links_and_legacy_tickets(store):
    from dataclasses import replace

    identity = replace(auth(), oauth_resource=f"{_settings().base_url}/mcp/telegram")
    ticket = policy.setup_url(_settings(), identity).split("ticket=")[1]
    req = request()
    routes.page(req, ticket)
    assert req.session["target"]["resource"].endswith("/mcp/telegram")
    legacy = URLSafeTimedSerializer(_settings().session_secret, salt="client-setup").dumps(
        {"sub": "alice", "client": "client-a"}
    )
    old_request = request()
    assert routes.page(old_request, legacy).status_code == 303
    # Old links show the styled page, but cannot read or change permissions.
    with pytest.raises(HTTPException) as exc:
        routes.catalog(old_request)
    assert exc.value.status_code == 400
    req.session["target"].pop("resource")
    with pytest.raises(HTTPException):
        routes.catalog(req)


@pytest.mark.parametrize(
    "resource", ["https://evil.test/mcp/telegram", "https://mcp.example.test/mcp/unknown"]
)
def test_resource_scope_cannot_be_forged(store, resource):
    req = telegram_request()
    req.session["target"]["resource"] = resource
    with pytest.raises(HTTPException):
        routes.save("client-a", routes.GrantBody(tools=[]), req)

