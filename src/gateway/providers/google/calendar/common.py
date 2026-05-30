"""Shared helpers for the Google Calendar tool modules."""

from __future__ import annotations

from sqlalchemy.orm import Session

from gateway.config import get_settings
from gateway.providers.base import CallContext
from gateway.providers.google.client import build_calendar_service
from gateway.providers.google.connections import get_active_connection

PROVIDER = "google"


def calendar_service(session: Session, ctx: CallContext):
    """Resolve the caller's active Google connection and build a Calendar service."""
    from gateway.providers.registry import ToolError

    conn = get_active_connection(session, ctx.user_id, PROVIDER)
    if conn is None:
        raise ToolError(
            "not_connected",
            "no active Google connection; authorize via /oauth/google/start",
        )
    return build_calendar_service(session, conn, get_settings())


def trim_event(ev: dict) -> dict:
    """Trim a Google event resource to a compact, model-friendly shape."""
    return {
        "id": ev.get("id"),
        "summary": ev.get("summary"),
        "start": ev.get("start"),
        "end": ev.get("end"),
        "location": ev.get("location"),
        "attendees_count": len(ev.get("attendees", [])),
        "html_link": ev.get("htmlLink"),
        "status": ev.get("status"),
    }
