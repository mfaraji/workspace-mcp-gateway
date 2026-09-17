"""Tests for the Telegram Bot API provider."""

from __future__ import annotations

import uuid

import httpx
import pytest

from gateway.config import Settings
from gateway.providers.base import CallContext, RiskLevel
from gateway.providers.registry import ToolError, ToolRegistry
from gateway.providers.telegram import auth as telegram_auth
from gateway.providers.telegram import client as telegram_client
from gateway.providers.telegram import write as telegram_write


def _settings(**overrides) -> Settings:
    base = dict(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
        base_url="http://localhost:8000",
        google_client_id="cid",
        google_client_secret="secret",
        token_encryption_key="x" * 43 + "=",
        gateway_shared_secret="shared-secret-value",
        trusted_open_webui_origin="https://openwebui.internal",
        session_secret="sess",
        telegram_bot_token="123:ABC",
        telegram_require_confirmation=True,
    )
    base.update(overrides)
    return Settings(**base)


def _ctx() -> CallContext:
    return CallContext(user_id=uuid.uuid4(), external_user_id="user-1", request_id="req-1")


class _FakeResponse:
    def __init__(self, status_code: int, json_body=None, text: str = ""):
        self.status_code = status_code
        self._json_body = json_body
        self.content = text.encode() if json_body is None else b"x"
        self.text = text

    def json(self):
        return self._json_body


def test_telegram_request_posts_to_bot_url_and_returns_json(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured.update(url=url, json=json, timeout=timeout)
        return _FakeResponse(200, json_body={"ok": True, "result": {"message_id": 1}})

    monkeypatch.setattr(telegram_client.httpx, "post", fake_post)

    result = telegram_client.telegram_request(
        _settings(), "123:TOKEN", "sendMessage", json={"chat_id": "@ch", "text": "hi"}
    )

    assert result == {"ok": True, "result": {"message_id": 1}}
    assert captured["url"] == "https://api.telegram.org/bot123:TOKEN/sendMessage"
    assert captured["json"] == {"chat_id": "@ch", "text": "hi"}


@pytest.mark.parametrize(
    "status,expected_code",
    [
        (400, "invalid_input"),
        (401, "unauthorized"),
        (403, "forbidden"),
        (404, "not_found"),
        (429, "rate_limited"),
        (500, "provider_error"),
    ],
)
def test_telegram_request_classifies_http_errors(monkeypatch, status, expected_code):
    monkeypatch.setattr(
        telegram_client.httpx,
        "post",
        lambda *a, **k: _FakeResponse(status, text="boom"),
    )

    with pytest.raises(ToolError) as exc_info:
        telegram_client.telegram_request(_settings(), "123:TOKEN", "sendMessage")

    assert exc_info.value.error_code == expected_code


def test_telegram_request_raises_on_ok_false(monkeypatch):
    body = {"ok": False, "description": "chat not found"}

    def fake_post(*a, **k):
        return _FakeResponse(200, json_body=body)

    monkeypatch.setattr(telegram_client.httpx, "post", fake_post)

    with pytest.raises(ToolError) as exc_info:
        telegram_client.telegram_request(_settings(), "123:TOKEN", "sendMessage")

    assert exc_info.value.error_code == "provider_error"
    assert "chat not found" in str(exc_info.value)


def test_telegram_request_wraps_transport_errors(monkeypatch):
    def raise_error(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(telegram_client.httpx, "post", raise_error)

    with pytest.raises(ToolError) as exc_info:
        telegram_client.telegram_request(_settings(), "123:TOKEN", "sendMessage")

    assert exc_info.value.error_code == "provider_error"


def test_send_message_builds_send_message_body(monkeypatch):
    captured = {}

    def fake_telegram_request(settings, bot_token, method, json=None):
        captured.update(settings=settings, bot_token=bot_token, method=method, json=json)
        return {"ok": True, "result": {"message_id": 42}}

    monkeypatch.setattr(telegram_write, "telegram_request", fake_telegram_request)
    monkeypatch.setattr(telegram_write, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        telegram_client.TELEGRAM_API_KEY,
        "get_credentials",
        lambda session, ctx, settings: "123:TOKEN",
    )

    args = telegram_write.SendMessageInput(
        chat_id="@mychannel",
        text="Hello",
        parse_mode="HTML",
        disable_notification=True,
    )
    result = telegram_write.send_message(args, _ctx(), None)

    assert result == {"ok": True, "result": {"message_id": 42}}
    assert captured["bot_token"] == "123:TOKEN"
    assert captured["method"] == "sendMessage"
    assert captured["json"] == {
        "chat_id": "@mychannel",
        "text": "Hello",
        "disable_notification": True,
        "parse_mode": "HTML",
    }


def test_telegram_set_api_key_tool_stores_the_key(monkeypatch):
    stored = {}

    def fake_store_api_key(_session, user_id, provider, api_key):
        stored.update(user_id=user_id, provider=provider, api_key=api_key)

    monkeypatch.setattr(telegram_auth, "store_api_key", fake_store_api_key)
    ctx = _ctx()

    result = telegram_auth.set_api_key(
        telegram_auth.SetApiKeyInput(api_key="123:ABC"), ctx, None
    )

    assert result == {"status": "connected", "provider": "telegram"}
    assert stored == {
        "user_id": ctx.user_id,
        "provider": "telegram",
        "api_key": "123:ABC",
    }


@pytest.mark.parametrize(
    "require_confirmation,expected_risk",
    [(True, RiskLevel.MUTATING), (False, RiskLevel.READ)],
)
def test_send_message_registered_with_configurable_risk(
    monkeypatch, require_confirmation, expected_risk
):
    monkeypatch.setattr(
        telegram_write,
        "get_settings",
        lambda: _settings(telegram_require_confirmation=require_confirmation),
    )

    settings = _settings(telegram_require_confirmation=require_confirmation)
    registry = ToolRegistry()
    telegram_auth.register(registry)
    telegram_write.register(registry, settings)
    specs = {spec.name: spec for spec in registry._specs}

    assert set(specs) == {"telegram_set_api_key", "telegram_send_message"}
    assert specs["telegram_set_api_key"].risk is RiskLevel.READ
    assert specs["telegram_send_message"].risk is expected_risk
    if expected_risk is RiskLevel.MUTATING:
        assert specs["telegram_send_message"].preview_builder is not None
    else:
        assert specs["telegram_send_message"].preview_builder is None
