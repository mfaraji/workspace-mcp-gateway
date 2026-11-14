"""Gateway-native consent: interactive sessions, scoped choices and OAuth boundaries."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from itsdangerous import URLSafeTimedSerializer
from starlette.requests import Request

from gateway.access import consent
from gateway.access.policy import grant_for
from tests.test_clerk_oauth import _settings
from tests.test_client_access import store  # noqa: F401


def req(token="token", origin="https://mcp.example.test"):
    return Request(
        {
            "type": "http",
            "headers": [
                (b"origin", origin.encode()),
                (b"authorization", ("Bearer " + token).encode()),
            ],
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    access_catalog=[
                        {
                            "slug": "telegram",
                            "name": "Telegram",
                            "tools": [
                                {
                                    "name": "telegram_list_destinations",
                                    "risk": "read",
                                    "description": "List",
                                },
                                {
                                    "name": "telegram_send_message",
                                    "risk": "mutating",
                                    "description": "Send",
                                },
                            ],
                        },
                        {
                            "slug": "calendar",
                            "name": "Calendar",
                            "tools": [
                                {
                                    "name": "google_calendar_list_events",
                                    "risk": "read",
                                    "description": "List",
                                }
                            ],
                        },
                    ]
                )
            ),
        }
    )


@pytest.fixture
def browser_key(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(consent, "_issuer_endpoint", lambda *_: "https://clerk.example.test/jwks")
    monkeypatch.setattr(
        consent,
        "_jwk_client",
        lambda _: SimpleNamespace(
            get_signing_key_from_jwt=lambda _: SimpleNamespace(key=key.public_key())
        ),
    )
    return key


def claims(**overrides):
    return {
        "iss": "https://clerk.example.test",
        "sub": "user_consent",
        "sid": "sess_browser",
        "exp": int(time.time()) + 60,
        "azp": "https://mcp.example.test",
        "aud": "browser",
        **overrides,
    }


def test_only_interactive_session_can_approve(browser_key):
    settings = _settings(clerk_session_audience="browser")
    token = jwt.encode(claims(), browser_key, algorithm="RS256")
    assert consent.browser_subject(req(token), settings) == "user_consent"
    with pytest.raises(HTTPException) as exc:
        consent.browser_subject(req(token, origin="https://evil.test"), settings)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "change",
    [
        {"sid": ""},
        {"sub": ""},
        {"azp": "https://evil.test"},
        {"aud": "https://mcp.example.test/mcp/telegram"},
        {"exp": 1},
        {"scope": "mcp:tools"},
        {"client_id": "client-a"},
        {"iss": "https://evil.test"},
    ],
)
def test_mcp_tokens_and_invalid_sessions_cannot_self_grant(browser_key, change):
    token = jwt.encode(claims(**change), browser_key, algorithm="RS256")
    with pytest.raises(HTTPException) as exc:
        consent.browser_subject(req(token), _settings(clerk_session_audience="browser"))
    assert exc.value.status_code == 401


@pytest.fixture
def env(store, monkeypatch):  # noqa: F811
    scope, alice, bob = store
    settings = _settings()
    monkeypatch.setattr(consent, "get_settings", lambda: settings)
    monkeypatch.setattr(consent, "session_scope", scope)
    monkeypatch.setattr(consent, "browser_subject", lambda *_: "user_consent")
    monkeypatch.setattr(consent, "registered_client", lambda *_: None)
    from gateway.db.models import User

    monkeypatch.setattr(consent, "user_for_subject", lambda session, *_: session.get(User, alice))
    return scope, alice, bob, settings


def context_request():
    return consent.ConsentContext(
        client_id="client-a",
        resource="https://mcp.example.test/mcp/telegram",
        redirect_uri="https://webui.example.test/callback",
        scope="openid mcp:tools",
    )


def test_permission_choices_precede_oauth_completion(env):
    scope, alice, _, settings = env
    result = json.loads(consent.context(context_request(), req()).body)
    assert [c["slug"] for c in result["connectors"]] == ["telegram"]
    with scope() as session:
        assert grant_for(session, alice, settings, "client-a") is None
    assert consent.approve(
        consent.ConsentApproval(intent=result["intent"], tools=["telegram_list_destinations"]),
        req(),
    ) == {"saved": True}
    with scope() as session:
        assert grant_for(session, alice, settings, "client-a").tools == [
            "telegram_list_destinations"
        ]


def test_scope_and_owner_binding(env):
    result = json.loads(consent.context(context_request(), req()).body)
    intent = result["intent"]
    for bad_tools in [["google_calendar_list_events"], ["unknown"]]:
        with pytest.raises(HTTPException):
            consent.approve(consent.ConsentApproval(intent=intent, tools=bad_tools), req())
    settings = env[3]
    payload = URLSafeTimedSerializer(settings.session_secret, salt=consent._INTENT_SALT).loads(
        intent
    )
    payload["sub"] = "other-user"
    other = URLSafeTimedSerializer(settings.session_secret, salt=consent._INTENT_SALT).dumps(
        payload
    )
    for bad_intent in [other, intent + "tampered"]:
        with pytest.raises(HTTPException):
            consent.approve(consent.ConsentApproval(intent=bad_intent, tools=[]), req())


def test_expired_consent_cannot_be_saved(env):
    from unittest.mock import patch

    settings = env[3]
    payload = {"sub": "user_consent", **context_request().model_dump()}
    with patch("itsdangerous.timed.time.time", return_value=time.time() - 700):
        intent = URLSafeTimedSerializer(settings.session_secret, salt=consent._INTENT_SALT).dumps(
            payload
        )
    with pytest.raises(HTTPException):
        consent.approve(consent.ConsentApproval(intent=intent, tools=[]), req())


def test_validation_rejects_unregistered_redirects_and_scopes(monkeypatch):
    app = {
        "client_id": "client-a",
        "redirect_uris": ["https://webui.example.test/callback"],
        "scopes": "openid mcp:tools",
        "consent_screen_enabled": True,
    }

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, *args, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"data": [app]})

    monkeypatch.setattr(consent.httpx, "Client", Client)
    settings = _settings(clerk_backend_api_key="test-key")
    consent.registered_client(
        settings, "client-a", "https://webui.example.test/callback", "openid mcp:tools"
    )
    for client, redirect, scope in [
        ("other", "https://webui.example.test/callback", "openid mcp:tools"),
        ("client-a", "https://evil.test/callback", "mcp:tools"),
        ("client-a", "https://webui.example.test/callback", "openid mcp:tools admin"),
        ("client-a", "https://webui.example.test/callback", "openid"),
    ]:
        with pytest.raises(HTTPException):
            consent.registered_client(settings, client, redirect, scope)


def test_access_logs_strip_oauth_query_credentials():
    import logging

    from gateway.logging import _NoQueryAccessLog

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", "/oauth/consent?code=secret&__clerk_db_jwt=secret", "1.1", 200),
        None,
    )
    assert _NoQueryAccessLog().filter(record)
    assert record.args[2] == "/oauth/consent"
    assert "secret" not in record.getMessage()


def test_shared_consent_never_creates_or_accepts_tool_grants(env):
    scope, alice, _, settings = env
    request = req()
    request.app.state.access_catalog.append(
        {
            "slug": "finance",
            "name": "Finance",
            "access_mode": "authenticated_shared",
            "tools": [{"name": "finance_list_currencies", "risk": "read"}],
        }
    )
    body = context_request().model_copy(update={"resource": settings.base_url + "/mcp/finance"})
    result = json.loads(consent.context(body, request).body)
    assert result["tools"] == []
    assert result["connectors"][0]["access_mode"] == "authenticated_shared"
    with pytest.raises(HTTPException) as exc:
        consent.approve(
            consent.ConsentApproval(intent=result["intent"], tools=["finance_list_currencies"]),
            request,
        )
    assert exc.value.status_code == 400
    consent.approve(consent.ConsentApproval(intent=result["intent"], tools=[]), request)
    with scope() as session:
        assert grant_for(session, alice, settings, "client-a") is None


def test_browser_config_identifies_shared_resource(env, monkeypatch):
    settings = env[3].model_copy(update={"clerk_publishable_key": "pk_test_example"})
    monkeypatch.setattr(consent, "get_settings", lambda: settings)
    request = req()
    request.app.state.access_catalog.append(
        {
            "slug": "finance",
            "access_mode": "authenticated_shared",
            "tools": [],
        }
    )
    assert json.loads(consent.browser_config(request).body)["shared_resources"] == [
        settings.base_url + "/mcp/finance"
    ]
