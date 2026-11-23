"""Low / medium risk tiers: defaults, per-user overrides, in-chat confirmation, API."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from gateway.access import policy, routes
from gateway.db.models import UserToolSetting
from gateway.policy import confirm
from gateway.providers.base import RiskLevel, RiskTier, ToolSpec
from gateway.providers.registry import _CONFIRM_INSTRUCTIONS, _run_pipeline
from tests.test_client_access import auth, request, store  # noqa: F401
from tests.test_policy_and_audit import _ctx, _settings

TOOL = "google_tasks_create_task"


class _In(BaseModel):
    event_id: str = "abc"


def _spec(requires_confirmation: bool | None = None, risk=RiskLevel.MUTATING) -> ToolSpec:
    return ToolSpec(
        name="google_calendar_delete_event",
        provider="google",
        risk=risk,
        description="d",
        input_model=_In,
        handler=lambda *a: {"done": True},
        preview_builder=lambda m: f"Delete {m.event_id}",
        requires_confirmation=requires_confirmation,
    )


def test_default_tiers_preserve_existing_behaviour():
    assert _spec().default_tier is RiskTier.MEDIUM
    assert _spec(requires_confirmation=False).default_tier is RiskTier.LOW
    assert _spec(risk=RiskLevel.READ).default_tier is None


def test_confirm_follows_the_tier_not_the_spec_flag():
    settings, ctx, args = _settings(), _ctx(), {"event_id": "abc"}
    # A tool that is low by default asks once the user raises it to medium ...
    low_spec = _spec(requires_confirmation=False)
    assert isinstance(
        confirm.check(settings, low_spec, args, ctx, tier=RiskTier.MEDIUM),
        confirm.NeedsConfirmation,
    )
    # ... and a medium-by-default tool runs directly once lowered.
    assert isinstance(
        confirm.check(settings, _spec(), args, ctx, tier=RiskTier.LOW), confirm.Allow
    )
    # Read tools never ask, whatever tier is passed.
    assert isinstance(
        confirm.check(settings, _spec(risk=RiskLevel.READ), args, ctx, tier=RiskTier.MEDIUM),
        confirm.Allow,
    )


def test_confirmation_token_ttl_is_configurable():
    settings, ctx, args = _settings(confirmation_token_ttl_seconds=0), _ctx(), {"event_id": "a"}
    token = confirm.mint_confirmation_token(settings, ctx, "google_calendar_delete_event", args)
    import time

    time.sleep(1.1)
    assert not confirm.verify_confirmation_token(
        settings, token, ctx, "google_calendar_delete_event", args
    )
    longer = _settings(confirmation_token_ttl_seconds=600)
    assert confirm.verify_confirmation_token(
        longer, token, ctx, "google_calendar_delete_event", args
    )


@pytest.fixture
def tiers(store):  # noqa: F811
    scope, alice, bob = store
    with scope() as session:
        UserToolSetting.__table__.create(session.bind)
    return scope, alice, bob


def test_effective_tier_uses_the_users_override(tiers):
    scope, alice, bob = tiers
    spec = _spec()
    with scope() as session:
        assert policy.effective_tier(session, alice, spec) is RiskTier.MEDIUM
        session.add(UserToolSetting(user_id=alice, tool_name=spec.name, tier="low"))
        session.flush()
        assert policy.effective_tier(session, alice, spec) is RiskTier.LOW
        # Another user is unaffected, and read tools have no tier.
        assert policy.effective_tier(session, bob, spec) is RiskTier.MEDIUM
        assert policy.effective_tier(session, alice, _spec(risk=RiskLevel.READ)) is None


def test_pipeline_asks_in_chat_for_medium_then_executes_with_the_token(tiers, monkeypatch):
    from gateway.mcp.context import _current_user
    from gateway.providers import registry

    scope, alice, _ = tiers
    spec = _spec(requires_confirmation=False)  # low by default
    with scope() as session:
        session.add(UserToolSetting(user_id=alice, tool_name=spec.name, tier="medium"))
    audits = []
    monkeypatch.setattr(registry, "session_scope", scope)
    monkeypatch.setattr(registry, "record_audit", audits.append)
    monkeypatch.setattr(registry, "get_or_create_user", lambda *_: SimpleNamespace(id=alice))
    monkeypatch.setattr(
        "gateway.access.policy.grant_for",
        lambda *_: SimpleNamespace(tools=[spec.name], revoked=False),
    )
    settings = _settings()
    token = _current_user.set(auth())
    try:
        first = _run_pipeline(spec, settings, {"event_id": "e1"}, None)
        assert first["status"] == "confirmation_required"
        assert first["question"] == "Delete e1\n\nDo you want me to proceed?"
        assert first["instructions"] == _CONFIRM_INSTRUCTIONS
        second = _run_pipeline(spec, settings, {"event_id": "e1"}, first["confirmation_token"])
    finally:
        _current_user.reset(token)
    assert second == {"done": True}
    assert [(a.result_status, a.risk_tier) for a in audits] == [
        ("needs_confirmation", "medium"),
        ("ok", "medium"),
    ]


def _risk_request(user="alice"):
    req = request("PUT", user=user)
    req.app.state.access_specs = {
        "google_calendar_delete_event": _spec(),
        "google_tasks_create_task": _spec(requires_confirmation=False),
        "google_calendar_list_events": _spec(risk=RiskLevel.READ),
    }
    return req


def test_risk_api_sets_and_clears_the_override(tiers):
    scope, alice, _ = tiers
    body = routes.TierBody(tool=TOOL, tier="medium")
    assert routes.set_tool_risk(body, _risk_request())["tier"] == "medium"
    with scope() as session:
        row = session.scalar(select(UserToolSetting))
        assert (row.user_id, row.tool_name, row.tier) == (alice, TOOL, "medium")
    # Back to the default removes the row instead of storing a redundant override.
    routes.set_tool_risk(routes.TierBody(tool=TOOL, tier="low"), _risk_request())
    with scope() as session:
        assert session.scalar(select(UserToolSetting)) is None


def test_risk_api_rejects_read_unknown_and_missing_csrf(tiers):
    for tool in ("google_calendar_list_events", "no_such_tool"):
        with pytest.raises(HTTPException) as exc:
            routes.set_tool_risk(routes.TierBody(tool=tool, tier="low"), _risk_request())
        assert exc.value.status_code == 400
    no_csrf = request("PUT", csrf=False)
    no_csrf.app.state.access_specs = {}
    with pytest.raises(HTTPException) as exc:
        routes.set_tool_risk(routes.TierBody(tool=TOOL, tier="low"), no_csrf)
    assert exc.value.status_code == 403
    with pytest.raises(ValidationError):
        routes.TierBody(tool=TOOL, tier="high")


def test_risk_override_is_per_user(tiers):
    scope, alice, bob = tiers
    routes.set_tool_risk(
        routes.TierBody(tool="google_calendar_delete_event", tier="low"), _risk_request("alice")
    )
    with scope() as session:
        spec = _spec()
        assert policy.effective_tier(session, alice, spec) is RiskTier.LOW
        assert policy.effective_tier(session, bob, spec) is RiskTier.MEDIUM
