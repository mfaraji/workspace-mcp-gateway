"""The list of connectors this gateway exposes.

Adding a new connector: define its provider package(s) with a
``register(registry)`` entrypoint (following the pattern in
``providers/apex`` or ``providers/google/calendar``), then add one
:class:`Connector` entry below. Mounting (``app.py``), tool filtering
(``mcp/server.py``), and connect-URL/error-code guidance (``providers/registry.py``)
all derive from this list — nothing else needs to change.
"""

from __future__ import annotations

from gateway.config import Settings
from gateway.connectors.base import AccessMode, Bundle, Connector
from gateway.providers.apex import auth as apex_auth
from gateway.providers.apex import expenses as apex_expenses
from gateway.providers.apex import read as apex_read
from gateway.providers.apex import transactions as apex_transactions
from gateway.providers.apex import wealth as apex_wealth
from gateway.providers.apex import write as apex_write
from gateway.providers.apex.client import (
    APEX_UPSTREAM,
    APEX_WEALTH_UPSTREAM,
)
from gateway.providers.base import ToolSpec
from gateway.providers.finance import read as finance_read
from gateway.providers.google.calendar import read as google_calendar_read
from gateway.providers.google.calendar import write as google_calendar_write
from gateway.providers.google.calendar.common import CALENDAR_OAUTH
from gateway.providers.google.drive import read as google_drive_read
from gateway.providers.google.drive import write as google_drive_write
from gateway.providers.google.drive.common import DRIVE_OAUTH
from gateway.providers.google.gmail import manage as google_gmail_manage
from gateway.providers.google.gmail import read as google_gmail_read
from gateway.providers.google.gmail import write as google_gmail_write
from gateway.providers.google.gmail.common import GMAIL_OAUTH
from gateway.providers.google.tasks import read as google_tasks_read
from gateway.providers.google.tasks import write as google_tasks_write
from gateway.providers.google.tasks.common import TASKS_OAUTH
from gateway.providers.recipes import read as recipes_read
from gateway.providers.recipes import write as recipes_write
from gateway.providers.recipes.common import classify_storage_error
from gateway.providers.registry import ToolRegistry
from gateway.providers.telegram import read as telegram_read
from gateway.providers.telegram import write as telegram_write
from gateway.providers.telegram.client import TELEGRAM_API_KEY


def _register_calendar(registry: ToolRegistry, _settings: Settings) -> None:
    google_calendar_read.register(registry)
    google_calendar_write.register(registry)


def _register_drive(registry: ToolRegistry, _settings: Settings) -> None:
    google_drive_read.register(registry)
    google_drive_write.register(registry)


def _register_tasks(registry: ToolRegistry, _settings: Settings) -> None:
    google_tasks_read.register(registry)
    google_tasks_write.register(registry)


def _register_gmail(registry: ToolRegistry, _settings: Settings) -> None:
    google_gmail_read.register(registry)
    google_gmail_write.register(registry)
    google_gmail_manage.register(registry)


def _google_names(prefix: str, names: list[str], **overrides: str) -> dict[str, str]:
    """Internal ``<prefix><name>`` -> Google's bare ``<name>`` (``overrides``: internal -> bare)."""
    mapping = {f"{prefix}{n}": n for n in names}
    mapping.update({f"{prefix}{k}": v for k, v in overrides.items()})
    return mapping


# Exactly the tools Google's own MCP servers expose (names verbatim). The gateway's own
# extras (list_accounts, search_messages, widgets) stay on /mcp and the bundle only.
GOOGLE_CALENDAR_TOOLS = [
    "get_event", "list_calendars", "list_events", "search_events", "suggest_time",
    "create_event", "delete_event", "respond_to_event", "update_event",
]  # fmt: skip
GOOGLE_DRIVE_TOOLS = [
    "download_file_content", "get_file_metadata", "get_file_permissions",
    "get_shared_drive_metadata", "list_file_revisions", "list_recent_files",
    "list_shared_drives", "read_file_content", "search_files",
    "copy_file", "create_file", "request_access", "share_file", "trash_file", "update_file",
]  # fmt: skip
# Drafts only: send_message, reply and forward are intentionally not offered.
GOOGLE_GMAIL_TOOLS = [
    "get_draft", "get_message", "get_thread", "list_drafts", "list_labels", "search_threads",
    "apply_sensitive_message_label", "apply_sensitive_thread_label", "create_draft",
    "create_label", "delete_draft", "delete_label", "label_message", "label_thread",
    "mark_message_spam", "mark_thread_spam", "trash_message", "trash_thread",
    "unlabel_message", "unlabel_thread", "unmark_message_spam", "unmark_thread_spam",
    "untrash_message", "untrash_thread", "update_draft", "update_label",
    "update_message_labels",
]  # fmt: skip


def _register_apex(registry: ToolRegistry, _settings: Settings) -> None:
    apex_read.register(registry)
    apex_write.register(registry)
    apex_expenses.register(registry)
    apex_auth.register(registry)
    apex_transactions.register(registry)


