"""Tests for Google OAuth flow construction."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from gateway.config import Settings
from gateway.oauth.google import build_flow
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


def test_relax_scope_check_restores_environment(monkeypatch):
    monkeypatch.delenv("OAUTHLIB_RELAX_TOKEN_SCOPE", raising=False)

    with _relax_oauthlib_scope_check():
        import os

        assert os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] == "1"

    import os

    assert "OAUTHLIB_RELAX_TOKEN_SCOPE" not in os.environ
