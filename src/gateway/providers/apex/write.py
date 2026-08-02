"""Apex mutating tools: create invoices and update timesheets."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from gateway.config import get_settings
from gateway.providers.apex.client import APEX_API_KEY, apex_request
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


class UpdateTimesheetInput(BaseModel):
    timesheet_id: str = Field(..., description="Timesheet UUID to update.")
    period_start: str | None = Field(None, description="New period start date, YYYY-MM-DD.")
    period_end: str | None = Field(None, description="New period end date, YYYY-MM-DD.")
    notes: str | None = Field(None, description="New notes.")
    entries: list[TimesheetEntryInput] | None = Field(
        None,
        min_length=1,
        description="Replacement work entries. When given, replaces all existing entries.",
    )


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
    api_key = APEX_API_KEY.get_credentials(session, ctx, settings)
    return apex_request(settings, api_key, "POST", "/api/invoices", json=body)


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
    api_key = APEX_API_KEY.get_credentials(session, ctx, settings)
    return apex_request(
        settings, api_key, "PUT", f"/api/timesheets/{args.timesheet_id}", json=body
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
            preview_builder=_create_invoice_preview,
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
            preview_builder=_update_timesheet_preview,
        )
    )