def _register_apex_wealth(registry: ToolRegistry, _settings: Settings) -> None:
    apex_wealth.register(registry)
    apex_auth.register_wealth(registry)


def _register_none(_registry: ToolRegistry, _settings: Settings) -> None:
    """For alias mounts: their tools are registered by the connector they point at."""


def _register_finance(registry: ToolRegistry, _settings: Settings) -> None:
    finance_read.register(registry)


def _register_recipes(registry: ToolRegistry, _settings: Settings) -> None:
    recipes_read.register(registry)
    recipes_write.register(registry)


def _register_telegram(registry: ToolRegistry, settings: Settings) -> None:
    telegram_read.register(registry)
    telegram_write.register(registry, settings)


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
        google_names=_google_names("google_calendar_", GOOGLE_CALENDAR_TOOLS),
    ),
    Connector(
        slug="drive",
        tool_prefix="google_drive_",
        display_name="Google Drive",
        upstream_provider="google",
        register=_register_drive,
        upstream=DRIVE_OAUTH,
        error_classifier=_google_classify_error,
        google_names=_google_names("google_drive_", GOOGLE_DRIVE_TOOLS),
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
        slug="gmail",
        tool_prefix="google_gmail_",
        display_name="Gmail",
        upstream_provider="google",
        register=_register_gmail,
        upstream=GMAIL_OAUTH,
        error_classifier=_google_classify_error,
        # download_attachment is the one override: Google's own Gmail MCP server calls it
        # download_gmail_attachment (added with the Google-compatible mounts, 57e8968), so it
        # is Google's name, not ours; it just differs from our internal name.
        google_names=_google_names(
            "google_gmail_", GOOGLE_GMAIL_TOOLS, download_attachment="download_gmail_attachment"
        ),
    ),
    Connector(
        slug="apex",
        tool_prefix="apex_",
        display_name="Apex Finance",
        upstream_provider="apex",
        register=_register_apex,
        upstream=APEX_UPSTREAM,
        exclude_prefixes=("apex_wealth_",),
    ),
    Connector(
        slug="apex-wealth",
        tool_prefix="apex_wealth_",
        display_name="Apex Invest",
        upstream_provider="apex_wealth",
        register=_register_apex_wealth,
        upstream=APEX_WEALTH_UPSTREAM,
    ),
    Connector(
        # Spending questions used to be their own mount. They are part of Apex Finance now
        # (`/mcp/apex`); this URL keeps serving the same tools for clients already using it.
        slug="apex-transactions",
        tool_prefix="apex_transactions_",
        display_name="Apex Finance (spending alias)",
        upstream_provider="apex",
        register=_register_none,
        hidden=True,
        alias_of="apex",
    ),
    Connector(
        slug="telegram",
        tool_prefix="telegram_",
        display_name="Telegram",
        upstream_provider="telegram",
        register=_register_telegram,
        upstream=TELEGRAM_API_KEY,
    ),
    Connector(
        slug="finance",
        tool_prefix="finance_",
        display_name="Market",
        upstream_provider="finance",
        register=_register_finance,
        access_mode=AccessMode.AUTHENTICATED_SHARED,
    ),
    Connector(
        slug="recipes",
        tool_prefix="recipes_",
        display_name="Recipes",
        upstream_provider="recipes",
        register=_register_recipes,
        error_classifier=classify_storage_error,
    ),
]


def by_slug(slug: str) -> Connector | None:
    """Return the connector mounted at ``/mcp/<slug>``, or ``None``."""
    return next((c for c in CONNECTORS if c.slug == slug), None)


def by_spec(spec: ToolSpec) -> Connector | None:
    """Return the connector that owns a tool spec, by name-prefix match, or ``None``."""
    return next((c for c in CONNECTORS if not c.hidden and c.matches(spec)), None)


# Overarching endpoints that combine the tools of several connectors. The per-connector
# endpoints stay as they are; these are optional conveniences.
BUNDLES: list[Bundle] = [
    Bundle(slug="google", display_name="Google Workspace", upstream_provider="google"),
]

if {b.slug for b in BUNDLES} & {c.slug for c in CONNECTORS}:  # pragma: no cover
    raise ValueError("a bundle slug collides with a connector slug")


def bundle_by_slug(slug: str) -> Bundle | None:
    """Return the bundle mounted at ``/mcp/<slug>``, or ``None``."""
    return next((b for b in BUNDLES if b.slug == slug), None)


def bundle_members(bundle: Bundle) -> list[Connector]:
    """The connectors a bundle serves, in registry order."""
    return [c for c in CONNECTORS if c.upstream_provider == bundle.upstream_provider]


def connector_slugs_for(slug: str) -> set[str] | None:
    """Connector slugs served at ``/mcp/<slug>``: itself, or a bundle's members.

    ``None`` means no endpoint exists at that slug.
    """
    connector = by_slug(slug)
    if connector is not None:
        return {connector.alias_of or slug}
    bundle = bundle_by_slug(slug)
    if bundle is not None:
        return {c.slug for c in bundle_members(bundle)}
    return None
