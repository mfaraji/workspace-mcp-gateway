"""Audit logging for tool invocations.

Every tool call writes exactly one audit document (success or failure) to Capella
via the registry wrapper. Each document carries an expiry (``AUDIT_RETENTION_DAYS``,
30 by default), so retention is enforced by the database rather than a cleanup job.
Input summaries are built from a per-tool whitelist so that secrets, file contents,
free-text queries, and full event descriptions are never persisted.

Writes are best-effort and happen on a background thread: an audit failure (or an
unconfigured/unreachable Capella) must never fail or slow down the tool call itself.
"""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import timedelta

from gateway.config import get_settings
from gateway.storage.capella.client import get_client
from gateway.storage.capella.models import utc_now_iso
from gateway.storage.capella.repository import OwnedDocumentRepository

log = logging.getLogger(__name__)

DOC_TYPE = "audit"
SCHEMA_VERSION = 1

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
    "apex_update_invoice_status": {"workspace_id", "invoice_id", "status", "paid_date"},
    "apex_update_timesheet": {"workspace_id"},
    "apex_list_clients": {"workspace_id"},
    # Expenses: ids/enums only — never merchant, amount, notes or file contents.
    "apex_list_expense_categories": {"kind"},
    "apex_create_expense": {"category_id"},
    "apex_upload_expense_receipt": {"expense_id"},
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
    "google_gmail_download_attachment": {"message_id", "attachment_id", "account"},
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


@dataclass
class AuditRecord:
    """One tool call's outcome. Holds only redacted, non-secret fields."""

    user_id: uuid.UUID
    provider: str
    tool_name: str
    result_status: str  # ok | needs_confirmation | error
    request_id: str | None = None
    input_summary: str = ""
    error_code: str | None = None
    auth_source: str | None = None
    # Effective tier for mutating tools ("low" / "medium"); None for read tools.
    risk_tier: str | None = None
    duration_ms: int | None = None
    oauth_client_id: str | None = None
    chat_id: str | None = None


_executor: ThreadPoolExecutor | None = None
_warned_unconfigured = False


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="audit")
    return _executor


def build_document(record: AuditRecord, doc_id: uuid.UUID) -> dict:
    """The stored document: owner-scoped, versioned, no secrets."""
    body = {k: v for k, v in asdict(record).items() if k != "user_id" and v is not None}
    return {
        "id": str(doc_id),
        "owner_user_id": str(record.user_id),
        "schema_version": SCHEMA_VERSION,
        "type": DOC_TYPE,
        "created_at": utc_now_iso(),
        **body,
    }


def _write(record: AuditRecord) -> None:
    settings = get_settings()
    repo = OwnedDocumentRepository(
        get_client(settings),
        scope=settings.capella_audit_scope,
        collection=settings.capella_audit_collection,
        doc_type=DOC_TYPE,
    )
    doc_id = uuid.uuid4()
    repo.insert(
        record.user_id,
        doc_id,
        build_document(record, doc_id),
        expiry=timedelta(days=settings.audit_retention_days),
    )


def _safe_write(record: AuditRecord) -> None:
    try:
        _write(record)
    except Exception as exc:  # audit must never raise into a tool call
        log.warning("audit write failed (%s)", type(exc).__name__)


def record_audit(record: AuditRecord) -> None:
    """Queue one audit document for Capella; never raises, never blocks the caller."""
    global _warned_unconfigured
    if not get_settings().capella_configured:
        if not _warned_unconfigured:
            _warned_unconfigured = True
            log.warning("Capella is not configured: tool-call audit is disabled")
        return
    try:
        _get_executor().submit(_safe_write, record)
    except RuntimeError:  # executor already shut down
        pass


def flush_audit() -> None:
    """Wait for queued audit writes (shutdown and tests)."""
    if _executor is not None:
        _executor.submit(lambda: None).result()


def shutdown_audit() -> None:
    """Drain queued writes and stop the worker; call before closing the Capella client."""
    global _executor
    executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=True)
