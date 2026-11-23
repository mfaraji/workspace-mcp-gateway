"""Google Calendar read tools.

Each tool resolves the calling user's active Google connection, builds an
authorized Calendar service (refreshing tokens as needed), calls the API, and
returns a trimmed, model-friendly result. All three are read-only and execute
directly through the registry pipeline.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field
from sqlalchemy.orm import Session

from gateway.providers.base import CallContext, RiskLevel, ToolSpec
from gateway.providers.google.calendar.common import (
    CALENDAR_UI,
    PROVIDER,
    UI_NOTE,
    calendar_service,
    trim_event,
)
from gateway.providers.google.common import GoogleAccountInput, make_list_accounts_spec
from gateway.providers.registry import ToolError


class ListCalendarsInput(GoogleAccountInput):
    """No parameters — lists the user's calendars."""


class ListEventsInput(GoogleAccountInput):
    calendar_id: str = Field("primary", description="Calendar id, or 'primary'.")
    time_min: str | None = Field(
        None,
        description=(
            "RFC3339 lower bound for event start (inclusive). Use system_get_current_time "
            "to choose this for relative or upcoming-date requests."
        ),
    )
    time_max: str | None = Field(
        None, description="RFC3339 upper bound for event start (exclusive)."
    )
    query: str | None = Field(None, description="Free-text search over events.")
    max_results: int = Field(25, ge=1, le=250, description="Max events to return.")
    single_events: bool = Field(True, description="Expand recurring events into instances.")
    order_by: str = Field("startTime", description="'startTime' or 'updated'.")
    include_past: bool = Field(
        False,
        description=(
            "Set true only when the user explicitly asks for past or historical events. "
            "When false and no time_min/time_max is supplied, results default to upcoming "
            "events starting from the current time."
        ),
    )


class GetEventInput(GoogleAccountInput):
    calendar_id: str = Field("primary", description="Calendar id, or 'primary'.")
    event_id: str = Field(..., description="The event id to fetch.")


class SearchEventsInput(ListEventsInput):
    """``list_events`` with a required free-text ``query`` (Google's ``search_events``)."""

    query: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Free-text search over event title, description, location and attendees.",
    )


class SuggestTimeInput(GoogleAccountInput):
    duration_minutes: int = Field(30, ge=5, le=480, description="Meeting length in minutes.")
    time_min: str | None = Field(
        None,
        description=(
            "RFC3339 start of the search window. Defaults to now; use system_get_current_time "
            "to resolve relative dates."
        ),
    )
    time_max: str | None = Field(
        None, description="RFC3339 end of the search window. Defaults to 7 days after time_min."
    )
    attendees: list[str] | None = Field(
        None,
        max_length=20,
        description=(
            "Email addresses whose free/busy to include. Calendars you cannot read are "
            "reported in `unavailable` and treated as free, so confirm with them."
        ),
    )
    calendar_ids: list[str] | None = Field(
        None, max_length=20, description="Your own calendars to check. Defaults to 'primary'."
    )
    working_hours_start: str = Field("09:00", pattern=r"^\d{2}:\d{2}$")
    working_hours_end: str = Field("17:00", pattern=r"^\d{2}:\d{2}$")
    time_zone: str | None = Field(
        None,
        description=(
            "IANA timezone used for working hours, e.g. 'America/Toronto'. Defaults to the "
            "primary calendar's timezone."
        ),
    )
    include_weekends: bool = Field(False, description="Also suggest Saturdays and Sundays.")
    max_suggestions: int = Field(5, ge=1, le=20, description="How many slots to return.")


def list_calendars(_: ListCalendarsInput, ctx: CallContext, session: Session) -> dict[str, Any]:
    service = calendar_service(session, ctx)
    resp = service.calendarList().list().execute()
    calendars = [
        {
            "id": c.get("id"),
            "summary": c.get("summary"),
            "primary": c.get("primary", False),
            "access_role": c.get("accessRole"),
        }
        for c in resp.get("items", [])
    ]
    return {"calendars": calendars}


