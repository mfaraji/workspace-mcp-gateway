"""Construction of authorized Google API clients from a stored connection."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from gateway.config import Settings
from gateway.db.models import ProviderConnection
from gateway.providers.google.connections import load_credentials


def _build_service(
    session: Session, conn: ProviderConnection, settings: Settings, api: str, version: str
):
    """Build an authorized Google API service, refreshing tokens as needed.

    Records ``last_used_at`` on the connection for every authorized client built.
    """
    from googleapiclient.discovery import build

    creds = load_credentials(session, conn, settings)
    conn.last_used_at = datetime.now(UTC)
    return build(api, version, credentials=creds, cache_discovery=False)


def build_calendar_service(session: Session, conn: ProviderConnection, settings: Settings):
    """Return an authorized Google Calendar v3 service for ``conn``."""
    return _build_service(session, conn, settings, "calendar", "v3")


def build_drive_service(session: Session, conn: ProviderConnection, settings: Settings):
    """Return an authorized Google Drive v3 service for ``conn``."""
    return _build_service(session, conn, settings, "drive", "v3")


def build_tasks_service(session: Session, conn: ProviderConnection, settings: Settings):
    """Return an authorized Google Tasks v1 service for ``conn``."""
    return _build_service(session, conn, settings, "tasks", "v1")
