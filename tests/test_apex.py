"""Tests for the Apex invoicing/timesheet provider."""

from __future__ import annotations

import uuid

import httpx
import pytest

from gateway.config import Settings
from gateway.providers.apex import client as apex_client
from gateway.providers.apex import read, write
from gateway.providers.base import CallContext, RiskLevel
from gateway.providers.registry import ToolError, ToolRegistry


def _settings(**overrides) -> Settings:
    base = dict(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
        base_url="http://localhost:8000",
        google_client_id="cid",
        google_client_secret="secret",
        apex_base_url="http://apex.local",
        apex_api_key="pk_live_test",
        token_encryption_key="x" * 43 + "=",
        gateway_shared_secret="shared-secret-value",
        trusted_open_webui_origin="https://openwebui.internal",
        session_secret="sess",
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


def test_apex_request_sends_bearer_auth_and_returns_json(monkeypatch):
    captured = {}

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        captured.update(method=method, url=url, params=params, json=json, headers=headers)
        return _FakeResponse(200, json_body={"ok": True})

    monkeypatch.setattr(apex_client.httpx, "request", fake_request)

    result = apex_client.apex_request(
        _settings(), "GET", "/api/clients", params={"clientId": "c1"}
    )

    assert result == {"ok": True}
    assert captured["method"] == "GET"
    assert captured["url"] == "http://apex.local/api/clients"
    assert captured["headers"] == {"Authorization": "Bearer pk_live_test"}
    assert captured["params"] == {"clientId": "c1"}


@pytest.mark.parametrize(
    "status,expected_code",
    [(401, "unauthorized"), (403, "forbidden"), (404, "not_found"), (429, "rate_limited"),
     (500, "provider_error")],
)
def test_apex_request_classifies_http_errors(monkeypatch, status, expected_code):
    monkeypatch.setattr(
        apex_client.httpx,
        "request",
        lambda *a, **k: _FakeResponse(status, text="boom"),
    )

    with pytest.raises(ToolError) as exc_info:
        apex_client.apex_request(_settings(), "GET", "/api/clients")

    assert exc_info.value.error_code == expected_code


def test_apex_request_wraps_transport_errors(monkeypatch):
    def raise_error(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(apex_client.httpx, "request", raise_error)

    with pytest.raises(ToolError) as exc_info:
        apex_client.apex_request(_settings(), "GET", "/api/clients")

    assert exc_info.value.error_code == "provider_error"


def test_list_timesheets_passes_client_id_filter(monkeypatch):
    captured = {}

    def fake_apex_request(settings, method, path, params=None, json=None):
        captured.update(method=method, path=path, params=params)
        return {"timesheets": []}

    monkeypatch.setattr(read, "apex_request", fake_apex_request)
    monkeypatch.setattr(read, "get_settings", lambda: _settings())

    args = read.ListTimesheetsInput(client_id="c1")
    result = read.list_timesheets(args, _ctx(), None)

    assert result == {"timesheets": []}
    assert captured == {"method": "GET", "path": "/api/timesheets", "params": {"clientId": "c1"}}


def test_create_invoice_requires_exactly_one_billing_payload():
    with pytest.raises(ValueError):
        write.CreateInvoiceInput(client_id="c1", issue_date="2026-07-31")

    with pytest.raises(ValueError):
        write.CreateInvoiceInput(
            client_id="c1",
            issue_date="2026-07-31",
            line_items=[{"description": "Consulting", "quantity": 2, "rate": 150}],
            timesheet={
                "periodStart": "2026-07-01",
                "periodEnd": "2026-07-14",
                "entries": [{"workDate": "2026-07-01", "description": "Work", "hours": 6}],
            },
        )


def test_create_invoice_builds_timesheet_body(monkeypatch):
    captured = {}

    def fake_apex_request(settings, method, path, params=None, json=None):
        captured.update(method=method, path=path, json=json)
        return {"id": "inv1"}

    monkeypatch.setattr(write, "apex_request", fake_apex_request)
    monkeypatch.setattr(write, "get_settings", lambda: _settings())

    args = write.CreateInvoiceInput(
        client_id="c1",
        issue_date="2026-07-31",
        timesheet=write.InlineTimesheetInput(
            period_start="2026-07-01",
            period_end="2026-07-14",
            entries=[
                write.TimesheetEntryInput(
                    work_date="2026-07-01", description="Implementation", hours=6
                )
            ],
        ),
    )
    result = write.create_invoice(args, _ctx(), None)

    assert result == {"id": "inv1"}
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/invoices"
    assert captured["json"] == {
        "clientId": "c1",
        "issueDate": "2026-07-31",
        "timesheet": {
            "periodStart": "2026-07-01",
            "periodEnd": "2026-07-14",
            "notes": None,
            "entries": [
                {"workDate": "2026-07-01", "description": "Implementation", "hours": 6.0}
            ],
        },
    }


def test_update_timesheet_builds_partial_body(monkeypatch):
    captured = {}

    def fake_apex_request(settings, method, path, params=None, json=None):
        captured.update(method=method, path=path, json=json)
        return {"id": "ts1"}

    monkeypatch.setattr(write, "apex_request", fake_apex_request)
    monkeypatch.setattr(write, "get_settings", lambda: _settings())

    args = write.UpdateTimesheetInput(timesheet_id="ts1", notes="Updated")
    result = write.update_timesheet(args, _ctx(), None)

    assert result == {"id": "ts1"}
    assert captured == {
        "method": "PUT",
        "path": "/api/timesheets/ts1",
        "json": {"notes": "Updated"},
    }


def test_read_and_write_tools_registered_with_expected_risk():
    registry = ToolRegistry()
    read.register(registry)
    write.register(registry)
    specs = {spec.name: spec for spec in registry._specs}

    assert set(specs) == {
        "apex_list_clients",
        "apex_list_timesheets",
        "apex_get_last_timesheet",
        "apex_create_invoice",
        "apex_update_timesheet",
    }
    assert specs["apex_list_clients"].risk is RiskLevel.READ
    assert specs["apex_create_invoice"].risk is RiskLevel.MUTATING
    assert specs["apex_update_timesheet"].risk is RiskLevel.MUTATING
