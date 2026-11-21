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
    # Apex wealth tools are read-only; balances live in responses, never inputs.
    # Apex tools: the workspace id is safe to record; minted tokens, headers and the
    # Clerk subject are never inputs and are never recorded.
    "apex_list_workspaces": set(),
    "apex_wealth_list_workspaces": set(),
    "apex_create_invoice": {"workspace_id"},
    "apex_create_timesheet": {"workspace_id"},
    "apex_mark_invoice_paid": {"workspace_id", "invoice_id", "paid_date"},
    "apex_update_timesheet": {"workspace_id"},
    "apex_list_clients": {"workspace_id"},
    "apex_list_timesheets": {"workspace_id"},
    "apex_get_last_timesheet": {"workspace_id"},
    "apex_wealth_list_accounts": {"workspace_id"},
    "apex_wealth_get_accounts_summary": {"workspace_id"},
    "apex_wealth_get_investments_summary": {"workspace_id"},
    "apex_wealth_get_positions": {"workspace_id", "account_id", "as_of_date"},
    "apex_wealth_list_manual_holdings": {"workspace_id", "account_id"},
    "apex_wealth_get_net_worth_history": {"workspace_id", "months"},
    "google_calendar_list_calendars": {"account"},
    "google_calendar_list_accounts": set(),
    "google_drive_list_accounts": set(),
    "google_tasks_list_accounts": set(),
    "google_gmail_list_accounts": set(),
    "google_calendar_list_events": {
        "calendar_id",
        "max_results",
        "order_by",
        "single_events",
        "account",
    },
    "google_calendar_get_event": {"calendar_id", "event_id", "account"},
    # Mutating calendar tools: record identifiers/times, never summary/description.
    "google_calendar_create_event": {"calendar_id", "start", "end", "time_zone", "account"},
    "google_calendar_update_event": {
        "calendar_id",
        "event_id",
        "start",
        "end",
        "time_zone",
        "account",
    },
    "google_calendar_delete_event": {"calendar_id", "event_id", "account"},
    "google_drive_search_files": {"page_size", "account"},
    "google_drive_get_file_metadata": {"file_id", "account"},
    "google_drive_api_search_files": set(),
    "google_drive_api_download_file": {"file_id"},
    "google_tasks_list_tasklists": {"max_results", "account"},
    "google_tasks_list_tasks": {
        "tasklist_id",
        "max_results",
        "show_assigned",
        "show_completed",
        "show_deleted",
        "show_hidden",
        "account",
    },
    # Mutating task tools: record identifiers/dates, never title/notes.
    "google_tasks_create_task": {"tasklist_id", "due", "parent", "previous", "account"},
    "google_tasks_update_task": {"tasklist_id", "task_id", "due", "status", "account"},
    "google_tasks_complete_task": {"tasklist_id", "task_id", "account"},
    "google_tasks_delete_task": {"tasklist_id", "task_id", "account"},
    # Gmail: ids/paging only — never queries, addresses, subjects or bodies.
    "google_gmail_search_messages": {"max_results", "account"},
    "google_gmail_get_message": {"message_id", "account"},
    "google_gmail_get_thread": {"thread_id", "account"},
    "google_gmail_list_drafts": {"account"},
    "google_gmail_create_draft": {"reply_to_message_id", "account"},
    "google_gmail_update_draft": {"draft_id", "account"},
    "google_gmail_delete_draft": {"draft_id", "account"},
    "finance_get_current_rate": {"from_currency", "to"},
    "finance_get_rate_change": {"from_currency", "to", "window"},
    "finance_get_rate_history": {"from_currency", "to", "view", "limit"},
    "finance_list_currencies": set(),
    # Recipe tools: ids and paging only — never titles, ingredients,
    # instructions, notes, tags, or search text (query is recorded as <set>).
    "recipes_get": {"recipe_id"},
    "recipes_list": {"limit", "offset", "category"},
    "recipes_search": {"limit", "offset"},
    "recipes_save": {"language"},
    "recipes_update": {"recipe_id"},
    "recipes_delete": {"recipe_id"},
    # Feedback: id and rating only — never the comment.
    "recipes_add_feedback": {"recipe_id", "rating"},
    "recipes_delete_feedback": {"recipe_id", "feedback_id"},
    "telegram_list_destinations": set(),
    "telegram_send_message": {"destination", "parse_mode", "disable_notification"},
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
