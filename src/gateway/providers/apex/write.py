"""Apex mutating tools: create invoices, send / mark them paid, and update timesheets."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from gateway.config import get_settings
from gateway.providers.apex.client import (
    SCOPE_INVOICING_WRITE,
    WORKSPACE_ID_DESC,
    apex_call,
    apex_credentials,
)
from gateway.providers.base import CallContext, RiskLevel, ToolSpec

PROVIDER = "apex"

InvoiceStatus = Literal["DRAFT", "SENT", "PAID", "OVERDUE", "VOID"]


class LineItemInput(BaseModel):
    description: str = Field(..., description="Line item description.")
    quantity: float = Field(..., ge=0, description="Quantity.")
    rate: float = Field(..., ge=0, description="Rate per unit.")


class TimesheetEntryInput(BaseModel):
    work_date: str = Field(..., description="Date worked, YYYY-MM-DD.")
    description: str = Field(..., description="What was worked on.")
    hours: float = Field(..., ge=0, description="Hours worked.")


class InlineTimesheetInput(BaseModel):
    period_start: str = Field(..., description="Timesheet period start date, YYYY-MM-DD.")
    period_end: str = Field(..., description="Timesheet period end date, YYYY-MM-DD.")
    notes: str | None = Field(None, description="Optional timesheet notes.")
    entries: list[TimesheetEntryInput] = Field(
        ..., min_length=1, description="Work entries for the period."
    )


class CreateInvoiceInput(BaseModel):
    workspace_id: str = Field(..., description=WORKSPACE_ID_DESC)
    client_id: str = Field(..., description="Client UUID to invoice.")
    issue_date: str = Field(..., description="Invoice issue date, YYYY-MM-DD.")
    due_date: str | None = Field(
        None, description="Due date, YYYY-MM-DD. Defaults from business payment terms."
    )
    gst_percent: float | None = Field(
        None,
        ge=0,
        description="Override GST/HST percent. Defaults from the client's chargeHst (13 or 0).",
    )
    notes: str | None = Field(None, description="Invoice notes.")
    status: InvoiceStatus | None = Field(None, description="Invoice status. Defaults to DRAFT.")
    line_items: list[LineItemInput] | None = Field(
        None,
        min_length=1,
        description=(
            "Direct line items, for DIRECT-billing clients. Mutually exclusive with "
            "timesheet — provide exactly one."
        ),
    )
    timesheet: InlineTimesheetInput | None = Field(
        None,
        description=(
            "Inline timesheet (period + hour entries), for TIMESHEET-billing clients. "
            "The server sums hours into one rollup line and sets HST from the client's "
            "chargeHst unless gst_percent is given. Mutually exclusive with line_items — "
            "provide exactly one. A standalone timesheet cannot be created; it is always "
            "created together with the invoice."
        ),
    )

    @model_validator(mode="after")
    def _exactly_one_billing_payload(self) -> CreateInvoiceInput:
        if bool(self.line_items) == bool(self.timesheet):
            raise ValueError("provide exactly one of line_items or timesheet")
        return self


class CreateTimesheetInput(BaseModel):
    workspace_id: str = Field(..., description=WORKSPACE_ID_DESC)
    client_id: str = Field(
        ..., description="Client UUID to invoice. Must be a TIMESHEET-billing client."
    )
    issue_date: str = Field(..., description="Invoice issue date, YYYY-MM-DD.")
    due_date: str | None = Field(
        None, description="Due date, YYYY-MM-DD. Defaults from business payment terms."
    )
    gst_percent: float | None = Field(
        None,
        ge=0,
        description="Override GST/HST percent. Defaults from the client's chargeHst (13 or 0).",
    )
    status: InvoiceStatus | None = Field(None, description="Invoice status. Defaults to DRAFT.")
    period_start: str = Field(..., description="Timesheet period start date, YYYY-MM-DD.")
    period_end: str = Field(..., description="Timesheet period end date, YYYY-MM-DD.")
    notes: str | None = Field(None, description="Optional timesheet notes.")
    entries: list[TimesheetEntryInput] = Field(
        ..., min_length=1, description="Work entries for the period."
    )


class UpdateTimesheetInput(BaseModel):
    workspace_id: str = Field(..., description=WORKSPACE_ID_DESC)
    timesheet_id: str = Field(..., description="Timesheet UUID to update.")
    period_start: str | None = Field(None, description="New period start date, YYYY-MM-DD.")
    period_end: str | None = Field(None, description="New period end date, YYYY-MM-DD.")
    notes: str | None = Field(None, description="New notes.")
    entries: list[TimesheetEntryInput] | None = Field(
        None,
        min_length=1,
        description="Replacement work entries. When given, replaces all existing entries.",
    )


class UpdateInvoiceStatusInput(BaseModel):
    workspace_id: str = Field(..., description=WORKSPACE_ID_DESC)
    invoice_id: str = Field(..., description="Invoice UUID whose status to change.")
    status: Literal["SENT", "PAID"] = Field(
        ...,
        description=(
            "Target status. SENT emails the invoice to the client; PAID records a payment."
        ),
    )
    paid_date: str | None = Field(
        None,
        description="PAID only: date the payment was received, YYYY-MM-DD. Defaults to today.",
    )
    paid_amount: float | None = Field(
        None, ge=0, description="PAID only: amount received. Defaults to the invoice total."
    )

    @model_validator(mode="after")
    def _payment_fields_only_when_paid(self) -> UpdateInvoiceStatusInput:
        if self.status != "PAID" and (self.paid_date is not None or self.paid_amount is not None):
            raise ValueError("paid_date and paid_amount can only be set when status is PAID")
        return self


def create_invoice(args: CreateInvoiceInput, ctx: CallContext, session: Session) -> Any:
    body: dict[str, Any] = {"clientId": args.client_id, "issueDate": args.issue_date}
    if args.due_date is not None:
        body["dueDate"] = args.due_date
    if args.gst_percent is not None:
        body["gstPercent"] = args.gst_percent
    if args.notes is not None:
        body["notes"] = args.notes
    if args.status is not None:
        body["status"] = args.status
    if args.line_items is not None:
        body["lineItems"] = [
            {"description": item.description, "quantity": item.quantity, "rate": item.rate}
            for item in args.line_items
        ]
    if args.timesheet is not None:
        body["timesheet"] = {
            "periodStart": args.timesheet.period_start,
            "periodEnd": args.timesheet.period_end,
            "notes": args.timesheet.notes,
            "entries": [
                {
                    "workDate": entry.work_date,
                    "description": entry.description,
                    "hours": entry.hours,
                }
                for entry in args.timesheet.entries
            ],
        }
    settings = get_settings()
    creds = apex_credentials(
        settings,
        ctx,
        scopes=[SCOPE_INVOICING_WRITE],
        workspace_id=args.workspace_id,
        capability="invoicing",
    )
    return apex_call(settings, creds, "POST", "/api/invoices", json=body)


def create_timesheet(args: CreateTimesheetInput, ctx: CallContext, session: Session) -> Any:
    body: dict[str, Any] = {
        "clientId": args.client_id,
        "issueDate": args.issue_date,
        "timesheet": {
            "periodStart": args.period_start,
            "periodEnd": args.period_end,
            "notes": args.notes,
            "entries": [
                {
                    "workDate": entry.work_date,
                    "description": entry.description,
                    "hours": entry.hours,
                }
                for entry in args.entries
            ],
        },
    }
    if args.due_date is not None:
        body["dueDate"] = args.due_date
    if args.gst_percent is not None:
        body["gstPercent"] = args.gst_percent
    if args.status is not None:
        body["status"] = args.status
    settings = get_settings()
    creds = apex_credentials(
        settings,
        ctx,
        scopes=[SCOPE_INVOICING_WRITE],
        workspace_id=args.workspace_id,
        capability="invoicing",
    )
    return apex_call(settings, creds, "POST", "/api/invoices", json=body)


def update_invoice_status(
    args: UpdateInvoiceStatusInput, ctx: CallContext, session: Session
) -> Any:
    body: dict[str, Any] = {"status": args.status}
    if args.paid_date is not None:
        body["paidDate"] = args.paid_date
    if args.paid_amount is not None:
        body["paidAmount"] = args.paid_amount
    settings = get_settings()
    creds = apex_credentials(
        settings,
        ctx,
        scopes=[SCOPE_INVOICING_WRITE],
        workspace_id=args.workspace_id,
        capability="invoicing",
    )
    return apex_call(
        settings, creds, "PATCH", f"/api/invoices/{quote(args.invoice_id, safe='')}", json=body
    )


def update_timesheet(args: UpdateTimesheetInput, ctx: CallContext, session: Session) -> Any:
    body: dict[str, Any] = {}
    if args.period_start is not None:
        body["periodStart"] = args.period_start
    if args.period_end is not None:
        body["periodEnd"] = args.period_end
    if args.notes is not None:
        body["notes"] = args.notes
    if args.entries is not None:
        body["entries"] = [
            {"workDate": entry.work_date, "description": entry.description, "hours": entry.hours}
            for entry in args.entries
        ]
    settings = get_settings()
    creds = apex_credentials(
        settings,
        ctx,
        scopes=[SCOPE_INVOICING_WRITE],
        workspace_id=args.workspace_id,
        capability="invoicing",
    )
    return apex_call(
        settings, creds, "PUT", f"/api/timesheets/{args.timesheet_id}", json=body
    )


def _create_invoice_preview(args: CreateInvoiceInput) -> str:
    if args.timesheet is not None:
        hours = sum(entry.hours for entry in args.timesheet.entries)
        return (
            f"Create invoice for client {args.client_id}, issued {args.issue_date}, "
            f"from a timesheet ({args.timesheet.period_start} to {args.timesheet.period_end}, "
            f"{hours} hours)."
        )
    count = len(args.line_items or [])
    return (
        f"Create invoice for client {args.client_id}, issued {args.issue_date}, "
        f"with {count} line item(s)."
    )


def _create_timesheet_preview(args: CreateTimesheetInput) -> str:
    hours = sum(entry.hours for entry in args.entries)
    return (
        f"File timesheet for client {args.client_id}, {args.period_start} to "
        f"{args.period_end} ({hours} hours, {len(args.entries)} entries). Apex will "
        f"create a linked DRAFT invoice from it."
    )


def _invoice_status_preview(args: UpdateInvoiceStatusInput) -> str:
    if args.status == "SENT":
        return (
            f"SEND invoice '{args.invoice_id}' by email to the client now (PDF attached). "
            f"The issue date is reset to today and the due date to today plus the "
            f"client's payment terms."
        )
    amount = f"{args.paid_amount:g}" if args.paid_amount is not None else "the full invoice total"
    when = args.paid_date or "today"
    return f"Mark invoice '{args.invoice_id}' as PAID: {amount} received on {when}."


def _update_timesheet_preview(args: UpdateTimesheetInput) -> str:
    return f"Update timesheet '{args.timesheet_id}'."


def register(registry) -> None:
    """Register Apex mutating tools with the tool registry."""
    registry.add(
        ToolSpec(
            name="apex_create_invoice",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            description=(
                "Create an invoice in Apex. Requires confirmation. Provide exactly one of "
                "line_items (for DIRECT-billing clients) or timesheet (inline period + hour "
                "entries, for TIMESHEET-billing clients — the server rolls the hours into "
                "one invoice line and sets HST from the client's chargeHst unless "
                "gst_percent is given). Use apex_list_clients to find the client's id and "
                "billing mode, and apex_get_last_timesheet to find the next period's start "
                "date."
            ),
            input_model=CreateInvoiceInput,
            handler=create_invoice,
            required_capability="invoicing",
            preview_builder=_create_invoice_preview,
            ui_app_callable=True,
        )
    )
    registry.add(
        ToolSpec(
            name="apex_create_timesheet",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            description=(
                "File a timesheet (period + hour entries) for a TIMESHEET-billing client. "
                "Apex creates a linked DRAFT invoice from it automatically — standalone "
                "timesheets don't exist, filing one always produces an invoice. Use "
                "apex_list_clients first to confirm the client's billing mode, and "
                "apex_get_last_timesheet to find the next period's start date. Requires "
                "confirmation."
            ),
            input_model=CreateTimesheetInput,
            handler=create_timesheet,
            required_capability="invoicing",
            preview_builder=_create_timesheet_preview,
            ui_app_callable=True,
        )
    )
    registry.add(
        ToolSpec(
            name="apex_update_invoice_status",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            description=(
                "Change an Apex invoice's status. Invoices move DRAFT -> SENT -> PAID. "
                "status=SENT EMAILS the invoice (PDF) to the client and resets its issue and "
                "due dates, so it is a real, outward-facing action: ask the user first. "
                "status=PAID records the payment (paid_date defaults to today, paid_amount to "
                "the invoice total); a paid invoice can no longer be edited or deleted. "
                "Void invoices can be neither sent nor marked paid. Use apex_list_timesheets "
                "or the invoice id from apex_create_invoice to find the invoice."
            ),
            input_model=UpdateInvoiceStatusInput,
            handler=update_invoice_status,
            required_capability="invoicing",
            preview_builder=_invoice_status_preview,
        )
    )
    registry.add(
        ToolSpec(
            name="apex_update_timesheet",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            description=(
                "Update an existing Apex timesheet's period, notes, and/or entries "
                "(entries, if given, replace all existing entries). Requires confirmation. "
                "Does not recalculate the parent invoice's totals."
            ),
            input_model=UpdateTimesheetInput,
            handler=update_timesheet,
            required_capability="invoicing",
            preview_builder=_update_timesheet_preview,
            ui_app_callable=True,
        )
    )