def list_events(args: ListEventsInput, ctx: CallContext, session: Session) -> dict[str, Any]:
    service = calendar_service(session, ctx)
    params: dict[str, Any] = {
        "calendarId": args.calendar_id,
        "maxResults": args.max_results,
        "singleEvents": args.single_events,
    }
    # Google requires singleEvents=True when ordering by startTime.
    params["orderBy"] = args.order_by if args.single_events else "updated"
    if args.time_min:
        params["timeMin"] = args.time_min
    if args.time_max:
        params["timeMax"] = args.time_max
    if not args.include_past and not args.time_min and not args.time_max:
        params["timeMin"] = _now_rfc3339_utc()
    if args.query:
        params["q"] = args.query

    resp = service.events().list(**params).execute()
    events = [trim_event(e) for e in resp.get("items", [])]
    return {
        "calendar_id": args.calendar_id,
        "events": events,
        "date_context": {
            "defaulted_to_upcoming": (
                not args.include_past and not args.time_min and not args.time_max
            ),
            "time_min": params.get("timeMin"),
            "guidance": (
                "Use system_get_current_time as the source of today's date for relative "
                "scheduling. Event dates in this result are not today's date."
            ),
        },
    }


def search_events(args: SearchEventsInput, ctx: CallContext, session: Session) -> dict[str, Any]:
    return list_events(args, ctx, session)


_SLOT_GRID = timedelta(minutes=15)
_MAX_WINDOW = timedelta(days=60)


