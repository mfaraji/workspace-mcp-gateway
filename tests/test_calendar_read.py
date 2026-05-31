"""Tests for Google Calendar read tools."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from gateway.config import Settings
from gateway.providers.base import CallContext
from gateway.providers.google.calendar import common, read
from gateway.providers.registry import ToolError


class _FakeExecute:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeEvents:
    def __init__(self):
        self.list_kwargs = None

    def list(self, **kwargs):
        self.list_kwargs = kwargs
        return _FakeExecute({"items": []})


class _FakeCalendarService:
    def __init__(self):
        self.events_resource = _FakeEvents()

    def events(self):
        return self.events_resource


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
        base_url="https://mcp.ashpazi.shop",
        google_client_id="cid",
        google_client_secret="secret",
        token_encryption_key="x" * 43 + "=",
        gateway_shared_secret="shared-secret-value",
        trusted_open_webui_origin="https://openwebui.internal",
        session_secret="sess",
    )


def test_now_rfc3339_utc_uses_google_friendly_z_suffix():
    now = datetime(2026, 5, 31, 9, 30, tzinfo=UTC)

    assert read._now_rfc3339_utc(now) == "2026-05-31T09:30:00Z"


def test_list_events_defaults_to_upcoming_when_no_time_bounds(monkeypatch):
    service = _FakeCalendarService()
    monkeypatch.setattr(read, "calendar_service", lambda _session, _ctx: service)
    monkeypatch.setattr(
        read,
        "_now_rfc3339_utc",
        lambda: "2026-05-31T09:30:00Z",
    )
    args = read.ListEventsInput()
    ctx = CallContext(user_id=uuid.uuid4(), external_user_id="alice", request_id="r1")

    result = read.list_events(args, ctx, session=None)

    assert service.events_resource.list_kwargs["timeMin"] == "2026-05-31T09:30:00Z"
    assert result["date_context"] == {
        "defaulted_to_upcoming": True,
        "time_min": "2026-05-31T09:30:00Z",
        "guidance": (
            "Use system_get_current_time as the source of today's date for relative "
            "scheduling. Event dates in this result are not today's date."
        ),
    }


def test_list_events_can_include_past_when_explicitly_requested(monkeypatch):
    service = _FakeCalendarService()
    monkeypatch.setattr(read, "calendar_service", lambda _session, _ctx: service)
    args = read.ListEventsInput(include_past=True)
    ctx = CallContext(user_id=uuid.uuid4(), external_user_id="alice", request_id="r1")

    read.list_events(args, ctx, session=None)

    assert "timeMin" not in service.events_resource.list_kwargs


def test_calendar_service_reauth_error_includes_product_scoped_link(monkeypatch):
    monkeypatch.setattr(common, "get_settings", _settings)
    monkeypatch.setattr(
        common,
        "get_active_connection",
        lambda _session, _user_id, _provider: SimpleNamespace(scopes=[]),
    )
    ctx = CallContext(user_id=uuid.uuid4(), external_user_id="alice", request_id="r1")

    with pytest.raises(ToolError) as exc_info:
        common.calendar_service(session=None, ctx=ctx)

    assert exc_info.value.error_code == "reauth_required"
    message = str(exc_info.value)
    assert "reconnect here: https://mcp.ashpazi.shop/oauth/google/start?" in message
    assert "product=calendar" in message
