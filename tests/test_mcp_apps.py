"""MCP Apps: tool metadata, structured results, grant-gated widget resources, widget hygiene."""

from __future__ import annotations

import re
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from gateway.db.models import ClientGrant, User
from gateway.identity.models import AuthenticatedUser
from gateway.mcp import ui
from gateway.mcp.context import _current_user
from gateway.mcp.server import build_mcp, product_tool_filter
from gateway.providers import registry
from gateway.providers.base import RiskLevel, ToolSpec
from tests.test_clerk_oauth import _settings as _base_settings

TASKS_URI = "ui://tasks/list"
RENDERING = {
    "google_tasks_list_tasks",
    "google_tasks_create_task",
    "google_tasks_update_task",
    "google_tasks_complete_task",
}


def _settings():
    """Settings with MCP Apps widgets switched on (they are off by default)."""
    return _base_settings().model_copy(update={"mcp_apps_enabled": True})


def _tools(product="tasks"):
    mcp = build_mcp(_settings(), product_tool_filter(product))
    return {t.name: t for t in mcp._tool_manager.list_tools()}


def test_tasks_tools_advertise_widget_and_app_callability():
    tools = _tools()
    for name in RENDERING:
        assert tools[name].meta["ui"]["resourceUri"] == TASKS_URI
        assert tools[name].meta["ui"]["autoApprove"] is True
        assert tools[name].meta["ui/resourceUri"] == TASKS_URI  # legacy key
    # Callable by the widget but nothing to render for these.
    for name in ("google_tasks_delete_task", "google_tasks_list_tasklists"):
        assert "resourceUri" not in tools[name].meta["ui"]
        assert tools[name].meta["ui"]["autoApprove"] is True
    assert tools["system_get_current_time"].meta is None


def test_ui_tool_descriptions_tell_the_model_not_to_repeat_the_widget():
    tools = _tools()
    for name in RENDERING:
        assert "do not repeat" in tools[name].description


def test_task_policy_one_click_for_reversible_actions_confirm_for_delete():
    tools = _tools()
    one_click = (
        "google_tasks_create_task",
        "google_tasks_update_task",
        "google_tasks_complete_task",
    )
    for name in one_click:
        assert "confirmation_token" not in tools[name].parameters["properties"]
    assert "confirmation_token" in tools["google_tasks_delete_task"].parameters["properties"]


def test_trim_task_exposes_real_completion_state():
    from gateway.providers.google.tasks.common import trim_task

    assert trim_task({"id": "1", "status": "completed"})["is_completed"] is True
    assert trim_task({"id": "1", "status": "needsAction"})["is_completed"] is False


def test_widget_tool_returns_text_and_structured_content(monkeypatch):
    from pydantic import BaseModel

    class In(BaseModel):
        x: int = 1

    spec = ToolSpec(
        name="demo_widget",
        provider="demo",
        risk=RiskLevel.READ,
        description="demo",
        input_model=In,
        handler=lambda *_: {"tasks": [{"title": "پرداخت"}]},
        ui_resource=TASKS_URI,
    )
    monkeypatch.setattr(registry, "_run_pipeline", lambda *a: {"tasks": [{"title": "پرداخت"}]})
    import anyio

    tool = registry.ToolRegistry()._build_callable(spec, _settings())
    result = anyio.run(lambda: tool(x=1))
    assert result.structuredContent == {"tasks": [{"title": "پرداخت"}]}
    assert "پرداخت" in result.content[0].text  # model-visible text keeps the data, unescaped
    # Confirmation prompts and non-dict results are not wrapped.
    monkeypatch.setattr(registry, "_run_pipeline", lambda *a: "Authorization required: ...")
    assert anyio.run(lambda: tool(x=1)) == "Authorization required: ..."


@pytest.fixture
def store(monkeypatch):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    User.__table__.create(engine)
    ClientGrant.__table__.create(engine)

    @contextmanager
    def scope():
        with Session(engine, expire_on_commit=False) as session:
            yield session
            session.commit()

    monkeypatch.setattr("gateway.db.engine.session_scope", scope)
    with scope() as session:
        alice = User(external_user_id="alice")
        session.add(alice)
        session.flush()
    return scope, alice.id


async def test_widget_resource_is_served_only_with_a_granted_tool(store):
    scope, alice = store
    mcp = build_mcp(_settings(), product_tool_filter("tasks"))
    token = _current_user.set(
        AuthenticatedUser("alice", source="oauth", clerk_subject="alice", oauth_client_id="c1")
    )
    try:
        assert await mcp.list_resources() == []
        with pytest.raises(ValueError):
            await mcp.read_resource(TASKS_URI)

        with scope() as session:
            session.add(
                ClientGrant(
                    user_id=alice,
                    issuer=_settings().clerk_issuer,
                    client_id="c1",
                    tools=["google_tasks_list_tasks"],
                )
            )
        listed = await mcp.list_resources()
        assert [str(r.uri) for r in listed] == [TASKS_URI]
        assert listed[0].mimeType == ui.MIME_TYPE
        (content,) = await mcp.read_resource(TASKS_URI)
        assert content.mime_type == ui.MIME_TYPE
        assert content.meta["ui"]["csp"] == {}
        assert "GatewayApp" in content.content and "{{" not in content.content
    finally:
        _current_user.reset(token)


def test_resources_exist_only_on_mounts_whose_tools_use_them():
    def served(product):
        mcp = build_mcp(_settings(), product_tool_filter(product))
        return [str(r.uri) for r in mcp._resource_manager.list_resources()]

    assert served("tasks") == [TASKS_URI]
    assert served("calendar") == ["ui://calendar/agenda"]
    assert served("apex") == ["ui://apex/billing"]


@pytest.mark.parametrize("uri", sorted(ui.WIDGETS))
def test_widgets_are_self_contained_and_safe(uri):
    html = ui.render_widget(uri)
    assert len(html.encode()) < 512 * 1024
    assert "{{" not in html
    # Sandbox has no network: no external script/style/img/font references or fetches.
    assert not re.search(r"(?:src|href)\s*=\s*[\"']https?://", html)
    assert not re.search(r"@import|url\(\s*[\"']?https?:", html)
    assert "fetch(" not in html and "XMLHttpRequest" not in html and "WebSocket" not in html
    # Data goes in through textContent/DOM nodes; innerHTML is only for static icon literals.
    for line in html.splitlines():
        if "innerHTML" in line:
            assert "static literals only" in line, line
    assert "eval(" not in html and "document.write" not in html


def test_every_referenced_widget_is_registered():
    from gateway.connectors.registry import CONNECTORS
    from gateway.providers.registry import ToolRegistry

    reg = ToolRegistry()
    settings = _settings()
    for connector in CONNECTORS:
        connector.register(reg, settings)
    used = {spec.ui_resource for spec in reg._specs if spec.ui_resource}
    assert used <= set(ui.WIDGETS)
    assert used == set(ui.WIDGETS)  # no orphaned widget files


def test_widgets_are_off_by_default_on_every_endpoint():
    settings = _base_settings()
    assert settings.mcp_apps_enabled is False
    for product in ("tasks", "calendar", "gmail", "finance", "apex"):
        mcp = build_mcp(settings, product_tool_filter(product))
        tools = mcp._tool_manager.list_tools()
        assert tools, product
        for tool in tools:
            assert not (tool.meta or {}).get("ui"), tool.name
            assert "Shows an interactive" not in (tool.description or ""), tool.name
        assert mcp._resource_manager.list_resources() == []
