"""Google Calendar read tools.

Each tool resolves the calling user's active Google connection, builds an
authorized Calendar service (refreshing tokens as needed), calls the API, and
returns a trimmed, model-friendly result. All three are read-only and execute
directly through the registry pipeline.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gateway.providers.base import CallContext, RiskLevel, ToolSpec
from gateway.providers.google.calendar.common import PROVIDER, calendar_service, trim_event


class ListCalendarsInput(BaseModel):
    """No parameters — lists the user's calendars."""


class ListEventsInput(BaseModel):
    calendar_id: str = Field("primary", description="Calendar id, or 'primary'.")
    time_min: str | None = Field(
        None, description="RFC3339 lower bound for event start (inclusive)."
    )
    time_max: str | None = Field(
        None, description="RFC3339 upper bound for event start (exclusive)."
    )
    query: str | None = Field(None, description="Free-text search over events.")
    max_results: int = Field(25, ge=1, le=250, description="Max events to return.")
    single_events: bool = Field(True, description="Expand recurring events into instances.")
    order_by: str = Field("startTime", description="'startTime' or 'updated'.")


class GetEventInput(BaseModel):
    calendar_id: str = Field("primary", description="Calendar id, or 'primary'.")
    event_id: str = Field(..., description="The event id to fetch.")


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
    if args.query:
        params["q"] = args.query

    resp = service.events().list(**params).execute()
    events = [trim_event(e) for e in resp.get("items", [])]
    return {"calendar_id": args.calendar_id, "events": events}


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
        ToolSpec(
            name="google_calendar_list_calendars",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description="List the user's Google calendars.",
            input_model=ListCalendarsInput,
            handler=list_calendars,
        )
    )
    registry.add(
        ToolSpec(
            name="google_calendar_list_events",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description="List events on a Google calendar, optionally filtered by time or query.",
            input_model=ListEventsInput,
            handler=list_events,
        )
    )
    registry.add(
        ToolSpec(
            name="google_calendar_get_event",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description="Get a single Google calendar event by id.",
            input_model=GetEventInput,
            handler=get_event,
        )
    )
