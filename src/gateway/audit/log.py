"""Audit logging for tool invocations.

Every tool call writes exactly one ``tool_audit_log`` row (success or failure)
via the registry wrapper. Input summaries are built from a per-tool whitelist so
that secrets, file contents, free-text queries, and full event descriptions are
never persisted.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from gateway.db.models import ToolAuditLog

# Fields safe to record per tool. Anything not listed is dropped from the summary.
_SAFE_FIELDS: dict[str, set[str]] = {
    "system_get_current_time": {"time_zone"},
    "google_calendar_list_calendars": set(),
    "google_calendar_list_events": {"calendar_id", "max_results", "order_by", "single_events"},
    "google_calendar_get_event": {"calendar_id", "event_id"},
    # Mutating calendar tools: record identifiers/times, never summary/description.
    "google_calendar_create_event": {"calendar_id", "start", "end", "time_zone"},
    "google_calendar_update_event": {"calendar_id", "event_id", "start", "end", "time_zone"},
    "google_calendar_delete_event": {"calendar_id", "event_id"},
    "google_tasks_list_tasklists": {"max_results"},
    "google_tasks_list_tasks": {
        "tasklist_id",
        "max_results",
        "show_assigned",
        "show_completed",
        "show_deleted",
        "show_hidden",
    },
    # Mutating task tools: record identifiers/dates, never title/notes.
    "google_tasks_create_task": {"tasklist_id", "due", "parent", "previous"},
    "google_tasks_update_task": {"tasklist_id", "task_id", "due", "status"},
    "google_tasks_complete_task": {"tasklist_id", "task_id"},
    "google_tasks_delete_task": {"tasklist_id", "task_id"},
}


def summarize_input(tool_name: str, args: dict) -> str:
    """Build a redacted, human-readable summary of tool input.

    Only whitelisted scalar fields are included. For time-range style inputs we
    record presence, not the values.
    """
    allowed = _SAFE_FIELDS.get(tool_name, set())
    parts: list[str] = []
    for key in sorted(allowed):
        if key in args and args[key] is not None:
            parts.append(f"{key}={args[key]!r}")

    # Record presence (not values) of time-range / query inputs.
    for key in ("time_min", "time_max", "query", "due_min", "due_max", "updated_min"):
        if args.get(key):
            parts.append(f"{key}=<set>")

    return ", ".join(parts)


def write_audit(
    session: Session,
    *,
    user_id: uuid.UUID | None,
    provider: str,
    tool_name: str,
    request_id: str | None,
    input_summary: str,
    result_status: str,
    error_code: str | None = None,
    auth_source: str | None = None,
) -> None:
    """Persist one audit row."""
    session.add(
        ToolAuditLog(
            user_id=user_id,
            provider=provider,
            tool_name=tool_name,
            auth_source=auth_source,
            request_id=request_id,
            input_summary=input_summary,
            result_status=result_status,
            error_code=error_code,
        )
    )
