"""Tests for Google Calendar read tools."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from gateway.providers.base import CallContext
from gateway.providers.google.calendar import read


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
