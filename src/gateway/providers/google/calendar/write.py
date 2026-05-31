"""Google Calendar mutating tools: create, update, and delete events.

All three are ``MUTATING`` and therefore flow through the registry's confirmation
gate: the first call returns a preview plus a ``confirmation_token``; the model
must re-invoke with that token to actually apply the change.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gateway.providers.base import CallContext, RiskLevel, ToolSpec
from gateway.providers.google.calendar.common import PROVIDER, calendar_service, trim_event


class CreateEventInput(BaseModel):
    calendar_id: str = Field("primary", description="Calendar id, or 'primary'.")
    summary: str = Field(..., description="Event title.")
    start: str = Field(
        ...,
        description=(
            "Start time: RFC3339 datetime, or 'YYYY-MM-DD' for an all-day event. "
            "For relative dates like 'Monday at 10' or 'the following Monday', first "
            "resolve the date with system_get_current_time. Do not infer relative dates "
            "from existing calendar events."
        ),
    )
    end: str = Field(
        ...,
        description=(
            "End time: RFC3339 datetime, or 'YYYY-MM-DD' for an all-day event. "
            "For relative dates like 'Monday at 10' or 'the following Monday', first "
            "resolve the date with system_get_current_time. Do not infer relative dates "
            "from existing calendar events."
        ),
    )
    description: str | None = Field(None, description="Event description / notes.")
    location: str | None = Field(None, description="Event location.")
    attendees: list[str] = Field(default_factory=list, description="Attendee email addresses.")
    time_zone: str | None = Field(
        None, description="IANA timezone for timed events, e.g. 'America/Los_Angeles'."
    )
    allow_past_event: bool = Field(
        False,
        description=(
            "Set true only when the user explicitly asks to create a past or historical "
            "event. Relative scheduling requests should leave this false."
        ),
    )


class UpdateEventInput(BaseModel):
    calendar_id: str = Field("primary", description="Calendar id, or 'primary'.")
    event_id: str = Field(..., description="The event id to update.")
    summary: str | None = Field(None, description="New event title.")
    start: str | None = Field(
        None,
        description=(
            "New start (RFC3339 datetime or 'YYYY-MM-DD'). For relative dates, first "
            "resolve the date with system_get_current_time. Do not infer relative dates "
            "from existing calendar events."
        ),
    )
    end: str | None = Field(
        None,
        description=(
            "New end (RFC3339 datetime or 'YYYY-MM-DD'). For relative dates, first "
            "resolve the date with system_get_current_time. Do not infer relative dates "
            "from existing calendar events."
        ),
    )
    description: str | None = Field(None, description="New description.")
    location: str | None = Field(None, description="New location.")
    time_zone: str | None = Field(None, description="IANA timezone for timed start/end.")
    allow_past_event: bool = Field(
        False,
        description=(
            "Set true only when the user explicitly asks to move the event into the past. "
            "Relative scheduling requests should leave this false."
        ),
    )


class DeleteEventInput(BaseModel):
    calendar_id: str = Field("primary", description="Calendar id, or 'primary'.")
    event_id: str = Field(..., description="The event id to delete.")


def _time_field(value: str, time_zone: str | None) -> dict[str, Any]:
    """Build a Calendar start/end object from a date or datetime string."""
    # A bare 'YYYY-MM-DD' is an all-day event; anything else is a timed datetime.
    if len(value) == 10 and value.count("-") == 2:
        return {"date": value}
    field: dict[str, Any] = {"dateTime": value}
    if time_zone:
        field["timeZone"] = time_zone
    return field


def _zone_or_utc(time_zone: str | None):
    if not time_zone:
        return UTC
    try:
        return ZoneInfo(time_zone)
    except ZoneInfoNotFoundError:
        return UTC


def _is_all_day_date(value: str) -> bool:
    return len(value) == 10 and value.count("-") == 2


def _event_start_is_past(
    value: str, time_zone: str | None, now: datetime | None = None
) -> bool:
    zone = _zone_or_utc(time_zone)
    current = (now.astimezone(zone) if now else datetime.now(zone))

    if _is_all_day_date(value):
        try:
            return date.fromisoformat(value) < current.date()
        except ValueError:
            return False

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed < current.astimezone(parsed.tzinfo)


def _reject_unintentional_past_start(
    value: str, time_zone: str | None, allow_past_event: bool
) -> None:
    if allow_past_event or not _event_start_is_past(value, time_zone):
        return

    from gateway.providers.registry import ToolError

    raise ToolError(
        "past_event_date",
        (
            f"event start '{value}' is in the past relative to the current date/time. "
            "Resolve relative dates with system_get_current_time and retry with a future "
            "start. If the user explicitly requested a historical event, call again with "
            "allow_past_event=true."
        ),
    )


def create_event(args: CreateEventInput, ctx: CallContext, session: Session) -> dict[str, Any]:
    _reject_unintentional_past_start(args.start, args.time_zone, args.allow_past_event)
    service = calendar_service(session, ctx)
    body: dict[str, Any] = {
        "summary": args.summary,
        "start": _time_field(args.start, args.time_zone),
        "end": _time_field(args.end, args.time_zone),
    }
    if args.description is not None:
        body["description"] = args.description
    if args.location is not None:
        body["location"] = args.location
    if args.attendees:
        body["attendees"] = [{"email": e} for e in args.attendees]

    ev = service.events().insert(calendarId=args.calendar_id, body=body).execute()
    return {"status": "created", **trim_event(ev)}


def update_event(args: UpdateEventInput, ctx: CallContext, session: Session) -> dict[str, Any]:
    if args.start is not None:
        _reject_unintentional_past_start(args.start, args.time_zone, args.allow_past_event)
    service = calendar_service(session, ctx)
    # PATCH semantics: only send the fields the caller provided.
    body: dict[str, Any] = {}
    if args.summary is not None:
        body["summary"] = args.summary
    if args.description is not None:
        body["description"] = args.description
    if args.location is not None:
        body["location"] = args.location
    if args.start is not None:
        body["start"] = _time_field(args.start, args.time_zone)
    if args.end is not None:
        body["end"] = _time_field(args.end, args.time_zone)

    ev = (
        service.events()
        .patch(calendarId=args.calendar_id, eventId=args.event_id, body=body)
        .execute()
    )
    return {"status": "updated", **trim_event(ev)}


def delete_event(args: DeleteEventInput, ctx: CallContext, session: Session) -> dict[str, Any]:
    service = calendar_service(session, ctx)
    service.events().delete(calendarId=args.calendar_id, eventId=args.event_id).execute()
    return {"status": "deleted", "event_id": args.event_id, "calendar_id": args.calendar_id}


def _create_preview(args: CreateEventInput) -> str:
    return (
        f"Create event '{args.summary}' ({args.start} -> {args.end}) "
        f"on calendar '{args.calendar_id}'."
    )


def _update_preview(args: UpdateEventInput) -> str:
    return f"Update event '{args.event_id}' on calendar '{args.calendar_id}'."


def _delete_preview(args: DeleteEventInput) -> str:
    return f"Delete event '{args.event_id}' from calendar '{args.calendar_id}'."


def register(registry) -> None:
    """Register the Calendar mutating tools with the tool registry."""
    registry.add(
        ToolSpec(
            name="google_calendar_create_event",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            description=(
                "Create a Google calendar event. Requires confirmation. For relative "
                "dates, use system_get_current_time as the date anchor; never use "
                "existing calendar event dates as today's date."
            ),
            input_model=CreateEventInput,
            handler=create_event,
            preview_builder=_create_preview,
        )
    )
    registry.add(
        ToolSpec(
            name="google_calendar_update_event",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            description="Update fields of a Google calendar event. Requires confirmation.",
            input_model=UpdateEventInput,
            handler=update_event,
            preview_builder=_update_preview,
        )
    )
    registry.add(
        ToolSpec(
            name="google_calendar_delete_event",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            description="Delete a Google calendar event. Requires confirmation.",
            input_model=DeleteEventInput,
            handler=delete_event,
            preview_builder=_delete_preview,
        )
    )
