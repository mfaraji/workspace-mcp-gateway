"""Shared helpers for the Google Calendar tool modules."""

from __future__ import annotations

from sqlalchemy.orm import Session

from gateway.config import get_settings
from gateway.connectors.upstream import OAuthStrategy
from gateway.oauth.google import CALENDAR_SCOPES
from gateway.providers.base import CallContext
from gateway.providers.google.client import build_calendar_service

PROVIDER = "google"

CALENDAR_OAUTH = OAuthStrategy(
    upstream_provider=PROVIDER,
    required_scopes=CALENDAR_SCOPES,
    display_name="Google Calendar",
    product="calendar",
)


def calendar_service(session: Session, ctx: CallContext):
    """Resolve the caller's active Google connection and build a Calendar service."""
    creds = CALENDAR_OAUTH.get_credentials(session, ctx, get_settings())
    return build_calendar_service(creds)


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
