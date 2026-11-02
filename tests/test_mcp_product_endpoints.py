"""Tests for product-specific MCP tool registration and auth mounting."""

from __future__ import annotations

from gateway.app import create_app
from gateway.config import Settings, get_settings
from gateway.mcp.context import IdentityMiddleware
from gateway.mcp.server import build_mcp, product_tool_filter


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
        base_url="http://localhost:8000",
        google_client_id="cid",
        google_client_secret="secret",
        apex_api_key="pk_live_test",
        token_encryption_key="x" * 43 + "=",
        gateway_shared_secret="shared-secret-value",
        trusted_open_webui_origin="https://openwebui.internal",
        session_secret="sess",
    )


def _tool_names(settings: Settings, product: str | None = None) -> set[str]:
    mcp = build_mcp(settings, product_tool_filter(product)) if product else build_mcp(settings)
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


def test_drive_filter_registers_drive_read_tools():
    settings = _settings()

    assert _tool_names(settings, "drive") == {
        "system_get_current_time",
        "google_drive_search_files",
        "google_drive_get_file_metadata",
    }


def test_tasks_filter_registers_expected_tasks_tools():
    names = _tool_names(_settings(), "tasks")

    assert "system_get_current_time" in names
    assert "google_tasks_list_tasklists" in names
    assert "google_tasks_create_task" in names
    assert "google_tasks_delete_task" in names
    assert all(
        name == "system_get_current_time" or name.startswith("google_tasks_")
        for name in names
    )


def test_apex_filter_registers_expected_apex_tools():
    names = _tool_names(_settings(), "apex")

    assert names == {
        "system_get_current_time",
        "apex_list_clients",
        "apex_list_timesheets",
        "apex_get_last_timesheet",
        "apex_create_invoice",
        "apex_create_timesheet",
        "apex_update_timesheet",
        "apex_set_api_key",
        "apex_list_workspaces",
        "apex_set_workspace",
    }


def test_apex_wealth_filter_registers_only_wealth_tools():
    names = _tool_names(_settings(), "apex-wealth")

    assert names == {
        "system_get_current_time",
        "apex_wealth_list_accounts",
        "apex_wealth_get_accounts_summary",
        "apex_wealth_get_investments_summary",
        "apex_wealth_get_positions",
        "apex_wealth_list_manual_holdings",
        "apex_wealth_get_net_worth_history",
        "apex_wealth_set_api_key",
        "apex_wealth_list_workspaces",
        "apex_wealth_set_workspace",
    }


def test_apex_filter_excludes_wealth_tools():
    assert not any(n.startswith("apex_wealth_") for n in _tool_names(_settings(), "apex"))


def test_telegram_filter_registers_expected_telegram_tools():
    names = _tool_names(_settings(), "telegram")

    assert names == {
        "system_get_current_time",
        "telegram_list_destinations",
        "telegram_send_message",
    }


def test_recipes_filter_registers_only_recipe_tools():
    assert _tool_names(_settings(), "recipes") == {
        "system_get_current_time",
        "recipes_get",
        "recipes_list",
        "recipes_search",
        "recipes_save",
        "recipes_update",
        "recipes_delete",
        "recipes_add_feedback",
        "recipes_delete_feedback",
    }


def test_other_connectors_unaffected_without_capella():
    settings = _settings().model_copy(update={"capella_connection_string": None})
    assert not settings.capella_configured
    names = _tool_names(settings)
    assert {"finance_get_current_rate", "apex_list_clients", "telegram_send_message"} <= names
    assert {"recipes_get", "recipes_save"} <= names
    assert not any(n.startswith("recipes_") for n in _tool_names(settings, "finance"))


def test_backward_compatible_mcp_endpoint_still_registers_all_enabled_tools():
    names = _tool_names(_settings())

    assert "system_get_current_time" in names
    assert "google_calendar_list_calendars" in names
    assert "google_calendar_delete_event" in names
    assert "google_tasks_list_tasks" in names
    assert "google_tasks_complete_task" in names
    assert "apex_list_clients" in names
    assert "apex_create_invoice" in names
    assert "apex_wealth_get_investments_summary" in names
    assert "google_drive_search_files" in names
    assert "telegram_send_message" in names
    assert "telegram_list_destinations" in names
    assert "recipes_search" in names
    assert "recipes_delete" in names


def test_every_mcp_mount_is_behind_identity_middleware(monkeypatch):
    settings = _settings()
    get_settings.cache_clear()
    monkeypatch.setattr("gateway.app.get_settings", lambda: settings)

    app = create_app()
    mounted = {route.path: route.app for route in app.routes if hasattr(route, "app")}

    for path in (
        "/mcp",
        "/mcp/calendar",
        "/mcp/drive",
        "/mcp/tasks",
        "/mcp/apex",
        "/mcp/telegram",
        "/mcp/recipes",
    ):
        assert isinstance(mounted[path], IdentityMiddleware)

    get_settings.cache_clear()


def test_app_starts_without_capella_and_lists_recipe_tools(monkeypatch):
    """The app boots with Capella unset and /mcp/recipes/ serves tool discovery."""
    from fastapi.testclient import TestClient

    settings = _settings().model_copy(update={"capella_connection_string": None})
    get_settings.cache_clear()
    monkeypatch.setattr("gateway.app.get_settings", lambda: settings)

    app = create_app()
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "x-gateway-auth": settings.gateway_shared_secret,
        "x-openwebui-user-id": "alice",
    }
    with TestClient(app, base_url="http://localhost:8000") as client:
        assert client.get("/health").json() == {"status": "ok"}
        response = client.post(
            "/mcp/recipes/",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    assert response.status_code == 200, response.text
    body = response.text
    for name in ("recipes_get", "recipes_search", "recipes_delete", "system_get_current_time"):
        assert name in body
    assert "google_calendar" not in body

    get_settings.cache_clear()


def test_product_mcp_no_slash_redirect_route_exists(monkeypatch):
    settings = _settings()
    get_settings.cache_clear()
    monkeypatch.setattr("gateway.app.get_settings", lambda: settings)

    app = create_app()
    route = next(route for route in app.routes if getattr(route, "path", None) == "/mcp/{product}")

    assert {"GET", "POST", "DELETE"}.issubset(route.methods)

    get_settings.cache_clear()
