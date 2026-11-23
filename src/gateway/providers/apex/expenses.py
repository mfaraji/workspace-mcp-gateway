"""Apex expense tools: categories, agent-created expenses and receipt upload.

Expenses created here land in the user's Apex review inbox, which belongs to no
workspace; the user picks the workspace when reviewing. Credentials are user-level
(no ``wid``); see :func:`gateway.providers.apex.client.apex_user_credentials`.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from gateway.config import get_settings
from gateway.providers.apex.client import (
    PROVIDER,
    SCOPE_EXPENSES_READ,
    SCOPE_EXPENSES_WRITE,
    apex_call,
    apex_credentials,
    apex_upload,
    apex_user_credentials,
)
from gateway.providers.apex.tickets import TICKET_TTL_SECONDS, mint_ticket
from gateway.providers.base import CallContext, RiskLevel, ToolSpec
from gateway.providers.registry import ToolError
from gateway.storage.staged_files import take

_INBOX_NOTE = (
    " The expense is added to the user's Apex review inbox (it belongs to no workspace); "
    "the user chooses the workspace when reviewing it in Apex."
)


class ListExpenseCategoriesInput(BaseModel):
    kind: Literal["all", "business", "rental"] = Field(
        "all", description="all categories, tax-deductible business ones, or the rental tree."
    )


class CreateExpenseInput(BaseModel):
    merchant: str | None = Field(None, min_length=1, max_length=200)
    amount: float | None = Field(None, gt=0, description="Total amount paid, positive.")
    date: str | None = Field(
        None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Purchase date, YYYY-MM-DD."
    )
    currency: str | None = Field(
        None, min_length=3, max_length=3, description="ISO code, e.g. CAD."
    )
    category_id: str | None = Field(None, description="From apex_list_expense_categories.")
    description: str | None = Field(None, max_length=500)
    notes: str | None = Field(None, max_length=2000)
    is_business: bool | None = Field(None, description="Hint that this is a business expense.")
    workspace_id: str | None = Field(
        None,
        description=(
            "Apex workspace this expense is for (from apex_list_workspaces; needs receipt "
            "review, i.e. a business or rental workspace). With it the attached receipt is "
            "scanned and categorized against that workspace's categories and the workspace "
            "is pre-selected at review. Without it the scan still fills merchant, amount and "
            "date but leaves the category empty."
        ),
    )
    receipt_file_ref: str | None = Field(
        None, description="file_ref from google_gmail_download_attachment to attach as receipt."
    )

    @model_validator(mode="after")
    def _details_or_receipt(self):
        missing = [n for n in ("merchant", "amount", "date") if getattr(self, n) is None]
        if missing and not self.receipt_file_ref:
            raise ValueError(
                f"{', '.join(missing)} required unless receipt_file_ref is given "
                "(Apex scans the receipt to fill them in)"
            )
        return self


class UploadExpenseReceiptInput(BaseModel):
    expense_id: str = Field(
        ..., min_length=1, max_length=128, description="From apex_create_expense."
    )
    file_ref: str | None = Field(
        None,
        description=(
            "file_ref from google_gmail_download_attachment. Omit it when the file is on your "
            "own machine: you then get an upload_url to POST the file to."
        ),
    )


def list_categories(args: ListExpenseCategoriesInput, ctx: CallContext, session: Session) -> Any:
    settings = get_settings()
    creds = apex_user_credentials(settings, ctx, scopes=[SCOPE_EXPENSES_READ])
    return apex_call(settings, creds, "GET", "/api/expense-categories", params={"kind": args.kind})


def _upload_to_expense(settings, creds, expense_id: str, filename: str, mime: str, data: bytes):
    return apex_upload(
        settings,
        creds,
        f"/api/expense-inbox/{quote(expense_id, safe='')}/receipt",
        filename=filename,
        mime_type=mime,
        data=data,
    )


def create_expense(args: CreateExpenseInput, ctx: CallContext, session: Session) -> Any:
    settings = get_settings()
    staged = take(session, ctx.user_id, args.receipt_file_ref) if args.receipt_file_ref else None
    if args.workspace_id:
        # Validates the workspace belongs to the user and supports receipt review.
        creds = apex_credentials(
            settings,
            ctx,
            scopes=[SCOPE_EXPENSES_WRITE],
            workspace_id=args.workspace_id,
            capability="receiptReview",
        )
    else:
        creds = apex_user_credentials(settings, ctx, scopes=[SCOPE_EXPENSES_WRITE])
    # Stable key so an agent retry returns the same inbox item instead of a duplicate.
    sha = staged.sha256 if staged else ""
    ref_src = f"{ctx.user_id}|{args.merchant}|{args.amount}|{args.date}|{args.workspace_id}|{sha}"
    body: dict[str, Any] = {"externalRef": hashlib.sha256(ref_src.encode()).hexdigest()}
    for key, value in (
        ("merchant", args.merchant),
        ("amount", args.amount),
        ("date", args.date),
        ("currency", args.currency),
        ("categoryId", args.category_id),
        ("description", args.description),
        ("notes", args.notes),
        ("isBusiness", args.is_business),
        ("workspaceId", args.workspace_id),
    ):
        if value is not None:
            body[key] = value
    expense = apex_call(settings, creds, "POST", "/api/expense-inbox", json=body)
    if staged is None or not isinstance(expense, dict) or not expense.get("id"):
        return expense
    try:
        _upload_to_expense(
            settings, creds, str(expense["id"]), staged.filename, staged.mime_type, staged.data
        )
        return {**expense, "receipt_uploaded": True}
    except ToolError as exc:
        return {
            **expense,
            "receipt_uploaded": False,
            "receipt_error": exc.error_code,
            "hint": (
                "Expense created; re-download the attachment and call apex_upload_expense_receipt."
            ),
        }


def upload_receipt(args: UploadExpenseReceiptInput, ctx: CallContext, session: Session) -> Any:
    settings = get_settings()
    creds = apex_user_credentials(settings, ctx, scopes=[SCOPE_EXPENSES_WRITE])
    if args.file_ref:
        staged = take(session, ctx.user_id, args.file_ref)
        return _upload_to_expense(
            settings, creds, args.expense_id, staged.filename, staged.mime_type, staged.data
        )
    if not ctx.clerk_subject:
        raise ToolError("not_connected", "Apex needs a signed-in Clerk user.")
    ticket = mint_ticket(
        settings,
        user_id=ctx.user_id,
        clerk_subject=ctx.clerk_subject,
        oauth_client_id=ctx.oauth_client_id,
        expense_id=args.expense_id,
    )
    url = f"{settings.base_url.rstrip('/')}/uploads/apex-receipt"
    return {
        "status": "upload_required",
        "upload_url": url,
        "expires_in_seconds": TICKET_TTL_SECONDS,
        "instructions": (
            "POST the receipt file (PDF, JPEG, PNG, WEBP or HEIC, max 10 MB) once, as "
            "multipart field 'file', with the ticket as a bearer token. Do not log the ticket."
        ),
        "curl": f'curl -sS -H "Authorization: Bearer {ticket}" -F file=@<path> {url}',
    }


def _create_preview(args: CreateExpenseInput) -> str:
    what = f"'{args.merchant}' {args.amount:g} on {args.date}" if args.amount else "from a receipt"
    return (
        f"Add expense {what} to your Apex review inbox (you choose the workspace when reviewing)."
    )


def _upload_preview(args: UploadExpenseReceiptInput) -> str:
    return f"Attach a receipt file to Apex expense '{args.expense_id}'."


def register(registry) -> None:
    registry.add(
        ToolSpec(
            name="apex_list_expense_categories",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description="List the user's Apex expense categories (id, name, parent)." + _INBOX_NOTE,
            input_model=ListExpenseCategoriesInput,
            handler=list_categories,
            required_capability="receiptReview",
        )
    )
    registry.add(
        ToolSpec(
            name="apex_create_expense",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            description=(
                "Create an expense (merchant, amount, date, optional category) from e.g. an "
                "emailed receipt. Pass receipt_file_ref from google_gmail_download_attachment "
                "to attach the receipt in the same call; Apex scans the receipt with AI, so "
                "merchant/amount/date may then be omitted and are filled from it (values you "
                "give win). Pass workspace_id (from apex_list_workspaces) to have the scan "
                "also pick a category from that workspace; without it the category stays "
                "empty and is chosen at review." + _INBOX_NOTE
            ),
            input_model=CreateExpenseInput,
            handler=create_expense,
            required_capability="receiptReview",
            preview_builder=_create_preview,
            requires_confirmation=False,
        )
    )
    registry.add(
        ToolSpec(
            name="apex_upload_expense_receipt",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            description=(
                "Attach a receipt file (PDF/image, max 10 MB) to an expense created with "
                "apex_create_expense; Apex scans it with AI to fill blank details. With file_ref "
                "the gateway uploads it directly; without it you get a one-time upload_url and "
                "curl command for a file on your machine."
            ),
            input_model=UploadExpenseReceiptInput,
            handler=upload_receipt,
            required_capability="receiptReview",
            preview_builder=_upload_preview,
            requires_confirmation=False,
        )
    )
