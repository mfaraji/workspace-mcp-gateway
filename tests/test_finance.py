"""Tests for the finance (exchange-rate) provider."""

from __future__ import annotations

import uuid

import pytest

from gateway.audit.log import summarize_input
from gateway.connectors.registry import by_spec
from gateway.providers.base import CallContext, RiskLevel
from gateway.providers.finance import client, read
from gateway.providers.registry import ToolError, ToolRegistry

CURRENCIES = [
    {"id": 1, "abbreviation": "USD", "title": "US Dollar"},
    {"id": 29, "abbreviation": "CAD", "title": "Canadian Dollar"},
    {"id": 69, "abbreviation": "IRR", "title": "Iranian Rial"},
]


def _ctx() -> CallContext:
    return CallContext(user_id=uuid.uuid4(), external_user_id="u", request_id="r")


@pytest.fixture(autouse=True)
def fake_api(monkeypatch):
    client.clear_cache()
    monkeypatch.setattr(client, "list_currencies", lambda: CURRENCIES)
    state: dict = {}
    monkeypatch.setattr(client, "get_sources", lambda f, t: state["sources"])
    monkeypatch.setattr(client, "get_rates", lambda f, t, v: state[f"rates_{v}"])
    return state


def _row(title, buy, sell, rate):
    return {"sourceTitle": title, "buy": buy, "sell": sell, "thisToBase": rate, "dateTime": "d"}


def test_registered_tools_belong_to_finance_connector():
    registry = ToolRegistry()
    read.register(registry)
    specs = registry._specs
    assert {s.name for s in specs} == {
        "finance_get_current_rate",
        "finance_get_rate_change",
        "finance_get_rate_history",
        "finance_list_currencies",
    }
    for spec in specs:
        assert spec.risk is RiskLevel.READ
        assert by_spec(spec).slug == "finance"


def test_current_rate_averages_and_skips_null(fake_api):
    fake_api["sources"] = {
        "hasMultiSources": True,
        "directRateRows": [
            _row("A", "1897000", "1942000", "1919500"),
            _row("B", None, "1943000", "1920500"),
        ],
        "invalids": [{"sourceTitle": "C", "isOutlier": 1, "isExpired": 1}],
    }
    out = read.get_current_rate(read.CurrentRateInput(from_currency="cad"), _ctx(), None)
    assert out["pair"] == "CAD/IRR"
    assert out["averageBuy"] == 1897000
    assert out["averageSell"] == 1942500
    assert out["averageRate"] == 1920000
    assert out["rowsUsed"] == 2
    assert out["excluded"] == [{"sourceTitle": "C", "reason": "outlier, expired"}]


def test_current_rate_no_data(fake_api):
    fake_api["sources"] = {"hasMultiSources": False}
    out = read.get_current_rate(read.CurrentRateInput(from_currency="USD"), _ctx(), None)
    assert out == {"pair": "USD/IRR", "noData": True}


def test_unknown_currency_lists_valid_codes(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(client, "list_currencies", lambda: CURRENCIES)
    with pytest.raises(ToolError) as exc:
        client.resolve_currency("XXX")
    assert "CAD, IRR, USD" in str(exc.value)


def test_rate_change_24h_uses_index_minus_25(fake_api):
    rates = [str(1000 + i) for i in range(168)]
    fake_api["rates_1W"] = {"rates": rates, "dates": [f"d{i}" for i in range(168)]}
    out = read.get_rate_change(read.RateChangeInput(from_currency="USD"), _ctx(), None)
    assert out["current"] == 1167
    assert out["previous"] == 1143
    assert out["previousDate"] == "d143"
    assert out["change"] == 24
    assert out["changePercent"] == pytest.approx(24 / 1143 * 100)


def test_rate_change_daily_uses_last_two(fake_api):
    fake_api["rates_all"] = {"rates": ["100", "110"], "dates": ["a", "b"]}
    out = read.get_rate_change(
        read.RateChangeInput(from_currency="USD", window="daily"), _ctx(), None
    )
    assert (out["current"], out["previous"], out["previousDate"]) == (110, 100, "a")


def test_history_all_respects_limit(fake_api):
    fake_api["rates_all"] = {"rates": ["1", "2", "3"], "dates": ["a", "b", "c"]}
    out = read.get_rate_history(
        read.RateHistoryInput(from_currency="USD", view="all", limit=2), _ctx(), None
    )
    assert out["points"] == [{"date": "b", "rate": 2.0}, {"date": "c", "rate": 3.0}]


def test_audit_records_pair_fields():
    summary = summarize_input("finance_get_current_rate", {"from_currency": "USD", "to": "IRR"})
    assert "from_currency='USD'" in summary and "to='IRR'" in summary


def test_mcp_schema_exposes_from_currency():
    from gateway.mcp.server import build_mcp, product_tool_filter
    from tests.test_mcp_product_endpoints import _settings

    mcp = build_mcp(_settings(), product_tool_filter("finance"))
    tool = mcp._tool_manager._tools["finance_get_current_rate"]
    assert "from_currency" in tool.parameters["properties"]
    assert "from_currency" in tool.parameters["required"]
