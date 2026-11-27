"""What an agent can learn from ``tools/list``: schemas, descriptions and tool names.

These tests read the registered tools directly (``MCPServer.list_tools``) so they do not
depend on grants or a database. Naming rules: ``spec.md`` §Tool Naming.
"""

from __future__ import annotations

import re

import pytest
from mcp.server.mcpserver import MCPServer

from gateway.connectors.registry import BUNDLES, CONNECTORS, by_slug
from gateway.db.models import ClientGrant
from gateway.mcp.context import _current_user
from gateway.mcp.server import build_mcp, product_tool_filter
from gateway.policy import confirm
from gateway.providers.apex import client as apex_client
from gateway.providers.apex import transactions, wealth
from gateway.providers.base import CallContext, RiskLevel, ToolSpec
from gateway.providers.registry import (
    ToolError,
    ToolRegistry,
    _connect_required_result,
    resolve_names,
)
from tests.test_clerk_oauth import _settings
from tests.test_client_access import auth, store  # noqa: F401 (store is a fixture)

_IDENT = re.compile(r"[a-z][a-z0-9_]*")


async def _published(mcp: MCPServer) -> dict:
    return {t.name: t for t in await MCPServer.list_tools(mcp)}


def _strings(value, path=""):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _strings(item, f"{path}[{i}]")


# --- 1. field guidance reaches tools/list ----------------------------------------------------


@pytest.mark.asyncio
async def test_summarize_publishes_descriptions_limits_enums_and_defaults():
    mcp = build_mcp(_settings(), product_tool_filter("apex-transactions"))
    schema = (await _published(mcp))["apex_transactions_summarize"].input_schema
    props = schema["properties"]

    assert set(props) == set(transactions.SummarizeInput.model_fields)
    assert [name for name, p in props.items() if not p.get("description")] == []
    assert schema["required"] == ["workspace_id"]

    group_by = next(p for p in props["group_by"]["anyOf"] if p.get("type") == "array")
    assert group_by["maxItems"] == 2
    assert "category" in group_by["items"]["enum"] and "month" in group_by["items"]["enum"]
    assert props["group_by"]["default"] is None

    category_ids = next(p for p in props["category_ids"]["anyOf"] if p.get("type") == "array")
    assert category_ids["maxItems"] == 20


@pytest.mark.asyncio
async def test_mutating_tools_advertise_the_confirmation_token():
    mcp = build_mcp(_settings(), product_tool_filter("apex"))
    tools = await _published(mcp)
    token = tools["apex_update_invoice_status"].input_schema["properties"]["confirmation_token"]
    assert "confirmation_token" in token["description"]
    assert token["default"] is None
    assert "confirmation_token" not in tools["apex_list_clients"].input_schema["properties"]
    assert "confirmation_token" not in tools["apex_list_clients"].input_schema.get("required", [])


@pytest.mark.asyncio
async def test_bare_mounts_publish_the_same_field_guidance():
    mcp = build_mcp(_settings(), product_tool_filter("drive"), connector=by_slug("drive"))
    props = (await _published(mcp))["create_file"].input_schema["properties"]
    assert props["name"]["description"] and props["file_ref"]["description"]
    assert props["mime_type"]["anyOf"][0]["maxLength"] == 127


def test_limits_are_enforced_by_the_pipeline_not_by_fastmcp():
    """MCPServer must not pre-validate: that would skip the audit record and echo input values."""
    mcp = build_mcp(_settings(), product_tool_filter("apex-transactions"))
    tool = mcp._tool_manager.get_tool("apex_transactions_summarize")
    parsed = tool.fn_metadata.arg_model.model_validate(
        {"workspace_id": "w", "group_by": ["category", "month", "year"]}
    )
    assert parsed.model_dump_one_level()["group_by"] == ["category", "month", "year"]


def test_wealth_tools_point_at_the_wealth_workspace_listing():
    assert "apex_wealth_list_workspaces" in apex_client.WEALTH_WORKSPACE_ID_DESC
    field = wealth.WorkspaceInput.model_fields["workspace_id"]
    assert field.description == apex_client.WEALTH_WORKSPACE_ID_DESC
    assert (
        "apex_wealth_list_workspaces"
        in wealth.GetPositionsInput.model_fields["workspace_id"].description
    )


# --- 2. Apex descriptions lead with the decision --------------------------------------------


def _apex_specs() -> dict[str, ToolSpec]:
    registry = ToolRegistry()
    for slug in ("apex", "apex-wealth", "apex-transactions"):
        by_slug(slug).register(registry, _settings())
    return {spec.name: spec for spec in registry._specs}


