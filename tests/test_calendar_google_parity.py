"""Calendar parity with Google's Calendar MCP: new tools, bare-name mount and prompts."""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from gateway.audit.log import summarize_input
from gateway.connectors.registry import by_slug
from gateway.db.models import ClientGrant
from gateway.mcp.context import _current_user
from gateway.mcp.server import build_mcp, product_tool_filter
from gateway.providers import registry as pipeline
from gateway.providers.base import CallContext, RiskLevel, RiskTier
from gateway.providers.google.calendar import read, write
from gateway.providers.registry import ToolError, ToolRegistry
from tests.test_clerk_oauth import _settings
from tests.test_client_access import auth, store  # noqa: F401 (store is a fixture)

GOOGLE_TOOLS = {
    "get_event",
    "list_calendars",
    "list_events",
    "search_events",
    "suggest_time",
    "create_event",
    "delete_event",
    "respond_to_event",
    "update_event",
}
PROMPTS = {
    "schedule_meeting",
    "quick_meeting_today",
    "recurring_1on1",
    "team_meeting_scheduler",
    "analyze_availability",
}
TORONTO = ZoneInfo("America/Toronto")


def _ctx() -> CallContext:
    return CallContext(user_id=uuid.uuid4(), external_user_id="u", request_id="r")


def _calendar_mcp():
    return build_mcp(_settings(), product_tool_filter("calendar"), connector=by_slug("calendar"))


class _Call:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _Resource:
    """Records keyword args per method and returns canned results."""

    def __init__(self, results):
        self.results = results
        self.calls: dict[str, list[dict]] = {}

    def __getattr__(self, name):
        def call(**kwargs):
            self.calls.setdefault(name, []).append(kwargs)
            result = self.results[name]
            return _Call(result(**kwargs) if callable(result) else result)

        return call


class _Service:
    def __init__(self, *, events=None, freebusy=None, primary_tz="America/Toronto"):
        self.events_res = _Resource(events or {})
        self.freebusy_res = _Resource({"query": freebusy or {"calendars": {}}})
        self.cal_list_res = _Resource({"get": {"timeZone": primary_tz}})

    def events(self):
        return self.events_res

    def freebusy(self):
        return self.freebusy_res

    def calendarList(self):  # noqa: N802 (Google client API name)
        return self.cal_list_res


# --- search_events -------------------------------------------------------------------------


def test_search_events_passes_query_and_defaults_to_upcoming(monkeypatch):
    svc = _Service(events={"list": {"items": [{"id": "e1", "summary": "Standup"}]}})
    monkeypatch.setattr(read, "calendar_service", lambda *_: svc)
    out = read.search_events(read.SearchEventsInput(query="standup"), _ctx(), None)
    params = svc.events_res.calls["list"][0]
    assert params["q"] == "standup" and "timeMin" in params and params["singleEvents"] is True
    assert out["events"][0]["id"] == "e1"


def test_search_events_requires_a_query():
    with pytest.raises(ValueError):
        read.SearchEventsInput()
    with pytest.raises(ValueError):
        read.SearchEventsInput(query="")


# --- suggest_time --------------------------------------------------------------------------


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, day, hour, minute, tzinfo=TORONTO)


def test_free_slots_skip_busy_time_and_snap_to_the_grid():
    # Monday 2026-06-01: busy 09:00-10:00 and 10:10-11:00.
    busy = [(_at(1, 9), _at(1, 10)), (_at(1, 10, 10), _at(1, 11))]
    slots = read.free_slots(
        busy,
        window=(_at(1, 8), _at(1, 18)),
        zone=TORONTO,
        day_start=time(9),
        day_end=time(17),
        duration=timedelta(minutes=30),
        include_weekends=False,
        limit=3,
    )
    assert [(s.hour, s.minute) for s, _ in slots] == [(11, 0), (11, 30), (12, 0)]
    assert all(e - s == timedelta(minutes=30) for s, e in slots)


def test_free_slots_ignore_weekends_unless_asked():
    window = (_at(6, 8), _at(7, 18))  # Saturday-Sunday
    kwargs = dict(
        window=window,
        zone=TORONTO,
        day_start=time(9),
        day_end=time(17),
        duration=timedelta(minutes=60),
        limit=2,
    )
    assert read.free_slots([], include_weekends=False, **kwargs) == []
    assert len(read.free_slots([], include_weekends=True, **kwargs)) == 2


def test_free_slots_respect_the_window_start():
    slots = read.free_slots(
        [],
        window=(_at(1, 13, 5), _at(1, 18)),
        zone=TORONTO,
        day_start=time(9),
        day_end=time(17),
        duration=timedelta(minutes=30),
        include_weekends=False,
        limit=1,
    )
    assert (slots[0][0].hour, slots[0][0].minute) == (13, 15)


