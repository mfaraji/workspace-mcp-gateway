"""The list of connectors this gateway exposes.

Adding a new connector: define its provider package(s) with a
``register(registry)`` entrypoint (following the pattern in
``providers/apex`` or ``providers/google/calendar``), then add one
:class:`Connector` entry below. Mounting (``app.py``), tool filtering
(``mcp/server.py``), and connect-URL/error-code guidance (``providers/registry.py``)
all derive from this list — nothing else needs to change.
"""

from __future__ import annotations

from gateway.connectors.base import Connector
from gateway.providers.apex import auth as apex_auth
from gateway.providers.apex import read as apex_read
from gateway.providers.apex import write as apex_write
from gateway.providers.apex.client import APEX_API_KEY
from gateway.providers.base import ToolSpec
from gateway.providers.google.calendar import read as google_calendar_read
from gateway.providers.google.calendar import write as google_calendar_write
from gateway.providers.google.calendar.common import CALENDAR_OAUTH
from gateway.providers.google.drive import read as google_drive_read
from gateway.providers.google.drive.common import DRIVE_OAUTH
from gateway.providers.google.tasks import read as google_tasks_read
from gateway.providers.google.tasks import write as google_tasks_write
from gateway.providers.google.tasks.common import TASKS_OAUTH


def _register_calendar(registry) -> None:
    google_calendar_read.register(registry)
    google_calendar_write.register(registry)


def _register_drive(registry) -> None:
    google_drive_read.register(registry)


def _register_tasks(registry) -> None:
    google_tasks_read.register(registry)
    google_tasks_write.register(registry)


def _register_apex(registry) -> None:
    apex_read.register(registry)
    apex_write.register(registry)
    apex_auth.register(registry)


def _google_classify_error(exc: Exception) -> str | None:
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        return None
    if not isinstance(exc, HttpError):
        return None
    status = getattr(exc.resp, "status", None)
    return {
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        429: "rate_limited",
    }.get(int(status) if status else 0, "provider_error")


CONNECTORS: list[Connector] = [
    Connector(
        slug="calendar",
        tool_prefix="google_calendar_",
        display_name="Google Calendar",
        upstream_provider="google",
        register=_register_calendar,
        upstream=CALENDAR_OAUTH,
        error_classifier=_google_classify_error,
    ),
    Connector(
        slug="drive",
        tool_prefix="google_drive_",
        display_name="Google Drive",
        upstream_provider="google",
        register=_register_drive,
        upstream=DRIVE_OAUTH,
        error_classifier=_google_classify_error,
    ),
    Connector(
        slug="tasks",
        tool_prefix="google_tasks_",
        display_name="Google Tasks",
        upstream_provider="google",
        register=_register_tasks,
        upstream=TASKS_OAUTH,
        error_classifier=_google_classify_error,
    ),
    Connector(
        slug="apex",
        tool_prefix="apex_",
        display_name="Apex",
        upstream_provider="apex",
        register=_register_apex,
        upstream=APEX_API_KEY,
    ),
]


def by_slug(slug: str) -> Connector | None:
    """Return the connector mounted at ``/mcp/<slug>``, or ``None``."""
    return next((c for c in CONNECTORS if c.slug == slug), None)


def by_spec(spec: ToolSpec) -> Connector | None:
    """Return the connector that owns a tool spec, by name-prefix match, or ``None``."""
    return next((c for c in CONNECTORS if c.matches(spec)), None)