def test_invoice_status_leads_with_what_sent_and_paid_do():
    specs = _apex_specs()
    first = specs["apex_update_invoice_status"].description.split(". ")[0]
    assert (
        "SENT EMAILS the invoice PDF to the client" in first and "PAID records a payment" in first
    )
    sent = specs["apex_update_invoice_status"].preview_builder
    model = specs["apex_update_invoice_status"].input_model
    assert "emails the client" in sent(model(workspace_id="w", invoice_id="i1", status="SENT"))
    assert "does not email" in sent(model(workspace_id="w", invoice_id="i1", status="PAID"))


def test_expense_descriptions():
    specs = _apex_specs()
    create = specs["apex_create_expense"].description
    preamble_end = create.index("The app fills in the file's content when the call runs. ")
    after = create[preamble_end + len("The app fills in the file's content when the call runs. ") :]
    assert after.startswith("Books a needs-review expense in the chosen workspace")
    assert "amount and date are known" in after
    assert "inbox" not in specs["apex_list_expense_categories"].description


def test_transactions_descriptions_state_how_much_versus_which():
    specs = _apex_specs()
    first = {n: specs[n].description.split(":")[0] for n in specs if "transactions_s" in n}
    assert "how much" in first["apex_transactions_summarize"]
    assert "which transactions" in first["apex_transactions_search"]
    assert "never by you" in specs["apex_transactions_summarize"].description


def test_every_apex_workspace_listing_says_what_the_mount_is_for_and_when_to_ask():
    specs = _apex_specs()
    assert specs["apex_list_workspaces"].description.startswith("Apex Finance workspaces")
    assert specs["apex_wealth_list_workspaces"].description.startswith("Personal wealth")
    for name in ("apex_list_workspaces", "apex_wealth_list_workspaces"):
        assert "ask the user" in specs[name].description or "ask them" in specs[name].description


# --- 3. names in guidance exist on the mount ------------------------------------------------


def _mounts():
    yield "all", build_mcp(_settings())
    for connector in CONNECTORS:
        yield (
            connector.slug,
            build_mcp(_settings(), product_tool_filter(connector.slug), connector=connector),
        )
    for bundle in BUNDLES:
        yield bundle.slug, build_mcp(_settings(), product_tool_filter(bundle.slug))


@pytest.mark.asyncio
async def test_no_mount_tells_the_model_to_call_a_tool_it_does_not_have():
    problems = []
    for label, mcp in _mounts():
        internal = {spec.name for spec in mcp.gateway_specs}
        tools = await _published(mcp)
        for tool in tools.values():
            texts = [
                ("description", tool.description or ""),
                *_strings(tool.input_schema, "schema"),
            ]
            for path, text in texts:
                for word in set(_IDENT.findall(text)):
                    if word in internal and word not in tools:
                        problems.append(f"/mcp/{label} {tool.name} {path}: {word}")
    assert problems == []


@pytest.mark.asyncio
async def test_bare_mounts_name_their_own_tools_by_bare_name():
    drive = await _published(
        build_mcp(_settings(), product_tool_filter("drive"), connector=by_slug("drive"))
    )
    assert "create_file" in drive["download_file_content"].description
    assert "google_drive_create_file" not in drive["download_file_content"].description
    assert (
        "download_file_content"
        in drive["create_file"].input_schema["properties"]["file_ref"]["description"]
    )

    gmail = await _published(
        build_mcp(_settings(), product_tool_filter("gmail"), connector=by_slug("gmail"))
    )
    text = gmail["download_gmail_attachment"].description
    assert "get_message" in text and "google_gmail_get_message" not in text
    assert "apex_" not in text


@pytest.mark.asyncio
async def test_prefixed_mounts_keep_internal_names_in_text():
    tools = await _published(build_mcp(_settings(), product_tool_filter("gmail")))
    assert "google_gmail_get_message" in tools["google_gmail_download_attachment"].description


def test_resolve_names_rewrites_whole_names_only():
    names = {"google_drive_create_file": "create_file"}
    assert resolve_names("use google_drive_create_file.", names) == "use create_file."
    assert resolve_names("google_drive_create_file_x", names) == "google_drive_create_file_x"
    assert resolve_names({"a": ["google_drive_create_file", 3]}, names) == {"a": ["create_file", 3]}


def _spec(name: str, risk: RiskLevel = RiskLevel.READ) -> ToolSpec:
    from pydantic import BaseModel

    class _In(BaseModel):
        pass

    return ToolSpec(
        name=name,
        provider="test",
        risk=risk,
        description="test",
        input_model=_In,
        handler=lambda _m, _c, _s: {},
    )


