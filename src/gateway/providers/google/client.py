"""Construction of authorized Google API clients from resolved credentials."""

from __future__ import annotations


def _build_service(creds, api: str, version: str):
    """Build an authorized Google API service from already-refreshed credentials."""
    from googleapiclient.discovery import build

    return build(api, version, credentials=creds, cache_discovery=False)


def build_calendar_service(creds):
    """Return an authorized Google Calendar v3 service for ``creds``."""
    return _build_service(creds, "calendar", "v3")


def build_drive_service(creds):
    """Return an authorized Google Drive v3 service for ``creds``."""
    return _build_service(creds, "drive", "v3")


def build_tasks_service(creds):
    """Return an authorized Google Tasks v1 service for ``creds``."""
    return _build_service(creds, "tasks", "v1")
