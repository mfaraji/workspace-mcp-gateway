"""Tests for the Apex transaction-question (read-only) provider."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from pydantic import ValidationError

from gateway.audit.log import summarize_input
from gateway.config import Settings
from gateway.connectors.registry import by_spec
from gateway.providers.apex import auth, transactions
from gateway.providers.apex import client as apex_client
from gateway.providers.base import CallContext, RiskLevel
from gateway.providers.registry import ToolError, ToolRegistry

EXPECTED_TOOLS = {
    "apex_transactions_find_categories",
    "apex_transactions_list_properties",
    "apex_transactions_find_merchants",
    "apex_transactions_summarize",
    "apex_transactions_search",
}

PERSONAL = {
    "id": "w-main",
    "name": "Main",
    "kind": "PERSONAL",
    "capabilities": ["transactionInsights", "budgets"],
}
BUSINESS = {
    "id": "w-biz",
    "name": "Acme",
    "kind": "BUSINESS",
    "capabilities": ["transactionInsights", "invoicing"],
}
RENTAL = {
    "id": "w-rent",
    "name": "Rentals",
    "kind": "RENTAL",
    "capabilities": ["transactionInsights", "rentalBooks", "properties"],
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


class _Calls(list):
    """The Apex requests made, with the credential requests that preceded them."""

    def __init__(self) -> None:
        super().__init__()
        self.credentials: list[dict] = []


@pytest.fixture
def captured(monkeypatch):
    """Records each Apex request and the credentials requested for it."""
    calls = _Calls()
    credentials = calls.credentials

    def fake_apex_request(
        settings, token, method, path, params=None, json=None, extra_headers=None
    ):
        calls.append(dict(token=token, method=method, path=path, params=params))
        return {"ok": True}

    def fake_credentials(settings, ctx, **kwargs):
        credentials.append(kwargs)
        return apex_client.ApexCredentials("tok", {"X-Workspace-Id": kwargs["workspace_id"]})

    monkeypatch.setattr(apex_client, "apex_request", fake_apex_request)
    monkeypatch.setattr(transactions, "get_settings", lambda: _settings())
    monkeypatch.setattr(transactions, "apex_credentials", fake_credentials)
    return calls


# --- request mapping -----------------------------------------------------------


def test_find_categories_sends_the_trimmed_query(captured):
    args = transactions.FindCategoriesInput(workspace_id="w1", query="  water ")
    assert transactions.find_categories(args, _ctx(), None) == {"ok": True}
    assert captured == [
        dict(token="tok", method="GET", path="/api/transactions/categories", params={"q": "water"})
    ]


def test_find_categories_without_a_query_sends_no_params(captured):
    transactions.find_categories(transactions.FindCategoriesInput(workspace_id="w1"), _ctx(), None)
    assert captured[0]["params"] is None


def test_summarize_defaults_send_only_the_page_size(captured):
    transactions.summarize(transactions.SummarizeInput(workspace_id="w1"), _ctx(), None)
    assert captured[0]["path"] == "/api/transactions/summary"
    assert captured[0]["params"] == {"limit": 25}


def test_summarize_maps_every_filter_to_the_api_names(captured):
    args = transactions.SummarizeInput(
        workspace_id="w1",
        period="last_quarter",
        category_ids=["c1", "c2"],
        exclude_category_ids=["c3"],
        include_subcategories=False,
        merchants=["toronto water"],
        text="bill",
        account_ids=["a1"],
        tags=["home"],
        direction="net",
        min_amount=5,
        max_amount=500.5,
        include_excluded_from_budget=True,
        include_hidden=False,
        include_transfers=True,
        property_ids=["p1"],
        t776_lines=["REPAIRS_MAINTENANCE"],
        treatments=["CAPITAL"],
        group_by=["month", "parent_category"],
        compare="same_period_last_year",
        limit=10,
    )
    transactions.summarize(args, _ctx(), None)
    assert captured[0]["params"] == {
        "period": "last_quarter",
        "categoryId": ["c1", "c2"],
        "excludeCategoryId": ["c3"],
        "includeSubcategories": False,
        "merchant": ["toronto water"],
        "text": "bill",
        "accountId": ["a1"],
        "tag": ["home"],
        "direction": "net",
        "minAmount": 5,
        "maxAmount": 500.5,
        "includeExcludedFromBudget": True,
        "includeHidden": False,
        "includeTransfers": True,
        "assetId": ["p1"],
        "t776Line": ["REPAIRS_MAINTENANCE"],
        "treatment": ["CAPITAL"],
        "groupBy": ["month", "parentCategory"],
        "compare": "same_period_last_year",
        "limit": 10,
    }


def test_false_and_zero_are_sent_not_dropped(captured):
    args = transactions.SummarizeInput(workspace_id="w1", include_hidden=False, min_amount=0)
    transactions.summarize(args, _ctx(), None)
    assert captured[0]["params"]["includeHidden"] is False
    assert captured[0]["params"]["minAmount"] == 0


def test_custom_dates_are_sent_as_iso_and_group_names_are_camel_cased(captured):
    args = transactions.SummarizeInput(
        workspace_id="w1",
        from_date=date(2026, 1, 1),
        to_date=date(2026, 3, 31),
        group_by=["t776_line"],
    )
    transactions.summarize(args, _ctx(), None)
    assert captured[0]["params"]["from"] == "2026-01-01"
    assert captured[0]["params"]["to"] == "2026-03-31"
    assert captured[0]["params"]["groupBy"] == ["t776Line"]


def test_search_sends_paging_and_sort(captured):
    args = transactions.SearchInput(
        workspace_id="w1", category_ids=["c1"], sort="amount", limit=50, cursor="abc"
    )
    transactions.search(args, _ctx(), None)
    assert captured[0]["path"] == "/api/transactions/search"
    assert captured[0]["params"] == {
        "categoryId": ["c1"],
        "cursor": "abc",
        "sort": "amount",
        "limit": 50,
    }


def test_find_merchants_combines_query_filters_and_limit(captured):
    args = transactions.FindMerchantsInput(
        workspace_id="w1", query=" toronto water ", period="ytd", limit=5
    )
    transactions.find_merchants(args, _ctx(), None)
    assert captured[0]["path"] == "/api/transactions/merchants"
    assert captured[0]["params"] == {"period": "ytd", "q": "toronto water", "limit": 5}


def test_list_properties_calls_its_path(captured):
    transactions.list_properties(transactions.WorkspaceInput(workspace_id="w1"), _ctx(), None)
    assert captured[0]["path"] == "/api/transactions/properties"
    assert captured[0]["params"] is None


# --- credentials: scope, workspace and capability --------------------------------


_CALLS = [
    (transactions.find_categories, transactions.FindCategoriesInput, "transactionInsights"),
    (transactions.summarize, transactions.SummarizeInput, "transactionInsights"),
    (transactions.search, transactions.SearchInput, "transactionInsights"),
    (transactions.find_merchants, transactions.FindMerchantsInput, "transactionInsights"),
    (transactions.list_properties, transactions.WorkspaceInput, "rentalBooks"),
]


@pytest.mark.parametrize("handler,input_model,capability", _CALLS)
def test_each_call_mints_the_read_scope_for_the_workspace(
    captured, handler, input_model, capability
):
    handler(input_model(workspace_id="w1"), _ctx(), None)
    assert captured.credentials == [
        dict(
            scopes=["apex:transactions.read"],
            workspace_id="w1",
            capability=capability,
        )
    ]


def test_a_workspace_without_the_capability_makes_no_apex_request(monkeypatch):
    monkeypatch.setattr(transactions, "get_settings", lambda: _settings())
    monkeypatch.setattr(apex_client, "user_workspaces", lambda *a, **k: [PERSONAL])

    def must_not_be_called(*a, **k):
        raise AssertionError("no Apex request should be made")

    monkeypatch.setattr(apex_client, "apex_request", must_not_be_called)
    ctx = CallContext(
        user_id=uuid.uuid4(),
        external_user_id="u",
        request_id="r",
        clerk_subject="user_1",
        oauth_client_id="c",
    )
    # A PERSONAL workspace has no rental books, so the properties lookup is refused.
    with pytest.raises(ToolError) as exc:
        transactions.list_properties(transactions.WorkspaceInput(workspace_id="w-main"), ctx, None)
    assert exc.value.error_code == "forbidden"


# --- input validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(to_date=date(2026, 1, 1)),  # to without from
        dict(from_date=date(2026, 5, 1), to_date=date(2026, 4, 1)),  # reversed
        dict(period="last_century"),
        dict(direction="sideways"),
        dict(min_amount=-1),
        dict(group_by=["month", "category", "merchant"]),  # more than two
        dict(group_by=["weekday"]),
        dict(limit=0),
        dict(limit=101),
        dict(compare="next_year"),
        dict(t776_lines=["NOT_A_LINE"]),
        dict(treatments=["GIFT"]),
        dict(category_ids=[f"c{i}" for i in range(21)]),
    ],
)
def test_summarize_rejects_bad_input(kwargs):
    with pytest.raises(ValidationError):
        transactions.SummarizeInput(workspace_id="w1", **kwargs)


def test_workspace_id_is_required():
    with pytest.raises(ValidationError):
        transactions.SummarizeInput()


def test_merchant_and_search_limits_are_bounded():
    with pytest.raises(ValidationError):
        transactions.FindMerchantsInput(workspace_id="w1", limit=51)
    with pytest.raises(ValidationError):
        transactions.SearchInput(workspace_id="w1", sort="size")


# --- registration ----------------------------------------------------------------


def _specs():
    registry = ToolRegistry()
    transactions.register(registry)
    auth.register_transactions(registry)
    return {s.name: s for s in registry._specs}


def test_tools_are_read_only_and_belong_to_the_transactions_connector():
    specs = _specs()
    assert set(specs) == EXPECTED_TOOLS | {"apex_transactions_list_workspaces"}
    assert all(s.risk is RiskLevel.READ for s in specs.values())
    assert {by_spec(s).slug for s in specs.values()} == {"apex-transactions"}


def test_capabilities_gate_which_tools_a_user_is_offered():
    specs = _specs()
    for name in EXPECTED_TOOLS - {"apex_transactions_list_properties"}:
        assert specs[name].required_capability == "transactionInsights"
    assert specs["apex_transactions_list_properties"].required_capability == "rentalBooks"
    assert specs["apex_transactions_list_workspaces"].required_capability is None


@pytest.mark.parametrize(
    "workspace,expected",
    [
        (PERSONAL, {"transactionInsights"}),
        (BUSINESS, {"transactionInsights"}),
        (RENTAL, {"transactionInsights", "rentalBooks"}),
    ],
)
def test_every_workspace_kind_gets_the_question_tools_but_only_rental_gets_properties(
    workspace, expected
):
    specs = _specs()
    permitted = {
        s.required_capability
        for s in specs.values()
        if s.required_capability and apex_client.workspace_has(workspace, s.required_capability)
    }
    assert permitted == expected


def test_tool_descriptions_teach_the_flow():
    specs = _specs()
    assert "FIRST" in specs["apex_transactions_find_categories"].description
    assert "ENGLISH" in specs["apex_transactions_find_categories"].description or (
        "English" in specs["apex_transactions_find_categories"].description
    )
    summarize = specs["apex_transactions_summarize"].description
    for phrase in ("interpretation.period", "leftOut", "BUSINESS", "RENTAL"):
        assert phrase in summarize


# --- workspace listing -----------------------------------------------------------


def test_list_workspaces_only_offers_workspaces_that_can_answer(monkeypatch):
    monkeypatch.setattr(auth, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        auth,
        "user_workspaces",
        lambda *a, **k: [
            PERSONAL,
            RENTAL,
            {"id": "w-old", "name": "Old", "kind": "BUSINESS", "capabilities": ["invoicing"]},
        ],
    )
    result = auth.list_transactions_workspaces(auth.NoInput(), _ctx(), None)
    assert [w["id"] for w in result] == ["w-main", "w-rent"]


def test_list_workspaces_refuses_an_unknown_apex_user(monkeypatch):
    monkeypatch.setattr(auth, "get_settings", lambda: _settings())
    monkeypatch.setattr(auth, "user_workspaces", lambda *a, **k: None)
    with pytest.raises(ToolError) as exc:
        auth.list_transactions_workspaces(auth.NoInput(), _ctx(), None)
    assert exc.value.error_code == "forbidden"


# --- audit -----------------------------------------------------------------------


def test_audit_records_ids_and_presets_but_not_what_was_searched_for():
    summary = summarize_input(
        "apex_transactions_summarize",
        {
            "workspace_id": "w1",
            "period": "ytd",
            "category_ids": ["c1"],
            "group_by": ["month"],
            "merchants": ["dr. smith clinic"],
            "text": "private",
            "tags": ["medical"],
            "min_amount": 5000,
            "from_date": "2026-01-01",
        },
    )
    assert "workspace_id='w1'" in summary
    assert "period='ytd'" in summary
    assert "category_ids=['c1']" in summary
    for secret in ("smith", "private", "medical", "5000", "2026-01-01"):
        assert secret not in summary


def test_audit_notes_that_a_query_was_given_without_recording_it():
    summary = summarize_input(
        "apex_transactions_find_categories", {"workspace_id": "w1", "query": "therapy"}
    )
    assert "therapy" not in summary
    assert "query=<set>" in summary
    assert summarize_input("apex_transactions_list_workspaces", {"x": "y"}) == ""