def test_duplicate_exposed_names_on_one_mount_are_rejected():
    registry = ToolRegistry()
    registry.add(_spec("google_x_a"))
    registry.add(_spec("google_x_b"))
    mcp = MCPServer(name="t")
    with pytest.raises(ValueError, match="duplicate exposed tool names: same"):
        registry.register_all(mcp, _settings(), names={"google_x_a": "same", "google_x_b": "same"})


def test_alias_for_an_unregistered_tool_fails_construction():
    registry = ToolRegistry()
    registry.add(_spec("google_x_a"))
    with pytest.raises(ValueError, match="unregistered tools: google_x_missing"):
        registry.register_all(
            MCPServer(name="t"),
            _settings(),
            names={"google_x_a": "a", "google_x_missing": "missing"},
        )


@pytest.mark.parametrize("slug", ["drive", "gmail"])
@pytest.mark.asyncio
async def test_discovery_on_bare_mounts_is_gated_by_internal_grants(
    slug,
    store,  # noqa: F811
    monkeypatch,
):
    scope, alice, _ = store
    monkeypatch.setattr("gateway.db.engine.session_scope", scope)
    mcp = build_mcp(_settings(), product_tool_filter(slug), connector=by_slug(slug))
    granted = {
        "drive": ("google_drive_search_files", "search_files"),
        "gmail": ("google_gmail_get_message", "get_message"),
    }[slug]
    token = _current_user.set(auth())
    try:
        assert {t.name for t in await mcp.list_tools()} == {"system_get_current_time"}
        with scope() as session:
            session.add(
                ClientGrant(
                    user_id=alice,
                    issuer=_settings().clerk_issuer,
                    client_id="client-a",
                    tools=[granted[0]],
                )
            )
        assert {t.name for t in await mcp.list_tools()} == {
            "system_get_current_time",
            granted[1],
        }
    finally:
        _current_user.reset(token)


def test_download_gmail_attachment_is_the_only_alias_outside_the_google_list():
    """Its bare name is the one Google's own Gmail MCP server uses (see registry.py)."""
    from gateway.connectors.registry import GOOGLE_GMAIL_TOOLS

    gmail = by_slug("gmail")
    outside = {
        bare for internal, bare in gmail.google_names.items() if bare not in GOOGLE_GMAIL_TOOLS
    }
    assert outside == {"download_gmail_attachment"}


# --- runtime text uses the exposed name -------------------------------------------------------


def test_connection_guidance_names_the_exposed_tool():
    spec = _spec("google_tasks_list_tasklists")
    spec = ToolSpec(**{**spec.__dict__, "provider": "google"})
    result = _connect_required_result(
        spec, _settings(), "alice", ToolError("reauth_required", "x"), "list_tasklists"
    )
    assert result is not None
    assert "Tool: list_tasklists" in result and "google_tasks_list_tasklists" not in result


def test_confirmation_fallback_preview_uses_the_exposed_name():
    spec = _spec("google_calendar_delete_event", RiskLevel.MUTATING)
    ctx = CallContext(user_id=None, external_user_id="alice", request_id="r")
    decision = confirm.check(_settings(), spec, {}, ctx, display_name="delete_event")
    assert isinstance(decision, confirm.NeedsConfirmation)
    assert decision.preview == "Confirm delete_event"
    plain = confirm.check(_settings(), spec, {}, ctx)
    assert plain.preview == "Confirm google_calendar_delete_event"


# --- cross-connector hints and runtime text ----------------------------------------------------


@pytest.mark.asyncio
async def test_related_tool_hints_appear_only_where_both_tools_are_served():
    everything = await _published(build_mcp(_settings()))
    text = everything["google_gmail_download_attachment"].description
    assert "receipt_file_ref to apex_create_expense" in text
    assert "apex_upload_expense_receipt" in text

    google = await _published(build_mcp(_settings(), product_tool_filter("google")))
    assert "apex" not in google["google_gmail_download_attachment"].description.lower()
    gmail = await _published(
        build_mcp(_settings(), product_tool_filter("gmail"), connector=by_slug("gmail"))
    )
    assert "apex" not in gmail["download_gmail_attachment"].description.lower()


@pytest.mark.asyncio
async def test_runtime_errors_on_a_bare_mount_use_that_mounts_tool_names(monkeypatch):
    import gateway.providers.registry as pipeline

    def failing(spec, settings, args, token, exposed=None):
        raise ToolError("invalid_input", "retry with google_drive_search_files first")

    monkeypatch.setattr(pipeline, "_run_pipeline", failing)
    mcp = build_mcp(_settings(), product_tool_filter("drive"), connector=by_slug("drive"))
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("get_file_metadata", {"file_id": "f1"})
    message = str(exc_info.value)
    assert "search_files first" in message and "google_drive_search_files" not in message
