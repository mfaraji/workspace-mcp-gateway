"""Tests for the Apex wealth (read-only) provider."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from gateway.audit.log import summarize_input
from gateway.config import Settings
from gateway.connectors.registry import by_spec
from gateway.providers.apex import auth, wealth
from gateway.providers.apex import client as apex_client
from gateway.providers.base import CallContext, RiskLevel
from gateway.providers.registry import ToolRegistry

EXPECTED_TOOLS = {
    "apex_wealth_list_accounts",
    "apex_wealth_get_accounts_summary",
    "apex_wealth_get_investments_summary",
    "apex_wealth_get_positions",
    "apex_wealth_list_manual_holdings",
    "apex_wealth_get_net_worth_history",
}


def _settings(**overrides) -> Settings:
    base = dict(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
        base_url="http://localhost:8000",
        google_client_id="cid",
        google_client_secret="secret",
        apex_base_url="http://apex.local",
        token_encryption_key="x" * 43 + "=",
        gateway_shared_secret="shared-secret-value",
        trusted_open_webui_origin="https://openwebui.internal",
        session_secret="sess",
    )
    base.update(overrides)
    return Settings(**base)


def _ctx() -> CallContext:
    return CallContext(user_id=uuid.uuid4(), external_user_id="user-1", request_id="req-1")


@pytest.fixture
def captured(monkeypatch):
    calls: list[dict] = []

    def fake_apex_request(
        settings, api_key, method, path, params=None, json=None, extra_headers=None
    ):
        calls.append(dict(api_key=api_key, method=method, path=path, params=params))
        return {"ok": True}

    monkeypatch.setattr(apex_client, "apex_request", fake_apex_request)
    monkeypatch.setattr(wealth, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        wealth,
        "apex_credentials",
        lambda *a, **k: apex_client.ApexCredentials("tok", {"X-Workspace-Id": k["workspace_id"]}),
    )
    return calls


@pytest.mark.parametrize(
    "handler,path",
    [
        (wealth.list_accounts, "/api/wealth/accounts"),
        (wealth.get_accounts_summary, "/api/wealth/accounts-summary"),
        (wealth.get_investments_summary, "/api/wealth/investments"),
    ],
)
def test_parameterless_tools_call_their_path(captured, handler, path):
    assert handler(wealth.WorkspaceInput(workspace_id="w1"), _ctx(), None) == {"ok": True}
    assert captured == [
        dict(api_key="tok", method="GET", path=path, params=None)
    ]


def test_get_positions_serialises_filters(captured):
    args = wealth.GetPositionsInput(workspace_id="w1", account_id="a1", as_of_date="2026-09-01")
    wealth.get_positions(args, _ctx(), None)
    assert captured[0]["path"] == "/api/wealth/positions"
    assert captured[0]["params"] == {"accountId": "a1", "asOfDate": "2026-09-01"}


def test_get_positions_without_filters_sends_no_params(captured):
    wealth.get_positions(wealth.GetPositionsInput(workspace_id="w1"), _ctx(), None)
    assert captured[0]["params"] is None


def test_list_manual_holdings_filters_by_account(captured):
    with_account = wealth.ListManualHoldingsInput(workspace_id="w1", account_id="a1")
    wealth.list_manual_holdings(with_account, _ctx(), None)
    wealth.list_manual_holdings(wealth.ListManualHoldingsInput(workspace_id="w1"), _ctx(), None)
    assert captured[0]["params"] == {"accountId": "a1"}
    assert captured[1]["params"] is None


def test_net_worth_months_default_and_bounds(captured):
    wealth.get_net_worth_history(wealth.GetNetWorthHistoryInput(workspace_id="w1"), _ctx(), None)
    assert captured[0]["path"] == "/api/wealth/net-worth"
    assert captured[0]["params"] == {"months": 12}
    for bad in (0, 61):
        with pytest.raises(ValidationError):
            wealth.GetNetWorthHistoryInput(workspace_id="w1", months=bad)


def test_wealth_tools_are_read_only_and_carry_a_capability():
    registry = ToolRegistry()
    wealth.register(registry)
    auth.register_wealth(registry)
    specs = {s.name: s for s in registry._specs}
    assert set(specs) == EXPECTED_TOOLS | {"apex_wealth_list_workspaces"}
    assert all(s.risk is RiskLevel.READ for s in specs.values())
    assert {by_spec(s).slug for s in specs.values()} == {"apex-wealth"}
    assert specs["apex_wealth_get_positions"].required_capability == "investments"
    assert specs["apex_wealth_get_accounts_summary"].required_capability == "assets"
    assert specs["apex_wealth_list_workspaces"].required_capability is None


def test_audit_records_only_safe_fields():
    assert summarize_input(
        "apex_wealth_get_positions", {"workspace_id": "w1", "account_id": "a1", "x": "s"}
    ) == "workspace_id='w1', account_id='a1'"
    assert summarize_input("apex_wealth_get_net_worth_history", {"months": 6}) == "months=6"
    assert summarize_input("apex_wealth_list_accounts", {"token": "secret"}) == ""
