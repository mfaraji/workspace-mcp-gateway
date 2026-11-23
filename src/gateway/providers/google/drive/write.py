"""Google Drive write tools: create, copy, update, trash, share and request access.

All of them use the full ``drive`` scope (see :func:`drive_write_service`). Sharing is
limited to named users/groups with reader/commenter/writer; there is no link sharing,
no domain sharing and no ownership transfer, and nothing is ever permanently deleted.
"""

from __future__ import annotations

import io
import re
from typing import Any, Literal

from pydantic import Field, model_validator
from sqlalchemy.orm import Session

from gateway.providers.base import CallContext, RiskLevel, ToolSpec
from gateway.providers.google.common import GoogleAccountInput
from gateway.providers.google.drive.common import (
    FILE_FIELDS,
    FOLDER_MIME_TYPE,
    PROVIDER,
    DriveProviderError,
    classify_google_error,
    compact_metadata,
    drive_write_service,
)
from gateway.providers.registry import ToolError
from gateway.storage.staged_files import MAX_FILE_BYTES, take

_EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")
_FILE_ID = Field(..., min_length=1, max_length=1024, description="Google Drive file id.")


def _api(request):
    try:
        return request.execute()
    except Exception as exc:
        raise classify_google_error(exc) from exc


def _tool_call(callable_):
    try:
        return callable_()
    except DriveProviderError as exc:
        raise ToolError(exc.code, str(exc)) from exc


def _media(data: bytes, mime_type: str):
    from googleapiclient.http import MediaIoBaseUpload

    return MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=False)


def _content_bytes(
    session: Session, ctx: CallContext, content: str | None, file_ref: str | None, mime: str | None
) -> tuple[bytes, str] | None:
    """Resolve inline text or a staged file into ``(bytes, media mime type)``."""
    if file_ref:
        staged = take(session, ctx.user_id, file_ref)
        return staged.data, staged.mime_type
    if content is not None:
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise ToolError("too_large", "content exceeds the 10 MB limit")
        return data, "text/plain" if not mime or mime.startswith(
            "application/vnd.google-apps."
        ) else mime
    return None


class CreateFileInput(GoogleAccountInput):
    name: str = Field(..., min_length=1, max_length=255, description="File name.")
    content: str | None = Field(None, description="Text content for the new file.")
    file_ref: str | None = Field(
        None,
        description=(
            "file_ref from google_gmail_download_attachment or google_drive_download_file_content "
            "to save that file without passing its bytes through the model."
        ),
    )
    mime_type: str | None = Field(
        None,
        max_length=127,
        description=(
            "Target type. Use application/vnd.google-apps.folder to create a folder, or a "
            "application/vnd.google-apps.* type to convert text into a Google Doc/Sheet."
        ),
    )
    parent_id: str | None = Field(None, max_length=1024, description="Folder id to create it in.")
    description: str | None = Field(None, max_length=1000)

    @model_validator(mode="after")
    def _one_source(self):
        if self.content is not None and self.file_ref:
            raise ValueError("give either content or file_ref, not both")
        return self


class CopyFileInput(GoogleAccountInput):
    file_id: str = _FILE_ID
    name: str | None = Field(None, min_length=1, max_length=255, description="Name of the copy.")
    parent_id: str | None = Field(None, max_length=1024, description="Folder for the copy.")


class UpdateFileInput(GoogleAccountInput):
    file_id: str = _FILE_ID
    name: str | None = Field(None, min_length=1, max_length=255, description="New name.")
    description: str | None = Field(None, max_length=1000)
    content: str | None = Field(None, description="Replace the file's content with this text.")
    file_ref: str | None = Field(None, description="Replace the content with a staged file.")
    parent_id: str | None = Field(
        None, max_length=1024, description="Move the file into this folder."
    )

    @model_validator(mode="after")
    def _something_to_change(self):
        if self.content is not None and self.file_ref:
            raise ValueError("give either content or file_ref, not both")
        fields = (self.name, self.description, self.content, self.file_ref, self.parent_id)
        if all(v is None for v in fields):
            raise ValueError("nothing to update")
        return self


class TrashFileInput(GoogleAccountInput):
    file_id: str = _FILE_ID