def test_suggest_time_queries_freebusy_for_attendees_and_reports_unreadable(monkeypatch):
    svc = _Service(
        freebusy={
            "calendars": {
                "primary": {
                    "busy": [{"start": "2026-06-01T13:00:00Z", "end": "2026-06-01T14:00:00Z"}]
                },
                "bob@example.com": {"busy": [], "errors": [{"reason": "notFound"}]},
            }
        }
    )
    monkeypatch.setattr(read, "calendar_service", lambda *_: svc)
    args = read.SuggestTimeInput(
        duration_minutes=30,
        time_min="2026-06-01T12:00:00Z",  # 08:00 Toronto
        time_max="2026-06-01T22:00:00Z",
        attendees=["bob@example.com"],
        max_suggestions=2,
    )
    out = read.suggest_time(args, _ctx(), None)
    body = svc.freebusy_res.calls["query"][0]["body"]
    assert [i["id"] for i in body["items"]] == ["primary", "bob@example.com"]
    assert body["timeZone"] == "America/Toronto"
    assert out["unavailable"] == ["bob@example.com"] and out["checked"] == ["primary"]
    # 09:00-10:00 Toronto is busy (13:00-14:00Z): first slot 10:00, then 10:30.
    starts = [s["start"][11:16] for s in out["suggestions"]]
    assert starts == ["10:00", "10:30"]


def test_suggest_time_validates_the_window(monkeypatch):
    monkeypatch.setattr(read, "calendar_service", lambda *_: _Service())
    bad = [
        dict(time_min="2026-06-02T00:00:00Z", time_max="2026-06-01T00:00:00Z"),
        dict(time_min="2026-06-01T00:00:00Z", time_max="2026-09-01T00:00:00Z"),
        dict(working_hours_start="17:00", working_hours_end="09:00"),
        dict(time_zone="Mars/Olympus"),
        dict(time_min="not-a-date"),
    ]
    for extra in bad:
        with pytest.raises(ToolError) as e:
            read.suggest_time(read.SuggestTimeInput(**extra), _ctx(), None)
        assert e.value.error_code == "invalid_input"


# --- respond_to_event ----------------------------------------------------------------------


def _event(attendees, **extra):
    return {"id": "e1", "summary": "Sync", "attendees": attendees, **extra}


def test_respond_updates_only_own_attendee_and_notifies(monkeypatch):
    attendees = [
        {"email": "org@example.com", "organizer": True, "responseStatus": "accepted"},
        {"email": "me@example.com", "self": True, "responseStatus": "needsAction"},
    ]
    svc = _Service(
        events={"get": _event(attendees), "patch": lambda **k: _event(k["body"]["attendees"])}
    )
    monkeypatch.setattr(write, "calendar_service", lambda *_: svc)
    args = write.RespondToEventInput(event_id="e1", response="declined", comment="Out sick")
    out = write.respond_to_event(args, _ctx(), None)
    patch = svc.events_res.calls["patch"][0]
    assert patch["sendUpdates"] == "all"
    sent = patch["body"]["attendees"]
    assert sent[0] == attendees[0] and sent[1]["responseStatus"] == "declined"
    assert sent[1]["comment"] == "Out sick"
    assert out["status"] == "responded" and out["response"] == "declined"
    assert attendees[1]["responseStatus"] == "needsAction"  # input not mutated


def test_respond_rejects_non_attendee_and_truncated_attendee_lists(monkeypatch):
    for event in (
        _event([{"email": "org@example.com", "organizer": True}]),
        _event([{"email": "me@example.com", "self": True}], attendeesOmitted=True),
    ):
        svc = _Service(events={"get": event})
        monkeypatch.setattr(write, "calendar_service", lambda *_, s=svc: s)
        with pytest.raises(ToolError) as e:
            write.respond_to_event(
                write.RespondToEventInput(event_id="e1", response="accepted"), _ctx(), None
            )
        assert e.value.error_code == "invalid_input"
        assert "patch" not in svc.events_res.calls


def test_respond_input_validation_and_preview():
    with pytest.raises(ValueError):
        write.RespondToEventInput(event_id="e1", response="maybe")
    args = write.RespondToEventInput(event_id="e1", response="tentative")
    assert "organizer will be notified" in write._respond_preview(args)
    quiet = write.RespondToEventInput(event_id="e1", response="tentative", send_updates="none")
    assert "notified" not in write._respond_preview(quiet)


# --- registration, tiers and audit ---------------------------------------------------------


def test_new_tools_are_registered_with_expected_risk_and_tier():
    registry = ToolRegistry()
    read.register(registry)
    write.register(registry)
    specs = {s.name: s for s in registry._specs}
    assert specs["google_calendar_search_events"].risk is RiskLevel.READ
    assert specs["google_calendar_suggest_time"].risk is RiskLevel.READ
    respond = specs["google_calendar_respond_to_event"]
    assert respond.risk is RiskLevel.MUTATING and respond.default_tier is RiskTier.MEDIUM
    assert respond.preview_builder is not None


