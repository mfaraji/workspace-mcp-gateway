"""Tests for the confirmation-token policy seam and audit redaction."""

from __future__ import annotations

import uuid

from pydantic import BaseModel

from gateway.audit.log import summarize_input
from gateway.config import Settings
from gateway.policy import confirm
from gateway.providers.base import CallContext, RiskLevel, ToolSpec


def _settings(**overrides) -> Settings:
    base = dict(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
        base_url="http://localhost:8000",
        google_client_id="cid",
        google_client_secret="secret",
        token_encryption_key="x" * 43 + "=",
        trusted_open_webui_origin="https://openwebui.internal",
        session_secret="sess",
    )
    base.update(overrides)
    return Settings(**base)


class _In(BaseModel):
    event_id: str = "abc"


def _ctx() -> CallContext:
    return CallContext(user_id=uuid.uuid4(), external_user_id="alice", request_id="r1")


def _spec(risk: RiskLevel) -> ToolSpec:
    return ToolSpec(
        name="google_calendar_delete_event",
        provider="google",
        risk=risk,
        description="d",
        input_model=_In,
        handler=lambda *a: None,
    )


def test_read_tool_is_allowed_immediately():
    decision = confirm.check(_settings(), _spec(RiskLevel.READ), {}, _ctx())
    assert isinstance(decision, confirm.Allow)


def test_mutating_tool_requires_confirmation_then_executes():
    settings = _settings()
    spec = _spec(RiskLevel.MUTATING)
    args = {"event_id": "abc"}
    ctx = _ctx()

    first = confirm.check(settings, spec, args, ctx)
    assert isinstance(first, confirm.NeedsConfirmation)
    assert first.confirmation_token

    second = confirm.check(
        settings, spec, args, ctx, confirmation_token=first.confirmation_token
    )
    assert isinstance(second, confirm.Allow)


def test_confirmation_token_is_bound_to_args():
    settings = _settings()
    spec = _spec(RiskLevel.MUTATING)
    ctx = _ctx()
    first = confirm.check(settings, spec, {"event_id": "abc"}, ctx)
    assert isinstance(first, confirm.NeedsConfirmation)

    # Same token but different args must not be accepted.
    retry = confirm.check(
        settings, spec, {"event_id": "DIFFERENT"}, ctx,
        confirmation_token=first.confirmation_token,
    )
    assert isinstance(retry, confirm.NeedsConfirmation)


def test_audit_summary_redacts_freetext_and_unknown_fields():
    summary = summarize_input(
        "google_calendar_list_events",
        {"calendar_id": "primary", "query": "secret board meeting", "max_results": 10},
    )
    assert "primary" in summary
    assert "max_results=10" in summary
    assert "secret board meeting" not in summary  # value redacted
    assert "query=<set>" in summary  # presence only
