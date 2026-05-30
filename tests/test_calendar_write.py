"""Tests for Calendar mutating tools: previews, time fields, audit redaction."""

from __future__ import annotations

import uuid

from gateway.audit.log import summarize_input
from gateway.config import Settings
from gateway.policy import confirm
from gateway.providers.base import CallContext, RiskLevel
from gateway.providers.google.calendar import write
from gateway.providers.registry import ToolRegistry


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


def _registered() -> dict[str, object]:
    registry = ToolRegistry()
    write.register(registry)
    return {spec.name: spec for spec in registry._specs}


def test_write_tools_are_registered_as_mutating():
    specs = _registered()
    assert set(specs) == {
        "google_calendar_create_event",
        "google_calendar_update_event",
        "google_calendar_delete_event",
    }
    assert all(s.risk is RiskLevel.MUTATING for s in specs.values())
    assert all(s.preview_builder is not None for s in specs.values())


def test_create_preview_includes_summary_but_audit_does_not():
    spec = _registered()["google_calendar_create_event"]
    args = {
        "calendar_id": "primary",
        "summary": "Secret board meeting",
        "start": "2026-06-01T10:00:00-07:00",
        "end": "2026-06-01T11:00:00-07:00",
        "description": "very sensitive notes",
    }
    model = spec.input_model(**args)

    # The human-facing preview is allowed to show the title.
    preview = spec.preview_builder(model)
    assert "Secret board meeting" in preview

    # The persisted audit summary must not leak summary/description free-text.
    summary = summarize_input("google_calendar_create_event", args)
    assert "Secret board meeting" not in summary
    assert "very sensitive notes" not in summary
    assert "calendar_id='primary'" in summary
    assert "start=" in summary


def test_time_field_distinguishes_all_day_from_timed():
    assert write._time_field("2026-06-01", None) == {"date": "2026-06-01"}
    assert write._time_field("2026-06-01T10:00:00-07:00", "America/Los_Angeles") == {
        "dateTime": "2026-06-01T10:00:00-07:00",
        "timeZone": "America/Los_Angeles",
    }


def test_delete_event_requires_then_accepts_confirmation():
    settings = _settings()
    spec = _registered()["google_calendar_delete_event"]
    args = {"calendar_id": "primary", "event_id": "evt123"}
    ctx = CallContext(user_id=uuid.uuid4(), external_user_id="alice", request_id="r1")

    first = confirm.check(
        settings,
        spec,
        args,
        ctx,
        preview_builder=lambda _: spec.preview_builder(spec.input_model(**args)),
    )
    assert isinstance(first, confirm.NeedsConfirmation)
    assert "evt123" in first.preview

    second = confirm.check(
        settings, spec, args, ctx, confirmation_token=first.confirmation_token
    )
    assert isinstance(second, confirm.Allow)