def test_audit_summaries_never_record_free_text():
    search = summarize_input(
        "google_calendar_search_events", {"query": "layoffs plan", "calendar_id": "primary"}
    )
    assert "layoffs" not in search and "calendar_id='primary'" in search
    suggest = summarize_input(
        "google_calendar_suggest_time",
        {"attendees": ["ceo@example.com"], "duration_minutes": 30, "time_zone": "UTC"},
    )
    assert "ceo@example.com" not in suggest and "duration_minutes=30" in suggest
    respond = summarize_input(
        "google_calendar_respond_to_event",
        {"event_id": "e1", "response": "declined", "comment": "private reason"},
    )
    assert "private reason" not in respond and "response='declined'" in respond


# --- the Google-compatible mount -----------------------------------------------------------


def test_calendar_mount_exposes_exactly_googles_tools_under_bare_names():
    mcp = _calendar_mcp()
    assert set(mcp._tool_manager._tools) == GOOGLE_TOOLS | {"system_get_current_time"}
    assert {
        s.name for s in mcp.gateway_exposed.values() if s.name != "system_get_current_time"
    } == {f"google_calendar_{n}" for n in GOOGLE_TOOLS}


def test_other_mounts_keep_prefixed_names_extras_and_have_no_prompts():
    plain = build_mcp(_settings(), product_tool_filter("calendar"))
    names = set(plain._tool_manager._tools)
    assert "google_calendar_list_accounts" in names and "google_calendar_get_event" in names
    assert not (names & GOOGLE_TOOLS)
    assert plain._prompt_manager.list_prompts() == []
    assert not any(n in build_mcp(_settings())._tool_manager._tools for n in GOOGLE_TOOLS)


@pytest.mark.asyncio
async def test_bare_name_call_runs_the_internal_spec(monkeypatch):
    seen = []
    monkeypatch.setattr(
        pipeline,
        "_run_pipeline",
        lambda spec, settings, args, token, exposed=None: seen.append((spec.name, args, exposed)),
    )
    mcp = _calendar_mcp()
    await mcp.call_tool("get_event", {"event_id": "e1"})
    await mcp.call_tool("respond_to_event", {"event_id": "e1", "response": "accepted"})
    assert [n for n, *_ in seen] == [
        "google_calendar_get_event",
        "google_calendar_respond_to_event",
    ]
    assert seen[0][1]["event_id"] == "e1"
    # Runtime text shown to the model uses the name this mount advertises.
    assert [e for *_, e in seen] == ["get_event", "respond_to_event"]


@pytest.mark.asyncio
async def test_discovery_on_the_bare_mount_is_gated_by_internal_grants(store, monkeypatch):  # noqa: F811
    scope, alice, _ = store
    monkeypatch.setattr("gateway.db.engine.session_scope", scope)
    mcp = _calendar_mcp()
    token = _current_user.set(auth())
    try:
        assert {t.name for t in await mcp.list_tools()} == {"system_get_current_time"}
        with scope() as session:
            session.add(
                ClientGrant(
                    user_id=alice,
                    issuer=_settings().clerk_issuer,
                    client_id="client-a",
                    tools=["google_calendar_search_events", "google_calendar_list_events"],
                )
            )
        assert {t.name for t in await mcp.list_tools()} == {
            "system_get_current_time",
            "search_events",
            "list_events",
        }
    finally:
        _current_user.reset(token)


# --- prompts -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calendar_mount_serves_the_five_prompts():
    mcp = _calendar_mcp()
    assert {p.name for p in await mcp.list_prompts()} == PROMPTS
    result = await mcp.get_prompt(
        "schedule_meeting",
        {"title": "Roadmap", "attendees": "a@example.com", "duration_minutes": "45"},
    )
    text = result.messages[0].content.text
    assert result.messages[0].role == "user"
    assert "Roadmap" in text and "45 minutes" in text and "a@example.com" in text
    for bare in ("suggest_time", "create_event", "system_get_current_time"):
        assert bare in text
    assert "google_calendar_" not in text and "explicit yes" in text


@pytest.mark.asyncio
async def test_every_prompt_renders_with_only_its_required_arguments():
    mcp = _calendar_mcp()
    required = {
        "schedule_meeting": {"title": "T"},
        "quick_meeting_today": {"title": "T"},
        "recurring_1on1": {"attendee": "p@example.com"},
        "team_meeting_scheduler": {"title": "T", "team_members": "a@x.com, b@x.com"},
        "analyze_availability": {},
    }
    assert set(required) == PROMPTS
    for name, args in required.items():
        text = (await mcp.get_prompt(name, args)).messages[0].content.text
        assert text and "google_calendar_" not in text
    read_only = (await mcp.get_prompt("analyze_availability", {})).messages[0].content.text
    assert "create_event" not in read_only.replace("do not create", "")
