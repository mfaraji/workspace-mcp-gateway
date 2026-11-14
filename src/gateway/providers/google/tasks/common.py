"""Shared helpers for Google Tasks tool modules."""

from __future__ import annotations

from sqlalchemy.orm import Session

from gateway.config import get_settings
from gateway.connectors.upstream import OAuthStrategy
from gateway.oauth.google import TASKS_SCOPES
from gateway.providers.base import CallContext
from gateway.providers.google.client import build_tasks_service

PROVIDER = "google"

TASKS_OAUTH = OAuthStrategy(
    upstream_provider=PROVIDER,
    required_scopes=TASKS_SCOPES,
    display_name="Google Tasks",
    product="tasks",
)


TASKS_UI = "ui://tasks/list"
UI_NOTE = (
    "Shows an interactive checklist in the chat (tick to complete, add, edit, delete): "
    "do not repeat the tasks in text; summarise or answer follow-ups instead."
)


def tasks_service(session: Session, ctx: CallContext):
    """Resolve the caller's active Google connection and build a Tasks service."""
    creds = TASKS_OAUTH.get_credentials(session, ctx, get_settings())
    return build_tasks_service(creds)


def trim_tasklist(tasklist: dict) -> dict:
    """Trim a Google task list resource to a compact, model-friendly shape."""
    return {
        "id": tasklist.get("id"),
        "title": tasklist.get("title"),
        "updated": tasklist.get("updated"),
        "self_link": tasklist.get("selfLink"),
    }


def trim_task(task: dict) -> dict:
    """Trim a Google task resource to a compact, model-friendly shape."""
    return {
        "id": task.get("id"),
        "title": task.get("title"),
        "notes": task.get("notes"),
        "status": task.get("status"),
        "due": task.get("due"),
        "completed": task.get("completed"),
        # Write tools overwrite ``status`` with the action taken ("created"/"completed"),
        # so widgets read this flag instead.
        "is_completed": task.get("status") == "completed",
        "updated": task.get("updated"),
        "parent": task.get("parent"),
        "position": task.get("position"),
        "deleted": task.get("deleted", False),
        "hidden": task.get("hidden", False),
        "web_view_link": task.get("webViewLink"),
    }