def _parse_rfc3339(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ToolError("invalid_input", f"{field} must be an RFC3339 datetime") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _hhmm(value: str, field: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError:
        raise ToolError("invalid_input", f"{field} must be HH:MM") from None


def _merge_busy(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _round_up(moment: datetime) -> datetime:
    """Round up to the next quarter hour (in the moment's own timezone)."""
    base = moment.replace(minute=0, second=0, microsecond=0)
    steps = -(-(moment - base) // _SLOT_GRID)
    return base + steps * _SLOT_GRID


def free_slots(
    busy: list[tuple[datetime, datetime]],
    *,
    window: tuple[datetime, datetime],
    zone: ZoneInfo,
    day_start: time,
    day_end: time,
    duration: timedelta,
    include_weekends: bool,
    limit: int,
) -> list[tuple[datetime, datetime]]:
    """Earliest-first free slots of ``duration`` inside working hours, off a 15-minute grid."""
    merged = _merge_busy(busy)
    slots: list[tuple[datetime, datetime]] = []
    day: date = window[0].astimezone(zone).date()
    last_day = window[1].astimezone(zone).date()
    while day <= last_day and len(slots) < limit:
        if include_weekends or day.weekday() < 5:
            lo = max(datetime.combine(day, day_start, zone), window[0])
            hi = min(datetime.combine(day, day_end, zone), window[1])
            cursor = _round_up(lo.astimezone(zone))
            while cursor + duration <= hi and len(slots) < limit:
                end = cursor + duration
                clash = next((b for b in merged if b[0] < end and b[1] > cursor), None)
                if clash is None:
                    slots.append((cursor, end))
                    cursor = end
                else:
                    cursor = _round_up(clash[1].astimezone(zone))
        day += timedelta(days=1)
    return slots


def suggest_time(args: SuggestTimeInput, ctx: CallContext, session: Session) -> dict[str, Any]:
    start = _parse_rfc3339(args.time_min, "time_min") if args.time_min else datetime.now(UTC)
    end = (
        _parse_rfc3339(args.time_max, "time_max") if args.time_max else start + timedelta(days=7)
    )
    if end <= start:
        raise ToolError("invalid_input", "time_max must be after time_min")
    if end - start > _MAX_WINDOW:
        raise ToolError("invalid_input", "the search window may span at most 60 days")
    day_start = _hhmm(args.working_hours_start, "working_hours_start")
    day_end = _hhmm(args.working_hours_end, "working_hours_end")
    if day_end <= day_start:
        raise ToolError("invalid_input", "working_hours_end must be after working_hours_start")

    service = calendar_service(session, ctx)
    tz_name = args.time_zone
    if not tz_name:
        primary = service.calendarList().get(calendarId="primary").execute()
        tz_name = primary.get("timeZone") or "UTC"
    try:
        zone = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        raise ToolError("invalid_input", f"unknown time zone '{tz_name}'") from None

    ids = list(dict.fromkeys([*(args.calendar_ids or ["primary"]), *(args.attendees or [])]))
    resp = (
        service.freebusy()
        .query(
            body={
                "timeMin": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "timeMax": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "timeZone": tz_name,
                "items": [{"id": i} for i in ids],
            }
        )
        .execute()
    )
    busy: list[tuple[datetime, datetime]] = []
    unavailable: list[str] = []
    for cal_id, info in (resp.get("calendars") or {}).items():
        if info.get("errors"):
            unavailable.append(cal_id)
        for b in info.get("busy", []) or []:
            busy.append((_parse_rfc3339(b["start"], "busy"), _parse_rfc3339(b["end"], "busy")))

    slots = free_slots(
        busy,
        window=(start, end),
        zone=zone,
        day_start=day_start,
        day_end=day_end,
        duration=timedelta(minutes=args.duration_minutes),
        include_weekends=args.include_weekends,
        limit=args.max_suggestions,
    )
    return {
        "time_zone": tz_name,
        "duration_minutes": args.duration_minutes,
        "suggestions": [{"start": a.isoformat(), "end": b.isoformat()} for a, b in slots],
        "checked": [i for i in ids if i not in unavailable],
        "unavailable": unavailable,
    }


def _now_rfc3339_utc(now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    return current.astimezone(UTC).isoformat().replace("+00:00", "Z")


def get_event(args: GetEventInput, ctx: CallContext, session: Session) -> dict[str, Any]:
    service = calendar_service(session, ctx)
    ev = service.events().get(calendarId=args.calendar_id, eventId=args.event_id).execute()
    detail = trim_event(ev)
    detail["description"] = ev.get("description")
    detail["organizer"] = ev.get("organizer")
    return detail


def register(registry) -> None:
    """Register the Calendar read tools with the tool registry."""
    registry.add(
        make_list_accounts_spec(
            name="google_calendar_list_accounts",
            provider=PROVIDER,
            product="calendar",
            display_name="Google Calendar",
        )
    )
    registry.add(
        ToolSpec(
            name="google_calendar_list_calendars",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description="List the user's Google calendars.",
            input_model=ListCalendarsInput,
            handler=list_calendars,
            ui_app_callable=True,
        )
    )
    registry.add(
        ToolSpec(
            name="google_calendar_list_events",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "List events on a Google calendar, optionally filtered by time or query. "
                "Do not use event dates returned by this tool as today's date; use "
                "system_get_current_time for current-date and relative-date context. "
                + UI_NOTE
            ),
            input_model=ListEventsInput,
            handler=list_events,
            ui_resource=CALENDAR_UI,
            ui_app_callable=True,
        )
    )
    registry.add(
        ToolSpec(
            name="google_calendar_search_events",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "Search a Google calendar's events by free text (title, description, "
                "location, attendees), optionally within a time range. Defaults to upcoming "
                "events; use system_get_current_time for relative dates. " + UI_NOTE
            ),
            input_model=SearchEventsInput,
            handler=search_events,
            ui_resource=CALENDAR_UI,
            ui_app_callable=True,
        )
    )
    registry.add(
        ToolSpec(
            name="google_calendar_suggest_time",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "Suggest meeting times: finds the earliest free slots of a given length "
                "within working hours, using free/busy for the user's calendars and optional "
                "attendees. Attendees whose calendars are not readable are listed in "
                "`unavailable` and treated as free. Does not create anything."
            ),
            input_model=SuggestTimeInput,
            handler=suggest_time,
        )
    )
    registry.add(
        ToolSpec(
            name="google_calendar_get_event",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description="Get a single Google calendar event by id. " + UI_NOTE,
            input_model=GetEventInput,
            handler=get_event,
            ui_resource=CALENDAR_UI,
            ui_app_callable=True,
        )
    )
