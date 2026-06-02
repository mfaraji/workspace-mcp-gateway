"""Tests for provider connection guidance returned by the registry."""

from __future__ import annotations

from pydantic import BaseModel

from gateway.config import Settings
from gateway.providers.base import RiskLevel, ToolSpec
from gateway.providers.registry import ToolError, _connect_required_result


class _Input(BaseModel):
    """No parameters."""


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


def test_google_connect_failure_returns_user_visible_authorization_url():
    spec = ToolSpec(
        name="google_tasks_list_tasklists",
        provider="google",
        risk=RiskLevel.READ,
        description="test",
        input_model=_Input,
        handler=lambda _model, _ctx, _session: {},
    )

    result = _connect_required_result(
        spec,
        _settings(),
        "alice",
        ToolError("reauth_required", "Google Tasks connection is missing required scopes"),
    )

    assert result is not None
    assert "Authorization required for Google Tasks." in result
    assert "Open this authorization URL" in result
    assert "https://mcp.ashpazi.shop/oauth/google/start?" in result
    assert "product=tasks" in result
    assert "Status: reauth_required" in result
    assert "Tool: google_tasks_list_tasklists" in result
    assert "Do not create a Calendar event as a fallback" in result


def test_non_connection_failure_stays_unstructured():
    spec = ToolSpec(
        name="google_tasks_list_tasklists",
        provider="google",
        risk=RiskLevel.READ,
        description="test",
        input_model=_Input,
        handler=lambda _model, _ctx, _session: {},
    )

    result = _connect_required_result(
        spec,
        _settings(),
        "alice",
        ToolError("invalid_input", "bad input"),
    )

    assert result is None
