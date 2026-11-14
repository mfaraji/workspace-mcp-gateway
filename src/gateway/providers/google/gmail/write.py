"""Gmail draft tools. Drafts only: nothing here can send mail.

The gateway never calls ``drafts.send`` or ``messages.send``. Update/delete are
limited to drafts recorded in ``gmail_agent_drafts`` (created via this gateway).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from gateway.db.models import GmailAgentDraft
from gateway.providers.base import CallContext, RiskLevel, ToolSpec
from gateway.providers.google.connections import get_active_connection
from gateway.providers.google.gmail.common import (
    MAX_BODY_CHARS,
    MAX_RECIPIENTS,
    PROVIDER,
    build_mime,
    gmail_service,
    parse_message,
    require_labeled,
    resolve_access_label_id,
    valid_address,
)
from gateway.providers.registry import ToolError


def _check_addresses(values: list[str]) -> list[str]:
    if len(values) > MAX_RECIPIENTS:
        raise ValueError(f"at most {MAX_RECIPIENTS} recipients per field")
    return [valid_address(v) for v in values]


class CreateDraftInput(BaseModel):
    to: list[str] = Field(default_factory=list, description="Recipients (optional for replies).")
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)
    subject: str = Field("", max_length=998)
    body_text: str | None = Field(None, max_length=MAX_BODY_CHARS)
    body_html: str | None = Field(None, max_length=MAX_BODY_CHARS, description="HTML body.")
    reply_to_message_id: str | None = Field(
        None, description="Labelled message id to reply to (threads the draft)."
    )

    _v = field_validator("to", "cc", "bcc")(lambda cls, v: _check_addresses(v))


class UpdateDraftInput(BaseModel):
    draft_id: str = Field(..., min_length=1, max_length=255)
    to: list[str] | None = None
    cc: list[str] | None = None
    bcc: list[str] | None = None
    subject: str | None = Field(None, max_length=998)
    body_text: str | None = Field(None, max_length=MAX_BODY_CHARS)
    body_html: str | None = Field(None, max_length=MAX_BODY_CHARS)

    @field_validator("to", "cc", "bcc")
    @classmethod
    def _addr(cls, v):
        return None if v is None else _check_addresses(v)


class DeleteDraftInput(BaseModel):
    draft_id: str = Field(..., min_length=1, max_length=255)


def _owned(session: Session, ctx: CallContext, draft_id: str) -> GmailAgentDraft:
    row = session.scalars(
        select(GmailAgentDraft).where(
            GmailAgentDraft.user_id == ctx.user_id, GmailAgentDraft.draft_id == draft_id
        )
    ).first()
    if row is None:
        raise ToolError("not_found", "draft not found")
    return row


def _hdr(parsed: dict, name: str) -> str | None:
    return parsed["headers"].get(name)


def _split(value: str | None) -> list[str]:
    if not value:
        return []
    out = []
    for part in value.split(","):
        part = part.strip()
        if "<" in part and part.endswith(">"):
            part = part[part.rindex("<") + 1 : -1]
        if part:
            out.append(part)
    return out


def create_draft(args: CreateDraftInput, ctx: CallContext, session: Session) -> dict[str, Any]:
    if not (args.body_text or args.body_html):
        raise ToolError("invalid_input", "provide body_text and/or body_html")
    service = gmail_service(session, ctx)
    to, subject = list(args.to), args.subject
    thread_id = in_reply_to = references = None
    if args.reply_to_message_id:
        label_id = resolve_access_label_id(service)
        original = (
            service.users()
            .messages()
            .get(userId="me", id=args.reply_to_message_id, format="full")
            .execute()
        )
        require_labeled(original, label_id)
        parsed = parse_message(original)
        thread_id = original.get("threadId")
        in_reply_to = _hdr(parsed, "Message-ID")
        references = _hdr(parsed, "References")
        if not to:
            to = _split(_hdr(parsed, "From"))
        if not subject:
            orig = _hdr(parsed, "Subject") or ""
            subject = orig if orig.lower().startswith("re:") else f"Re: {orig}"
    raw = build_mime(
        to=to,
        cc=args.cc,
        bcc=args.bcc,
        subject=subject,
        body_text=args.body_text,
        body_html=args.body_html,
        in_reply_to=in_reply_to,
        references=references,
    )
    message: dict[str, Any] = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id
    draft = service.users().drafts().create(userId="me", body={"message": message}).execute()
    conn = get_active_connection(session, ctx.user_id, PROVIDER)
    msg = draft.get("message", {})
    session.add(
        GmailAgentDraft(
            user_id=ctx.user_id,
            google_account_id=conn.provider_account_id if conn else "",
            draft_id=draft["id"],
            thread_id=msg.get("threadId") or thread_id,
        )
    )
    return {
        "status": "draft_created",
        "draft_id": draft["id"],
        "message_id": msg.get("id"),
        "thread_id": msg.get("threadId") or thread_id,
    }


def update_draft(args: UpdateDraftInput, ctx: CallContext, session: Session) -> dict[str, Any]:
    row = _owned(session, ctx, args.draft_id)
    service = gmail_service(session, ctx)
    current = service.users().drafts().get(userId="me", id=row.draft_id, format="full").execute()
    cur_msg = current.get("message", {})
    parsed = parse_message(cur_msg)
    body_text = args.body_text if args.body_text is not None else parsed["body_text"]
    body_html = args.body_html if args.body_html is not None else parsed["body_html"]
    if args.body_html is not None and args.body_text is None:
        body_text = None  # regenerate the plain-text fallback from the new HTML
    if not (body_text or body_html):
        raise ToolError("invalid_input", "draft needs body_text and/or body_html")
    raw = build_mime(
        to=args.to if args.to is not None else _split(_hdr(parsed, "To")),
        cc=args.cc if args.cc is not None else _split(_hdr(parsed, "Cc")),
        bcc=args.bcc if args.bcc is not None else [],
        subject=args.subject if args.subject is not None else _hdr(parsed, "Subject") or "",
        body_text=body_text,
        body_html=body_html,
        in_reply_to=_in_reply_to(cur_msg),
        references=_hdr(parsed, "References"),
    )
    message: dict[str, Any] = {"raw": raw}
    if cur_msg.get("threadId"):
        message["threadId"] = cur_msg["threadId"]
    updated = (
        service.users()
        .drafts()
        .update(userId="me", id=row.draft_id, body={"id": row.draft_id, "message": message})
        .execute()
    )
    return {"status": "draft_updated", "draft_id": updated.get("id", row.draft_id)}


def _in_reply_to(msg: dict) -> str | None:
    for h in (msg.get("payload") or {}).get("headers", []):
        if str(h.get("name", "")).lower() == "in-reply-to":
            return h.get("value")
    return None


def delete_draft(args: DeleteDraftInput, ctx: CallContext, session: Session) -> dict[str, Any]:
    row = _owned(session, ctx, args.draft_id)
    service = gmail_service(session, ctx)
    service.users().drafts().delete(userId="me", id=row.draft_id).execute()
    session.delete(row)
    return {"status": "draft_deleted", "draft_id": args.draft_id}


def register(registry) -> None:
    """Register Gmail draft tools (MUTATING but never sends; no confirmation round-trip)."""
    registry.add(
        ToolSpec(
            name="google_gmail_create_draft",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            requires_confirmation=False,
            description=(
                "Create a Gmail DRAFT (never sent). Supports HTML via body_html (a "
                "plain-text fallback is generated if body_text is omitted). Set "
                "reply_to_message_id (must be a labelled message) to draft a threaded reply."
            ),
            input_model=CreateDraftInput,
            handler=create_draft,
        )
    )
    registry.add(
        ToolSpec(
            name="google_gmail_update_draft",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            requires_confirmation=False,
            description="Update a draft created through this gateway. Never sends.",
            input_model=UpdateDraftInput,
            handler=update_draft,
        )
    )
    registry.add(
        ToolSpec(
            name="google_gmail_delete_draft",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            requires_confirmation=False,
            description="Delete a draft created through this gateway.",
            input_model=DeleteDraftInput,
            handler=delete_draft,
        )
    )
