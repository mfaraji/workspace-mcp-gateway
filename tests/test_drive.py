"""Tests for Google Drive search, metadata, shortcuts, and downloads."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError

from gateway.providers.google.drive import common


class _Execute:
    def __init__(self, result):
        self.result = result

    def execute(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _Files:
    def __init__(self, *, listed=None, metadata=None):
        self.listed = listed or {"files": []}
        self.metadata = metadata or {}
        self.list_kwargs = None
        self.get_calls = []
        self.export_calls = []
        self.media_calls = []

    def list(self, **kwargs):
        self.list_kwargs = kwargs
        return _Execute(self.listed)

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return _Execute(self.metadata[kwargs["fileId"]])

    def export_media(self, **kwargs):
        self.export_calls.append(kwargs)
        return SimpleNamespace(payload=b"export")

    def get_media(self, **kwargs):
        self.media_calls.append(kwargs)
        return SimpleNamespace(payload=b"regular")


class _Service:
    def __init__(self, files):
        self.resource = files

    def files(self):
        return self.resource


class _Downloader:
    def __init__(self, output, request, chunksize):
        self.output = output
        self.request = request
        self.chunksize = chunksize
        self.done = False

    def next_chunk(self):
        self.output.write(self.request.payload)
        self.done = True
        return None, True


def test_escape_drive_query_handles_slashes_and_quotes():
    assert common.escape_drive_query("team\\'s plan") == "team\\\\\\'s plan"


def test_search_uses_safe_query_pagination_and_shared_drive_flags():
    files = _Files(listed={"files": [], "nextPageToken": "opaque", "incompleteSearch": True})
    result = common.search_files(
        _Service(files), query="O'Brien", page_token="prior", page_size=33
    )

    assert "name contains 'O\\'Brien'" in files.list_kwargs["q"]
    assert "trashed = false" in files.list_kwargs["q"]
    assert "mimeType != 'application/vnd.google-apps.folder'" in files.list_kwargs["q"]
    assert files.list_kwargs["pageToken"] == "prior"
    assert files.list_kwargs["pageSize"] == 33
    assert files.list_kwargs["corpora"] == "allDrives"
    assert files.list_kwargs["includeItemsFromAllDrives"] is True
    assert files.list_kwargs["supportsAllDrives"] is True
    assert files.list_kwargs["orderBy"] == "modifiedTime desc"
    assert result["next_page_token"] == "opaque"
    assert result["incomplete_search"] is True


def test_empty_search_returns_recent_files_without_name_clause():
    files = _Files()
    common.search_files(_Service(files), query="  ", page_token=None, page_size=25)

    assert "name contains" not in files.list_kwargs["q"]
    assert "pageToken" not in files.list_kwargs
    assert files.list_kwargs["orderBy"] == "modifiedTime desc"


def test_search_with_parent_id_narrows_to_folder():
    files = _Files()
    common.search_files(
        _Service(files),
        query="",
        page_token=None,
        page_size=25,
        parent_id="folder-123",
    )

    assert "'folder-123' in parents" in files.list_kwargs["q"]


def test_search_without_parent_id_omits_parents_clause():
    files = _Files()
    common.search_files(_Service(files), query="", page_token=None, page_size=25)

    assert "in parents" not in files.list_kwargs["q"]


def test_metadata_is_trimmed_and_marks_shared_and_oversized(monkeypatch):
    monkeypatch.setattr(common, "MAX_ATTACHMENT_BYTES", 10)
    result = common.compact_metadata(
        {
            "id": "f1",
            "name": "report.pdf",
            "mimeType": "application/pdf",
            "size": "11",
            "modifiedTime": "2026-01-01T00:00:00Z",
            "driveId": "shared-drive",
            "owners": [{"emailAddress": "must-not-leak@example.com"}],
        }
    )

    assert result == {
        "id": "f1",
        "name": "report.pdf",
        "source_name": "report.pdf",
        "mime_type": "application/pdf",
        "source_mime_type": "application/pdf",
        "size": 11,
        "modified_time": "2026-01-01T00:00:00Z",
        "web_view_link": None,
        "shared": True,
        "shared_drive_id": "shared-drive",
        "owned_by_me": None,
        "is_shortcut": False,
        "selectable": False,
        "unselectable_reason": "too_large",
    }


def test_shortcut_resolves_accessible_target_and_preserves_shortcut_identity():
    files = _Files(
        metadata={
            "target": {
                "id": "target",
                "name": "Target",
                "mimeType": "application/pdf",
                "size": "7",
                "driveId": "team-drive",
            }
        }
    )
    item = {
        "id": "shortcut",
        "name": "Friendly name",
        "mimeType": common.SHORTCUT_MIME_TYPE,
        "shortcutDetails": {"targetId": "target"},
    }

    resolved = common.resolve_shortcut(_Service(files), item, tolerate_broken=False)

    assert resolved["id"] == "shortcut"
    assert resolved["resolvedFileId"] == "target"
    assert resolved["name"] == "Friendly name"
    assert resolved["mimeType"] == "application/pdf"
    assert resolved["isShortcut"] is True


def _http_error(status: int, reason: str) -> HttpError:
    response = SimpleNamespace(status=status, reason="error")
    content = (
        '{"error":{"errors":[{"reason":"' + reason + '"}]}}'
    ).encode()
    return HttpError(response, content, uri="https://google.invalid")


def test_broken_shortcut_is_unselectable_in_search():
    files = _Files(metadata={"target": _http_error(404, "notFound")})
    item = {
        "id": "shortcut",
        "name": "Gone",
        "mimeType": common.SHORTCUT_MIME_TYPE,
        "shortcutDetails": {"targetId": "target"},
    }

    resolved = common.resolve_shortcut(_Service(files), item, tolerate_broken=True)
    assert common.compact_metadata(resolved)["unselectable_reason"] == "broken_shortcut"


@pytest.mark.parametrize(
    ("source_mime", "export_mime", "extension"),
    [
        (
            "application/vnd.google-apps.document",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".docx",
        ),
        (
            "application/vnd.google-apps.spreadsheet",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xlsx",
        ),
        (
            "application/vnd.google-apps.presentation",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".pptx",
        ),
    ],
)
def test_google_native_files_export_with_office_filename(
    monkeypatch, source_mime, export_mime, extension
):
    monkeypatch.setattr("googleapiclient.http.MediaIoBaseDownload", _Downloader)
    files = _Files(metadata={"f1": {"id": "f1", "name": "Quarterly", "mimeType": source_mime}})

    prepared = common.prepare_download(_Service(files), "f1")
    try:
        assert files.export_calls == [{"fileId": "f1", "mimeType": export_mime}]
        assert prepared.filename == f"Quarterly{extension}"
        assert prepared.mime_type == export_mime
        assert prepared.file.read() == b"export"
    finally:
        prepared.file.close()


def test_regular_file_downloads_unchanged(monkeypatch):
    monkeypatch.setattr("googleapiclient.http.MediaIoBaseDownload", _Downloader)
    files = _Files(
        metadata={
            "f1": {
                "id": "f1",
                "name": "manual.pdf",
                "mimeType": "application/pdf",
                "size": "7",
            }
        }
    )

    prepared = common.prepare_download(_Service(files), "f1")
    try:
        assert files.media_calls == [{"fileId": "f1", "supportsAllDrives": True}]
        assert prepared.filename == "manual.pdf"
        assert prepared.mime_type == "application/pdf"
        assert prepared.file.read() == b"regular"
    finally:
        prepared.file.close()


def test_stream_enforces_post_export_limit(monkeypatch):
    monkeypatch.setattr("googleapiclient.http.MediaIoBaseDownload", _Downloader)
    monkeypatch.setattr(common, "MAX_ATTACHMENT_BYTES", 3)
    files = _Files(
        metadata={
            "f1": {
                "id": "f1",
                "name": "Doc",
                "mimeType": "application/vnd.google-apps.document",
            }
        }
    )

    with pytest.raises(common.DriveProviderError) as exc_info:
        common.prepare_download(_Service(files), "f1")

    assert exc_info.value.code == "too_large"
    assert exc_info.value.status_code == 413


@pytest.mark.parametrize(
    ("status", "reason", "code"),
    [
        (401, "authError", "reauth_required"),
        (403, "insufficientPermissions", "reauth_required"),
        (404, "notFound", "not_found"),
        (429, "rateLimitExceeded", "rate_limited"),
        (500, "backendError", "provider_error"),
    ],
)
def test_google_errors_are_stable(status, reason, code):
    assert common.classify_google_error(_http_error(status, reason)).code == code


def test_unsupported_google_native_type_is_unselectable():
    result = common.compact_metadata(
        {
            "id": uuid.uuid4().hex,
            "name": "Survey",
            "mimeType": "application/vnd.google-apps.form",
        }
    )
    assert result["selectable"] is False
    assert result["unselectable_reason"] == "unsupported_type"