class ShareFileInput(GoogleAccountInput):
    file_id: str = _FILE_ID
    email: str = Field(..., max_length=320, description="Email of the person or group.")
    role: Literal["reader", "commenter", "writer"] = Field(..., description="Access to grant.")
    type: Literal["user", "group"] = Field("user", description="Whether email is a user or group.")
    send_notification: bool = Field(True, description="Email the recipient about the share.")
    message: str | None = Field(None, max_length=1000, description="Optional note in the email.")

    @model_validator(mode="after")
    def _valid_email(self):
        if not _EMAIL_RE.match(self.email.strip()):
            raise ValueError("invalid email address")
        return self


class RequestAccessInput(GoogleAccountInput):
    file_id: str = _FILE_ID
    message: str | None = Field(None, max_length=1000)


def create_file(args: CreateFileInput, ctx: CallContext, session: Session) -> dict[str, Any]:
    service = drive_write_service(session, ctx)
    body: dict[str, Any] = {"name": args.name}
    if args.description:
        body["description"] = args.description
    if args.parent_id:
        body["parents"] = [args.parent_id]
    kwargs: dict[str, Any] = {"body": body, "fields": FILE_FIELDS, "supportsAllDrives": True}
    if args.mime_type == FOLDER_MIME_TYPE:
        if args.content is not None or args.file_ref:
            raise ToolError("invalid_input", "a folder cannot have content")
        body["mimeType"] = FOLDER_MIME_TYPE
    else:
        resolved = _content_bytes(session, ctx, args.content, args.file_ref, args.mime_type)
        if resolved is not None:
            data, media_mime = resolved
            body["mimeType"] = args.mime_type or media_mime
            kwargs["media_body"] = _media(data, media_mime)
        elif args.mime_type:
            body["mimeType"] = args.mime_type
    created = _tool_call(lambda: _api(service.files().create(**kwargs)))
    return compact_metadata(created)


def copy_file(args: CopyFileInput, ctx: CallContext, session: Session) -> dict[str, Any]:
    service = drive_write_service(session, ctx)
    body: dict[str, Any] = {}
    if args.name:
        body["name"] = args.name
    if args.parent_id:
        body["parents"] = [args.parent_id]
    copied = _tool_call(
        lambda: _api(
            service.files().copy(
                fileId=args.file_id, body=body, fields=FILE_FIELDS, supportsAllDrives=True
            )
        )
    )
    return compact_metadata(copied)


def update_file(args: UpdateFileInput, ctx: CallContext, session: Session) -> dict[str, Any]:
    service = drive_write_service(session, ctx)
    body: dict[str, Any] = {}
    if args.name:
        body["name"] = args.name
    if args.description is not None:
        body["description"] = args.description
    kwargs: dict[str, Any] = {
        "fileId": args.file_id,
        "body": body,
        "fields": FILE_FIELDS,
        "supportsAllDrives": True,
    }
    resolved = _content_bytes(session, ctx, args.content, args.file_ref, None)
    if resolved is not None:
        data, media_mime = resolved
        kwargs["media_body"] = _media(data, media_mime)

    def run() -> dict[str, Any]:
        if args.parent_id:
            current = _api(
                service.files().get(fileId=args.file_id, fields="parents", supportsAllDrives=True)
            )
            kwargs["addParents"] = args.parent_id
            old = [p for p in current.get("parents", []) if p != args.parent_id]
            if old:
                kwargs["removeParents"] = ",".join(old)
        return _api(service.files().update(**kwargs))

    return compact_metadata(_tool_call(run))


def trash_file(args: TrashFileInput, ctx: CallContext, session: Session) -> dict[str, Any]:
    """Move to the trash (recoverable). Permanent deletion is never offered."""
    service = drive_write_service(session, ctx)
    updated = _tool_call(
        lambda: _api(
            service.files().update(
                fileId=args.file_id,
                body={"trashed": True},
                fields="id,name,trashed",
                supportsAllDrives=True,
            )
        )
    )
    return {"id": updated.get("id"), "trashed": bool(updated.get("trashed", True))}


