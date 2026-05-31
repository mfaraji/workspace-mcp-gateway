"""Tests for Google OAuth flow construction."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from gateway.config import Settings
from gateway.oauth.google import (
    CALENDAR_SCOPES,
    DEFAULT_SCOPES,
    build_flow,
    build_start_url,
    scopes_for_product,
)
from gateway.oauth.routes import _relax_oauthlib_scope_check


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


def test_build_flow_does_not_generate_pkce_challenge():
    flow = build_flow(_settings(), state="signed-state")

    authorization_url, _ = flow.authorization_url()
    params = parse_qs(urlparse(authorization_url).query)

    assert "code_challenge" not in params
    assert "code_challenge_method" not in params


def test_product_scopes_are_calendar_only_until_other_providers_are_enabled():
    assert scopes_for_product(None) == DEFAULT_SCOPES
    assert scopes_for_product("calendar") == ["openid", "email", "profile", *CALENDAR_SCOPES]

    with pytest.raises(ValueError, match="Google drive tools are not enabled"):
        scopes_for_product("drive")


def test_product_start_url_carries_calendar_selector():
    url = build_start_url(_settings(), "alice", product="calendar")
    params = parse_qs(urlparse(url).query)

    assert url.startswith("https://mcp.ashpazi.shop/oauth/google/start?")
    assert params["product"] == ["calendar"]
    assert "ticket" in params


def test_relax_scope_check_restores_environment(monkeypatch):
    monkeypatch.delenv("OAUTHLIB_RELAX_TOKEN_SCOPE", raising=False)

    with _relax_oauthlib_scope_check():
        import os

        assert os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] == "1"

    import os

    assert "OAUTHLIB_RELAX_TOKEN_SCOPE" not in os.environ
