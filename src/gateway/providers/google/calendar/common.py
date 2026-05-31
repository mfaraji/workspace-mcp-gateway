"""Shared helpers for the Google Calendar tool modules."""

from __future__ import annotations

from sqlalchemy.orm import Session

from gateway.config import get_settings
from gateway.oauth.google import CALENDAR_SCOPES, build_start_url
from gateway.providers.base import CallContext
from gateway.providers.google.client import build_calendar_service
from gateway.providers.google.connections import get_active_connection

PROVIDER = "google"


def calendar_service(session: Session, ctx: CallContext):
    """Resolve the caller's active Google connection and build a Calendar service."""
    from gateway.providers.registry import ToolError

    settings = get_settings()
    conn = get_active_connection(session, ctx.user_id, PROVIDER)
    if conn is None:
        connect_url = build_start_url(settings, ctx.external_user_id, product="calendar")
        raise ToolError(
            "not_connected",
            f"no active Google connection; authorize here: {connect_url}",
        )
    missing_scopes = sorted(set(CALENDAR_SCOPES) - set(conn.scopes or []))
    if missing_scopes:
        connect_url = build_start_url(settings, ctx.external_user_id, product="calendar")
        raise ToolError(
            "reauth_required",
            (
                "Google Calendar connection is missing required scopes; reconnect here: "
                f"{connect_url}"
            ),
        )
    return build_calendar_service(session, conn, settings)


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
