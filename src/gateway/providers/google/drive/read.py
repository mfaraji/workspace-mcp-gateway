"""Read-only Google Drive MCP tools."""

from __future__ import annotations

from typing import Any

from pydantic import Field
from sqlalchemy.orm import Session

from gateway.providers.base import CallContext, RiskLevel, ToolSpec
from gateway.providers.google.common import GoogleAccountInput, make_list_accounts_spec
from gateway.providers.google.drive.common import (
    DRIVE_UI,
    PROVIDER,
    UI_NOTE,
    DriveProviderError,
    compact_metadata,
    drive_service,
    get_file,
    search_files,
)
from gateway.providers.registry import ToolError


class SearchFilesInput(GoogleAccountInput):
    query: str = Field("", max_length=200, description="Filename text; empty returns recent files.")
    page_token: str | None = Field(None, description="Opaque token from a previous response.")
    page_size: int = Field(25, ge=1, le=100, description="Maximum files to return.")


class GetFileMetadataInput(GoogleAccountInput):
    file_id: str = Field(..., min_length=1, max_length=1024, description="Google Drive file id.")


def _tool_call(callable_):
    try:
        return callable_()
    except DriveProviderError as exc:
        raise ToolError(exc.code, str(exc)) from exc


def search_drive_files(
    args: SearchFilesInput, ctx: CallContext, session: Session
) -> dict[str, Any]:
    """Search filenames across My Drive, shared files, and Shared Drives."""
    service = drive_service(session, ctx)
    return _tool_call(
        lambda: search_files(
            service,
            query=args.query,
            page_token=args.page_token,
            page_size=args.page_size,
        )
    )


def get_drive_file_metadata(
    args: GetFileMetadataInput, ctx: CallContext, session: Session
) -> dict[str, Any]:
    """Return compact, shortcut-resolved metadata for one accessible file."""
    service = drive_service(session, ctx)
    return _tool_call(lambda: compact_metadata(get_file(service, args.file_id)))


def register(registry) -> None:
    """Register Google Drive read tools."""
    registry.add(
        make_list_accounts_spec(
            name="google_drive_list_accounts",
            provider=PROVIDER,
            product="drive",
            display_name="Google Drive",
        )
    )
    registry.add(
        ToolSpec(
            name="google_drive_search_files",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "Search filenames across all Google Drive files accessible to the user, "
                "including shared files and Shared Drives. Empty query returns recent files. "
                + UI_NOTE
            ),
            input_model=SearchFilesInput,
            handler=search_drive_files,
            ui_resource=DRIVE_UI,
            ui_app_callable=True,
        )
    )
    registry.add(
        ToolSpec(
            name="google_drive_get_file_metadata",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "Get metadata and attachment compatibility for one Google Drive file. "
                + UI_NOTE
            ),
            input_model=GetFileMetadataInput,
            handler=get_drive_file_metadata,
            ui_resource=DRIVE_UI,
            ui_app_callable=True,
        )
    )
