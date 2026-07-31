"""Shared Google Drive metadata, shortcut, download, and error helpers."""

from __future__ import annotations

import io
import json
import tempfile
from dataclasses import dataclass
from typing import Any, BinaryIO

from sqlalchemy.orm import Session

from gateway.config import Settings, get_settings
from gateway.oauth.google import DRIVE_SCOPES, build_start_url
from gateway.providers.base import CallContext
from gateway.providers.google.client import build_drive_service
from gateway.providers.google.connections import get_active_connection

PROVIDER = "google"
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 1024 * 1024

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."

EXPORT_FORMATS: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
}

FILE_FIELDS = (
    "id,name,mimeType,size,modifiedTime,driveId,shared,ownedByMe,webViewLink,"
    "resourceKey,shortcutDetails(targetId,targetMimeType,targetResourceKey)"
)


class DriveProviderError(Exception):
    """A stable, non-sensitive Google Drive failure."""

    def __init__(self, code: str, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass
class PreparedDownload:
    """A bounded temporary download ready to stream to Open WebUI."""

    file: BinaryIO
    filename: str
    mime_type: str
    size: int


def required_scopes_present(scopes: list[str] | None) -> bool:
    """Return whether a connection has every read-only Drive scope."""
    return set(DRIVE_SCOPES).issubset(set(scopes or []))


def drive_service(session: Session, ctx: CallContext):
    """Resolve the caller's scoped Google connection and build Drive v3."""
    from gateway.providers.registry import ToolError

    settings = get_settings()
    conn = get_active_connection(session, ctx.user_id, PROVIDER)
    if conn is None:
        url = build_start_url(settings, ctx.external_user_id, product="drive")
        raise ToolError("not_connected", f"no active Google connection; authorize here: {url}")
    if not required_scopes_present(conn.scopes):
        url = build_start_url(settings, ctx.external_user_id, product="drive")
        raise ToolError(
            "reauth_required",
            f"Google Drive connection is missing required read scopes; reconnect here: {url}",
        )
    return build_drive_service(session, conn, settings)


def build_scoped_drive_service(session: Session, conn, settings: Settings):
    """Build Drive v3 for an HTTP route after validating its read scopes."""
    if not required_scopes_present(conn.scopes):
        raise DriveProviderError(
            "reauth_required",
            "Google Drive authorization must be renewed.",
            status_code=401,
        )
    try:
        return build_drive_service(session, conn, settings)
    except Exception as exc:
        from gateway.providers.google.connections import ReauthRequired

        if isinstance(exc, ReauthRequired):
            raise DriveProviderError(
                "reauth_required",
                "Google Drive authorization must be renewed.",
                status_code=401,
            ) from exc
        raise classify_google_error(exc) from exc


def escape_drive_query(value: str) -> str:
    """Escape a literal for the Drive API query language."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def search_files(service, *, query: str, page_token: str | None, page_size: int) -> dict:
    """Search the whole accessible Drive corpus, resolving shortcuts."""
    clauses = ["trashed = false", f"mimeType != '{FOLDER_MIME_TYPE}'"]
    if query.strip():
        clauses.append(f"name contains '{escape_drive_query(query.strip())}'")

    params: dict[str, Any] = {
        "q": " and ".join(clauses),
        "pageSize": page_size,
        "fields": f"nextPageToken,incompleteSearch,files({FILE_FIELDS})",
        "spaces": "drive",
        "corpora": "allDrives",
        "includeItemsFromAllDrives": True,
        "supportsAllDrives": True,
        "orderBy": "modifiedTime desc",
    }
    if page_token:
        params["pageToken"] = page_token

    try:
        response = service.files().list(**params).execute()
    except Exception as exc:
        raise classify_google_error(exc) from exc

    files = [
        resolve_shortcut(service, item, tolerate_broken=True)
        for item in response.get("files", [])
    ]
    return {
        "files": [compact_metadata(item) for item in files],
        "next_page_token": response.get("nextPageToken"),
        "incomplete_search": bool(response.get("incompleteSearch", False)),
    }


def get_file(service, file_id: str) -> dict:
    """Get accessible metadata and resolve a Drive shortcut target."""
    try:
        item = (
            service.files()
            .get(fileId=file_id, fields=FILE_FIELDS, supportsAllDrives=True)
            .execute()
        )
    except Exception as exc:
        raise classify_google_error(exc) from exc
    return resolve_shortcut(service, item, tolerate_broken=False)


def resolve_shortcut(service, item: dict, *, tolerate_broken: bool) -> dict:
    """Resolve a shortcut while preserving the shortcut id/name for selection."""
    if item.get("mimeType") != SHORTCUT_MIME_TYPE:
        return item

    target_id = (item.get("shortcutDetails") or {}).get("targetId")
    if not target_id:
        return _broken_shortcut(item) if tolerate_broken else _raise_broken_shortcut()

    try:
        target = (
            service.files()
            .get(fileId=target_id, fields=FILE_FIELDS, supportsAllDrives=True)
            .execute()
        )
    except Exception as exc:
        classified = classify_google_error(exc)
        if tolerate_broken and classified.code in {"not_found", "forbidden"}:
            return _broken_shortcut(item)
        raise classified from exc

    resolved = dict(target)
    resolved["id"] = item.get("id")
    resolved["name"] = item.get("name") or target.get("name")
    resolved["resolvedFileId"] = target.get("id")
    resolved["isShortcut"] = True
    resolved["shortcutTargetMimeType"] = target.get("mimeType")
    resolved["driveId"] = target.get("driveId") or item.get("driveId")
    resolved["shared"] = bool(target.get("shared") or item.get("shared"))
    return resolved


def _broken_shortcut(item: dict) -> dict:
    broken = dict(item)
    broken["isShortcut"] = True
    broken["brokenShortcut"] = True
    return broken


def _raise_broken_shortcut():
    raise DriveProviderError(
        "not_found", "The Drive shortcut target is missing or inaccessible.", status_code=404
    )


def compact_metadata(item: dict) -> dict:
    """Trim Drive metadata and derive whether the item may be attached."""
    mime_type = item.get("mimeType")
    raw_size = item.get("size")
    size = int(raw_size) if raw_size not in (None, "") else None
    broken = bool(item.get("brokenShortcut"))
    unsupported = bool(
        mime_type
        and mime_type.startswith(GOOGLE_NATIVE_PREFIX)
        and mime_type not in EXPORT_FORMATS
    )
    oversized = size is not None and size > MAX_ATTACHMENT_BYTES

    reason = None
    if broken:
        reason = "broken_shortcut"
    elif unsupported:
        reason = "unsupported_type"
    elif oversized:
        reason = "too_large"

    export = EXPORT_FORMATS.get(mime_type)
    return {
        "id": item.get("id"),
        "name": attachment_filename(item.get("name") or "Untitled", mime_type),
        "source_name": item.get("name"),
        "mime_type": export[0] if export else mime_type,
        "source_mime_type": mime_type,
        "size": size,
        "modified_time": item.get("modifiedTime"),
        "web_view_link": item.get("webViewLink"),
        "shared": bool(item.get("shared") or item.get("driveId")),
        "shared_drive_id": item.get("driveId"),
        "owned_by_me": item.get("ownedByMe"),
        "is_shortcut": bool(item.get("isShortcut")),
        "selectable": reason is None,
        "unselectable_reason": reason,
    }


def attachment_filename(name: str, mime_type: str | None) -> str:
    """Append an Office extension when exporting a Google-native file."""
    export = EXPORT_FORMATS.get(mime_type or "")
    if export is None:
        return name
    extension = export[1]
    return name if name.lower().endswith(extension) else f"{name}{extension}"


def prepare_download(service, file_id: str) -> PreparedDownload:
    """Download/export one accessible item into a bounded spooled temp file."""
    item = get_file(service, file_id)
    metadata = compact_metadata(item)
    if not metadata["selectable"]:
        reason = metadata["unselectable_reason"]
        status = 413 if reason == "too_large" else 422
        raise DriveProviderError(reason, _selection_error_message(reason), status_code=status)

    effective_file_id = item.get("resolvedFileId") or item.get("id")
    mime_type = item.get("mimeType")
    export = EXPORT_FORMATS.get(mime_type)
    try:
        if export:
            request = service.files().export_media(fileId=effective_file_id, mimeType=export[0])
            response_mime_type = export[0]
        else:
            request = service.files().get_media(
                fileId=effective_file_id,
                supportsAllDrives=True,
            )
            response_mime_type = mime_type or "application/octet-stream"

        output = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
        _download_request(request, output)
    except DriveProviderError:
        raise
    except Exception as exc:
        raise classify_google_error(exc) from exc

    size = output.tell()
    output.seek(0)
    return PreparedDownload(
        file=output,
        filename=attachment_filename(item.get("name") or "Untitled", mime_type),
        mime_type=response_mime_type,
        size=size,
    )


def _download_request(request, output: BinaryIO) -> None:
    from googleapiclient.http import MediaIoBaseDownload

    downloader = MediaIoBaseDownload(output, request, chunksize=DOWNLOAD_CHUNK_BYTES)
    done = False
    try:
        while not done:
            _, done = downloader.next_chunk()
            if output.tell() > MAX_ATTACHMENT_BYTES:
                raise DriveProviderError(
                    "too_large",
                    "The exported file exceeds the 50 MB attachment limit.",
                    status_code=413,
                )
    except Exception:
        output.close()
        raise


def _selection_error_message(reason: str) -> str:
    if reason == "too_large":
        return "The file exceeds the 50 MB attachment limit."
    if reason == "broken_shortcut":
        return "The Drive shortcut target is missing or inaccessible."
    return "This Google-native file type cannot be attached."


def classify_google_error(exc: Exception) -> DriveProviderError:
    """Map Google API failures to stable errors without exposing response bodies."""
    if isinstance(exc, DriveProviderError):
        return exc

    try:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError):
            status = int(getattr(exc.resp, "status", 0) or 0)
            reasons: set[str] = set()
            try:
                body = json.loads(exc.content.decode("utf-8"))
                errors = body.get("error", {}).get("errors", [])
                reasons = {str(entry.get("reason")) for entry in errors if entry.get("reason")}
            except Exception:
                pass

            if status == 401 or reasons & {"authError", "insufficientPermissions"}:
                return DriveProviderError(
                    "reauth_required",
                    "Google Drive authorization must be renewed.",
                    status_code=401,
                )
            if status == 404:
                return DriveProviderError(
                    "not_found", "The Drive file is missing or inaccessible.", status_code=404
                )
            if status == 429 or reasons & {
                "rateLimitExceeded",
                "userRateLimitExceeded",
                "sharingRateLimitExceeded",
            }:
                return DriveProviderError(
                    "rate_limited",
                    "Google Drive is temporarily rate limited. Try again shortly.",
                    status_code=429,
                )
            if status == 403:
                return DriveProviderError(
                    "forbidden", "The Drive file is not accessible.", status_code=403
                )
    except Exception:
        pass

    if isinstance(exc, (TimeoutError, io.BlockingIOError)):
        return DriveProviderError(
            "provider_timeout", "Google Drive did not respond in time.", status_code=504
        )
    return DriveProviderError(
        "provider_error", "Google Drive could not complete the request.", status_code=502
    )
