"""Shared helpers for Google Tasks tool modules."""

from __future__ import annotations

from sqlalchemy.orm import Session

from gateway.config import get_settings
from gateway.oauth.google import TASKS_SCOPES, build_start_url
from gateway.providers.base import CallContext
from gateway.providers.google.client import build_tasks_service
from gateway.providers.google.connections import get_active_connection

PROVIDER = "google"


def tasks_service(session: Session, ctx: CallContext):
    """Resolve the caller's active Google connection and build a Tasks service."""
    from gateway.providers.registry import ToolError

    settings = get_settings()
    conn = get_active_connection(session, ctx.user_id, PROVIDER)
    if conn is None:
        connect_url = build_start_url(settings, ctx.external_user_id, product="tasks")
        raise ToolError(
            "not_connected",
            f"no active Google connection; authorize here: {connect_url}",
        )
    missing_scopes = sorted(set(TASKS_SCOPES) - set(conn.scopes or []))
    if missing_scopes:
        connect_url = build_start_url(settings, ctx.external_user_id, product="tasks")
        raise ToolError(
            "reauth_required",
            (
                "Google Tasks connection is missing required scopes; reconnect here: "
                f"{connect_url}"
            ),
        )
    return build_tasks_service(session, conn, settings)


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
        "updated": task.get("updated"),
        "parent": task.get("parent"),
        "position": task.get("position"),
        "deleted": task.get("deleted", False),
        "hidden": task.get("hidden", False),
        "web_view_link": task.get("webViewLink"),
    }

