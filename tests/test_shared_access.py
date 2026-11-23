"""Shared data remains authenticated while personal data stays grant protected."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from gateway.access import policy, routes
from gateway.connectors.base import AccessMode
from gateway.connectors.registry import CONNECTORS, by_slug
from gateway.db.models import ClientGrant
from gateway.mcp.context import _current_user
from gateway.mcp.server import build_mcp, product_tool_filter
from gateway.providers.base import RiskLevel
from gateway.providers.registry import ToolError
from tests.test_clerk_oauth import _settings
from tests.test_client_access import auth, request, store  # noqa: F401


def test_only_finance_is_explicitly_shared():
    assert {c.slug for c in CONNECTORS if c.access_mode is AccessMode.AUTHENTICATED_SHARED} == {
        "finance"
    }
    assert by_slug("recipes").access_mode is AccessMode.PERSONAL


@pytest.mark.asyncio
async def test_shared_discovery_and_execution_without_grants(store, monkeypatch):  # noqa: F811
    scope, alice, bob = store
    monkeypatch.setattr("gateway.db.engine.session_scope", scope)
    finance = build_mcp(_settings(), product_tool_filter("finance"))
    expected = {s.name for s in finance.gateway_specs if s.name.startswith("finance_")}
    for user, owner in [("alice", alice), ("bob", bob)]:
        identity = auth(user=user)
        token = _current_user.set(identity)
        try:
            tools = await finance.list_tools()
            assert {t.name for t in tools} == expected | {"system_get_current_time"}
            assert "permissions" not in next(
                t.description for t in tools if t.name == "system_get_current_time"
            )
            with scope() as session:
                for name in expected:
                    policy.enforce(
                        session, owner, _settings(), identity, SimpleNamespace(name=name)
                    )
                with pytest.raises(ToolError):
                    policy.enforce(
                        session, owner, _settings(), identity, SimpleNamespace(name="recipes_list")
                    )
        finally:
            _current_user.reset(token)


@pytest.mark.asyncio
async def test_mixed_endpoint_and_revoked_personal_grants(store, monkeypatch):  # noqa: F811
    scope, alice, _ = store
    monkeypatch.setattr("gateway.db.engine.session_scope", scope)
    with scope() as session:
        session.add(
            ClientGrant(
                user_id=alice,
                issuer=_settings().clerk_issuer,
                client_id="client-a",
                tools=["telegram_send_message"],
                revoked=True,
            )
        )
    token = _current_user.set(auth())
    try:
        names = {t.name for t in await build_mcp(_settings()).list_tools()}
        assert "finance_list_currencies" in names
        assert "telegram_send_message" not in names
        assert not any(n.startswith("recipes_") for n in names)
    finally:
        _current_user.reset(token)


def test_shared_needs_oauth_identity():
    spec = SimpleNamespace(name="finance_list_currencies")
    assert not policy.tool_allowed(spec, replace(auth(), source="header"), set())
    assert not policy.tool_allowed(spec, replace(auth(), oauth_client_id=None), set())


def test_shared_write_registration_rejected(monkeypatch):
    from gateway.providers.finance import read

    original = read.register

    def register(registry):
        original(registry)
        spec = registry._specs[-1]
        registry._specs[-1] = replace(spec, risk=RiskLevel.MUTATING)

    monkeypatch.setattr(read, "register", register)
    with pytest.raises(ValueError, match="Shared connector finance must contain only read tools"):
        build_mcp(_settings())


def test_shared_grant_management_rejects_selections(store):  # noqa: F811
    req = request("PUT")
    req.session["target"]["resource"] = f"{_settings().base_url}/mcp/finance"
    req.app.state.access_catalog.append(
        {
            "slug": "finance",
            "access_mode": "authenticated_shared",
            "tools": [{"name": "finance_list_currencies"}],
        }
    )
    with pytest.raises(HTTPException) as exc:
        routes.save("client-a", routes.GrantBody(tools=["finance_list_currencies"]), req)
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        routes.revoke("client-a", req)
    assert exc.value.status_code == 400


def test_shared_execution_is_audited(store, monkeypatch):  # noqa: F811
    from gateway.providers import registry

    scope, _, _ = store
    monkeypatch.setattr(registry, "session_scope", scope)
    audits = []
    monkeypatch.setattr(registry, "record_audit", audits.append)
    spec = next(
        s for s in build_mcp(_settings()).gateway_specs if s.name == "finance_list_currencies"
    )
    spec = replace(spec, handler=lambda *_: {"currencies": ["USD", "IRR"]})
    token = _current_user.set(auth())
    try:
        assert registry._run_pipeline(spec, _settings(), {}, None) == {"currencies": ["USD", "IRR"]}
    finally:
        _current_user.reset(token)
    assert len(audits) == 1 and audits[0].result_status == "ok"
