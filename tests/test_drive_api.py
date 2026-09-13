"""Tests for the private Open WebUI Google Drive attachment API."""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

from starlette.requests import Request

from gateway.api import drive
from gateway.config import Settings
from gateway.identity.models import AuthenticatedUser


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
        base_url="https://mcp.ashpazi.shop",
        google_client_id="cid",
        google_client_secret="secret",
        apex_api_key="pk_live_test",
        token_encryption_key="x" * 43 + "=",
        gateway_shared_secret="shared-secret-value",
        trusted_open_webui_origin="https://openwebui.internal",
        session_secret="sess",
    )


def _request(headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw_headers})


def _body(response) -> dict:
    return json.loads(response.body)


def test_status_rejects_missing_and_forged_identity(monkeypatch):
    monkeypatch.setattr(drive, "get_settings", _settings)

    missing = drive.status(_request())
    forged = drive.status(_request({"X-OpenWebUI-User-Id": "alice"}))

    assert missing.status_code == 401
    assert forged.status_code == 401
    assert _body(missing)["error"] == "unauthorized"
    assert missing.headers["cache-control"] == "no-store"


def test_disconnected_status_returns_user_bound_drive_authorization_url(monkeypatch):
    auth = AuthenticatedUser("alice", "alice@example.com", "Alice")
    monkeypatch.setattr(drive, "get_settings", _settings)
    monkeypatch.setattr(drive, "_authenticate", lambda _request: (auth, uuid.uuid4()))

    @contextmanager
    def fake_session_scope():
        yield object()

    monkeypatch.setattr(drive, "session_scope", fake_session_scope)
    monkeypatch.setattr(drive, "get_active_connection", lambda _session, _user_id: None)

    response = drive.status(_request())
    body = _body(response)

    assert body["connected"] is False
    assert body["connection_state"] == "not_connected"
    assert body["authorization_url"].startswith(
        "https://mcp.ashpazi.shop/oauth/google/start?"
    )
    assert "product=drive" in body["authorization_url"]
    assert "shared-secret-value" not in response.body.decode()


def test_connected_status_returns_only_account_identity(monkeypatch):
    auth = AuthenticatedUser("alice", "alice@example.com", "Alice")
    monkeypatch.setattr(drive, "get_settings", _settings)
    monkeypatch.setattr(drive, "_authenticate", lambda _request: (auth, uuid.uuid4()))

    @contextmanager
    def fake_session_scope():
        yield object()

    connection = SimpleNamespace(
        provider_email="drive-account@example.com",
        scopes=[
            "https://www.googleapis.com/auth/drive.metadata.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
    )
    monkeypatch.setattr(drive, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        drive, "get_active_connection", lambda _session, _user_id: connection
    )

    response = drive.status(_request())

    assert _body(response) == {
        "connected": True,
        "connection_state": "connected",
        "account_email": "drive-account@example.com",
    }


def test_search_forwards_no_query_content_to_audit(monkeypatch):
    auth = AuthenticatedUser("alice")
    user_id = uuid.uuid4()
    audits = []
    monkeypatch.setattr(drive, "_authenticate", lambda _request: (auth, user_id))
    monkeypatch.setattr(drive, "get_settings", _settings)

    @contextmanager
    def fake_session_scope():
        yield object()

    monkeypatch.setattr(drive, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        drive,
        "get_active_connection",
        lambda _session, _user_id: SimpleNamespace(scopes=[]),
    )
    monkeypatch.setattr(drive, "build_scoped_drive_service", lambda *_args: object())
    monkeypatch.setattr(
        drive,
        "search_files",
        lambda _service, **_kwargs: {"files": [], "next_page_token": None},
    )
    monkeypatch.setattr(drive, "_audit", lambda **kwargs: audits.append(kwargs))

    response = drive.files(
        _request(), q="payroll acquisition", parent_id=None, page_size=25
    )

    assert response.status_code == 200
    assert audits[0]["input_summary"] == "query=<set> parent_id=<none>"
    assert "payroll acquisition" not in str(audits)
    assert response.headers["cache-control"] == "no-store"


def test_search_forwards_parent_id_to_search_files(monkeypatch):
    auth = AuthenticatedUser("alice")
    user_id = uuid.uuid4()
    captured_kwargs = {}
    monkeypatch.setattr(drive, "_authenticate", lambda _request: (auth, user_id))
    monkeypatch.setattr(drive, "get_settings", _settings)

    @contextmanager
    def fake_session_scope():
        yield object()

    monkeypatch.setattr(drive, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        drive,
        "get_active_connection",
        lambda _session, _user_id: SimpleNamespace(scopes=[]),
    )
    monkeypatch.setattr(drive, "build_scoped_drive_service", lambda *_args: object())

    def fake_search_files(_service, **kwargs):
        captured_kwargs.update(kwargs)
        return {"files": [], "next_page_token": None}

    monkeypatch.setattr(drive, "search_files", fake_search_files)
    monkeypatch.setattr(drive, "_audit", lambda **kwargs: None)

    response = drive.files(_request(), q="", parent_id="folder-123", page_size=25)

    assert response.status_code == 200
    assert captured_kwargs["parent_id"] == "folder-123"


def test_download_response_is_private_and_closes_stream(monkeypatch):
    auth = AuthenticatedUser("alice")
    user_id = uuid.uuid4()
    file_object = SimpleNamespace(
        data=b"payload",
        closed=False,
        read=lambda _size: b"",
        close=lambda: None,
    )
    monkeypatch.setattr(drive, "_authenticate", lambda _request: (auth, user_id))
    monkeypatch.setattr(drive, "get_settings", _settings)

    @contextmanager
    def fake_session_scope():
        yield object()

    monkeypatch.setattr(drive, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        drive,
        "get_active_connection",
        lambda _session, _user_id: SimpleNamespace(scopes=[]),
    )
    monkeypatch.setattr(drive, "build_scoped_drive_service", lambda *_args: object())
    monkeypatch.setattr(
        drive,
        "prepare_download",
        lambda *_args: SimpleNamespace(
            file=file_object,
            filename="Résumé.docx",
            mime_type="application/docx",
            size=7,
        ),
    )
    monkeypatch.setattr(drive, "_audit", lambda **_kwargs: None)

    response = drive.content(_request(), "file-123")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "filename*=UTF-8''R%C3%A9sum%C3%A9.docx" in response.headers[
        "content-disposition"
    ]