def share_file(args: ShareFileInput, ctx: CallContext, session: Session) -> dict[str, Any]:
    service = drive_write_service(session, ctx)
    kwargs: dict[str, Any] = {
        "fileId": args.file_id,
        "body": {"type": args.type, "role": args.role, "emailAddress": args.email.strip()},
        "sendNotificationEmail": args.send_notification,
        "supportsAllDrives": True,
        "fields": "id,type,role,emailAddress",
    }
    if args.send_notification and args.message:
        kwargs["emailMessage"] = args.message
    perm = _tool_call(lambda: _api(service.permissions().create(**kwargs)))
    return {
        "permission_id": perm.get("id"),
        "type": perm.get("type"),
        "role": perm.get("role"),
        "email_address": perm.get("emailAddress"),
    }


def request_access(args: RequestAccessInput, ctx: CallContext, session: Session) -> dict[str, Any]:
    """The Drive API has no request-access call; point the user to Drive's own flow."""
    service = drive_write_service(session, ctx)
    url = f"https://drive.google.com/file/d/{args.file_id}/view"
    try:
        _api(service.files().get(fileId=args.file_id, fields="id", supportsAllDrives=True))
    except DriveProviderError as exc:
        if exc.code in {"not_found", "forbidden"}:
            return {
                "status": "manual_action_required",
                "url": url,
                "instructions": (
                    "Google's API cannot send access requests. Ask the user to open this link "
                    "with the account that needs access and press 'Request access'."
                ),
            }
        raise ToolError(exc.code, str(exc)) from exc
    return {"status": "already_has_access", "url": url}


def _create_preview(a: CreateFileInput) -> str:
    kind = "folder" if a.mime_type == FOLDER_MIME_TYPE else "file"
    where = f" in folder {a.parent_id}" if a.parent_id else ""
    return f"Create Drive {kind} '{a.name}'{where}."


def _copy_preview(a: CopyFileInput) -> str:
    return f"Copy Drive file {a.file_id}" + (f" as '{a.name}'." if a.name else ".")


def _update_preview(a: UpdateFileInput) -> str:
    parts = [
        label
        for label, v in (
            ("rename", a.name),
            ("change the description", a.description),
            ("replace the content", a.content if a.content is not None else a.file_ref),
            ("move", a.parent_id),
        )
        if v is not None
    ]
    return f"Update Drive file {a.file_id}: {', '.join(parts)}."


def _trash_preview(a: TrashFileInput) -> str:
    return f"Move Drive file {a.file_id} to the trash (it can be restored from Drive)."


def _share_preview(a: ShareFileInput) -> str:
    notify = "; they will be emailed" if a.send_notification else ""
    return f"Share Drive file {a.file_id} with {a.email.strip()} as {a.role}{notify}."


def _access_preview(a: RequestAccessInput) -> str:
    return f"Check access to Drive file {a.file_id} and give you the link to request it."


def register(registry) -> None:
    specs = (
        ("google_drive_create_file", "Create a Google Drive file or folder from text, or save a "
         "staged file_ref (e.g. an email attachment) without sending its bytes through the model.",
         CreateFileInput, create_file, _create_preview, False),
        ("google_drive_copy_file", "Copy a Google Drive file, optionally renaming it or "
         "choosing a folder.", CopyFileInput, copy_file, _copy_preview, False),
        ("google_drive_update_file", "Rename, move, or replace the content/description of a "
         "Google Drive file.", UpdateFileInput, update_file, _update_preview, None),
        ("google_drive_trash_file", "Move a Google Drive file to the trash (recoverable; there "
         "is no permanent delete).", TrashFileInput, trash_file, _trash_preview, None),
        ("google_drive_share_file", "Share a Google Drive file with one person or group as "
         "reader, commenter or writer. Link sharing and ownership transfer are not available.",
         ShareFileInput, share_file, _share_preview, None),
        ("google_drive_request_access", "Check whether the user can open a Google Drive file and "
         "return the link to request access if not.", RequestAccessInput, request_access,
         _access_preview, None),
    )  # fmt: skip
    for name, description, model, handler, preview, confirm in specs:
        registry.add(
            ToolSpec(
                name=name,
                provider=PROVIDER,
                risk=RiskLevel.MUTATING,
                description=description,
                input_model=model,
                handler=handler,
                preview_builder=preview,
                requires_confirmation=confirm,
            )
        )
