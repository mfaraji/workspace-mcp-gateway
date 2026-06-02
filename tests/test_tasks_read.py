"""Tests for Google Tasks read tools."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from gateway.config import Settings
from gateway.providers.base import CallContext
from gateway.providers.google.tasks import common, read
from gateway.providers.registry import ToolError


class _FakeExecute:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeTasklists:
    def __init__(self):
        self.list_kwargs = None

    def list(self, **kwargs):
        self.list_kwargs = kwargs
        return _FakeExecute(
            {
                "items": [{"id": "tl1", "title": "Inbox", "updated": "2026-06-01T00:00:00Z"}],
                "nextPageToken": "next-tl",
            }
        )


class _FakeTasks:
    def __init__(self):
        self.list_kwargs = None

    def list(self, **kwargs):
        self.list_kwargs = kwargs
        return _FakeExecute(
            {
                "items": [
                    {
                        "id": "task1",
                        "title": "Buy milk",
                        "notes": "2%",
                        "status": "needsAction",
                        "due": "2026-06-03T00:00:00.000Z",
                    }
                ],
                "nextPageToken": "next-task",
            }
        )


class _FakeTasksService:
    def __init__(self):
        self.tasklists_resource = _FakeTasklists()
        self.tasks_resource = _FakeTasks()

    def tasklists(self):
        return self.tasklists_resource

    def tasks(self):
        return self.tasks_resource


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
        base_url="https://mcp.ashpazi.shop",
        google_client_id="cid",
        google_client_secret="secret",
        token_encryption_key="x" * 43 + "=",
        gateway_shared_secret="shared-secret-value",
        trusted_open_webui_origin="https://openwebui.internal",
        session_secret="sess",
    )


def test_list_tasklists_passes_pagination_and_trims_result(monkeypatch):
    service = _FakeTasksService()
    monkeypatch.setattr(read, "tasks_service", lambda _session, _ctx: service)
    ctx = CallContext(user_id=uuid.uuid4(), external_user_id="alice", request_id="r1")

    result = read.list_tasklists(
        read.ListTasklistsInput(max_results=10, page_token="page-1"), ctx, session=None
    )

    assert service.tasklists_resource.list_kwargs == {
        "maxResults": 10,
        "pageToken": "page-1",
    }
    assert result == {
        "tasklists": [
            {
                "id": "tl1",
                "title": "Inbox",
                "updated": "2026-06-01T00:00:00Z",
                "self_link": None,
            }
        ],
        "next_page_token": "next-tl",
    }


def test_list_tasks_passes_filters_and_trims_result(monkeypatch):
    service = _FakeTasksService()
    monkeypatch.setattr(read, "tasks_service", lambda _session, _ctx: service)
    ctx = CallContext(user_id=uuid.uuid4(), external_user_id="alice", request_id="r1")

    result = read.list_tasks(
        read.ListTasksInput(
            tasklist_id="tl1",
            max_results=5,
            show_completed=True,
            due_min="2026-06-01T00:00:00Z",
        ),
        ctx,
        session=None,
    )

    assert service.tasks_resource.list_kwargs == {
        "tasklist": "tl1",
        "maxResults": 5,
        "showCompleted": True,
        "showDeleted": False,
        "showHidden": False,
        "showAssigned": False,
        "dueMin": "2026-06-01T00:00:00Z",
    }
    assert result["tasklist_id"] == "tl1"
    assert result["next_page_token"] == "next-task"
    assert result["tasks"][0]["id"] == "task1"
    assert result["tasks"][0]["title"] == "Buy milk"


def test_tasks_service_reauth_error_includes_product_scoped_link(monkeypatch):
    monkeypatch.setattr(common, "get_settings", _settings)
    monkeypatch.setattr(
        common,
        "get_active_connection",
        lambda _session, _user_id, _provider: SimpleNamespace(scopes=[]),
    )
    ctx = CallContext(user_id=uuid.uuid4(), external_user_id="alice", request_id="r1")

    with pytest.raises(ToolError) as exc_info:
        common.tasks_service(session=None, ctx=ctx)

    assert exc_info.value.error_code == "reauth_required"
    message = str(exc_info.value)
    assert "reconnect here: https://mcp.ashpazi.shop/oauth/google/start?" in message
    assert "product=tasks" in message

