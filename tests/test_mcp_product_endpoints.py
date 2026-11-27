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
        token_encryption_key="x" * 43 + "=",
        gateway_shared_secret="shared-secret-value",
        trusted_open_webui_origin="https://openwebui.internal",
        session_secret="sess",
        clerk_issuer="https://clerk.example.test",
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
        name == "system_get_current_time" or name.startswith("google_calendar_") for name in names
    )


def test_drive_filter_registers_prefixed_drive_tools():
    # Without a connector (the `/mcp` style registration) names stay prefixed and the
    # gateway's own list_accounts extra is present; the bare Google mount is covered in
    # tests/test_drive_google_parity.py.
    from gateway.connectors.registry import GOOGLE_DRIVE_TOOLS

    assert _tool_names(_settings(), "drive") == {
        "system_get_current_time",
        "google_drive_list_accounts",
        *(f"google_drive_{n}" for n in GOOGLE_DRIVE_TOOLS),
    }


def test_tasks_filter_registers_expected_tasks_tools():
    names = _tool_names(_settings(), "tasks")

    assert "system_get_current_time" in names
    assert "google_tasks_list_tasklists" in names
    assert "google_tasks_create_task" in names
    assert "google_tasks_delete_task" in names
    assert all(
        name == "system_get_current_time" or name.startswith("google_tasks_") for name in names
    )


def test_gmail_filter_registers_read_and_draft_tools_only():
    names = _tool_names(_settings(), "gmail")

    assert names == {
        "system_get_current_time",
        "google_gmail_search_messages",
        "google_gmail_get_message",
        "google_gmail_get_thread",
        "google_gmail_download_attachment",
        "google_gmail_list_accounts",
        "google_gmail_search_threads",
        "google_gmail_get_draft",
        "google_gmail_list_labels",
        "google_gmail_create_label",
        "google_gmail_update_label",
        "google_gmail_delete_label",
        "google_gmail_label_message",
        "google_gmail_unlabel_message",
        "google_gmail_update_message_labels",
        "google_gmail_label_thread",
        "google_gmail_unlabel_thread",
        "google_gmail_trash_message",
        "google_gmail_untrash_message",
        "google_gmail_trash_thread",
        "google_gmail_untrash_thread",
        "google_gmail_mark_message_spam",
        "google_gmail_unmark_message_spam",
        "google_gmail_mark_thread_spam",
        "google_gmail_unmark_thread_spam",
        "google_gmail_apply_sensitive_message_label",
        "google_gmail_apply_sensitive_thread_label",
        "google_gmail_list_drafts",
        "google_gmail_create_draft",
        "google_gmail_update_draft",
        "google_gmail_delete_draft",
    }


def test_apex_filter_registers_expected_apex_tools():
    names = _tool_names(_settings(), "apex")

    assert names == {
        "system_get_current_time",
        "apex_list_clients",
        "apex_list_timesheets",
        "apex_get_last_timesheet",
        "apex_create_invoice",
        "apex_create_timesheet",
        "apex_update_invoice_status",
        "apex_update_timesheet",
        "apex_list_expense_categories",
        "apex_create_expense",
        "apex_upload_expense_receipt",
        "apex_list_workspaces",
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
        "apex_wealth_list_workspaces",
    }


def test_apex_transactions_filter_registers_only_transaction_tools():
    names = _tool_names(_settings(), "apex-transactions")

    assert names == {
        "system_get_current_time",
        "apex_transactions_find_categories",
        "apex_transactions_list_properties",
        "apex_transactions_find_merchants",
        "apex_transactions_summarize",
        "apex_transactions_search",
        "apex_transactions_list_workspaces",
    }


def test_apex_filter_excludes_transaction_tools():
    assert not any(
        n.startswith("apex_transactions_") for n in _tool_names(_settings(), "apex")
    )
    assert not any(
        n.startswith("apex_transactions_") for n in _tool_names(_settings(), "apex-wealth")
    )


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
        "/mcp/gmail",
        "/mcp/apex",
        "/mcp/telegram",
        "/mcp/recipes",
    ):
        assert isinstance(mounted[path], IdentityMiddleware)

    get_settings.cache_clear()


def test_app_starts_without_capella_and_rejects_legacy_headers(monkeypatch):
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
    assert response.status_code == 401
    assert "OAuth authentication required" in response.text

    get_settings.cache_clear()


def test_product_mcp_no_slash_redirect_route_exists(monkeypatch):
    settings = _settings()
    get_settings.cache_clear()
    monkeypatch.setattr("gateway.app.get_settings", lambda: settings)

    app = create_app()
    route = next(route for route in app.routes if getattr(route, "path", None) == "/mcp/{product}")

    assert {"GET", "POST", "DELETE"}.issubset(route.methods)

    get_settings.cache_clear()
