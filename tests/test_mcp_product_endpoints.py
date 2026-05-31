"""Tests for product-specific MCP tool registration and auth mounting."""

from __future__ import annotations

from typing import cast

from gateway.app import create_app
from gateway.config import Settings, get_settings
from gateway.mcp.context import IdentityMiddleware
from gateway.mcp.server import ProductEndpoint, build_mcp, product_tool_filter


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
        base_url="http://localhost:8000",
        google_client_id="cid",
        google_client_secret="secret",
        token_encryption_key="x" * 43 + "=",
        gateway_shared_secret="shared-secret-value",
        trusted_open_webui_origin="https://openwebui.internal",
        session_secret="sess",
    )


def _tool_names(settings: Settings, product: str | None = None) -> set[str]:
    mcp = (
        build_mcp(settings, product_tool_filter(cast(ProductEndpoint, product)))
        if product
        else build_mcp(settings)
    )
    return set(mcp._tool_manager._tools)


def test_product_filters_register_expected_calendar_tools():
    names = _tool_names(_settings(), "calendar")

    assert "system_get_current_time" in names
    assert "google_calendar_list_events" in names
    assert "google_calendar_create_event" in names
    assert all(
        name == "system_get_current_time" or name.startswith("google_calendar_")
        for name in names
    )


def test_drive_and_tasks_filters_are_system_only_until_providers_exist():
    settings = _settings()

    assert _tool_names(settings, "drive") == {"system_get_current_time"}
    assert _tool_names(settings, "tasks") == {"system_get_current_time"}


def test_backward_compatible_mcp_endpoint_still_registers_all_enabled_tools():
    names = _tool_names(_settings())

    assert "system_get_current_time" in names
    assert "google_calendar_list_calendars" in names
    assert "google_calendar_delete_event" in names


def test_every_mcp_mount_is_behind_identity_middleware(monkeypatch):
    settings = _settings()
    get_settings.cache_clear()
    monkeypatch.setattr("gateway.app.get_settings", lambda: settings)

    app = create_app()
    mounted = {route.path: route.app for route in app.routes if hasattr(route, "app")}

    for path in ("/mcp", "/mcp/calendar", "/mcp/drive", "/mcp/tasks"):
        assert isinstance(mounted[path], IdentityMiddleware)

    get_settings.cache_clear()


def test_product_mcp_no_slash_redirect_route_exists(monkeypatch):
    settings = _settings()
    get_settings.cache_clear()
    monkeypatch.setattr("gateway.app.get_settings", lambda: settings)

    app = create_app()
    route = next(route for route in app.routes if getattr(route, "path", None) == "/mcp/{product}")

    assert {"GET", "POST", "DELETE"}.issubset(route.methods)

    get_settings.cache_clear()
