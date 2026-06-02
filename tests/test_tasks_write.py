"""Tests for Google Tasks mutating tools."""

from __future__ import annotations

import uuid

from gateway.audit.log import summarize_input
from gateway.config import Settings
from gateway.policy import confirm
from gateway.providers.base import CallContext, RiskLevel
from gateway.providers.google.tasks import write
from gateway.providers.registry import ToolRegistry


class _FakeExecute:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeTasks:
    def __init__(self):
        self.insert_kwargs = None
        self.patch_kwargs = None
        self.delete_kwargs = None

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        return _FakeExecute({"id": "task1", "title": kwargs["body"]["title"]})

    def patch(self, **kwargs):
        self.patch_kwargs = kwargs
        return _FakeExecute(
            {
                "id": kwargs["task"],
                "title": kwargs["body"].get("title"),
                "status": kwargs["body"].get("status", "needsAction"),
            }
        )

    def delete(self, **kwargs):
        self.delete_kwargs = kwargs
        return _FakeExecute({})


class _FakeTasksService:
    def __init__(self):
        self.tasks_resource = _FakeTasks()

    def tasks(self):
        return self.tasks_resource


def _settings(**overrides) -> Settings:
    base = dict(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
        base_url="http://localhost:8000",
        google_client_id="cid",
        google_client_secret="secret",
        token_encryption_key="x" * 43 + "=",
        gateway_shared_secret="shared-secret-value",
        trusted_open_webui_origin="https://openwebui.internal",
        session_secret="sess",
    )
    base.update(overrides)
    return Settings(**base)


def _registered() -> dict[str, object]:
    registry = ToolRegistry()
    write.register(registry)
    return {spec.name: spec for spec in registry._specs}


def test_write_tools_are_registered_as_mutating():
    specs = _registered()
    assert set(specs) == {
        "google_tasks_create_task",
        "google_tasks_update_task",
        "google_tasks_complete_task",
        "google_tasks_delete_task",
    }
    assert all(s.risk is RiskLevel.MUTATING for s in specs.values())
    assert all(s.preview_builder is not None for s in specs.values())


def test_create_preview_includes_title_but_audit_does_not():
    spec = _registered()["google_tasks_create_task"]
    args = {
        "tasklist_id": "@default",
        "title": "Secret task",
        "notes": "private notes",
        "due": "2026-06-03T00:00:00Z",
    }
    model = spec.input_model(**args)

    preview = spec.preview_builder(model)
    assert "Secret task" in preview

    summary = summarize_input("google_tasks_create_task", args)
    assert "Secret task" not in summary
    assert "private notes" not in summary
    assert "tasklist_id='@default'" in summary
    assert "due=" in summary


def test_create_task_calls_insert_with_optional_position(monkeypatch):
    service = _FakeTasksService()
    monkeypatch.setattr(write, "tasks_service", lambda _session, _ctx: service)
    ctx = CallContext(user_id=uuid.uuid4(), external_user_id="alice", request_id="r1")

    result = write.create_task(
        write.CreateTaskInput(
            tasklist_id="tl1",
            title="Buy milk",
            notes="2%",
            due="2026-06-03T00:00:00Z",
            parent="parent1",
            previous="prev1",
        ),
        ctx,
        session=None,
    )

    assert service.tasks_resource.insert_kwargs == {
        "tasklist": "tl1",
        "body": {
            "title": "Buy milk",
            "notes": "2%",
            "due": "2026-06-03T00:00:00Z",
        },
        "parent": "parent1",
        "previous": "prev1",
    }
    assert result["status"] == "created"
    assert result["id"] == "task1"


def test_update_and_complete_task_use_patch(monkeypatch):
    service = _FakeTasksService()
    monkeypatch.setattr(write, "tasks_service", lambda _session, _ctx: service)
    ctx = CallContext(user_id=uuid.uuid4(), external_user_id="alice", request_id="r1")

    write.update_task(
        write.UpdateTaskInput(
            tasklist_id="tl1",
            task_id="task1",
            title="New title",
            status="needsAction",
        ),
        ctx,
        session=None,
    )
    assert service.tasks_resource.patch_kwargs == {
        "tasklist": "tl1",
        "task": "task1",
        "body": {"title": "New title", "status": "needsAction"},
    }

    result = write.complete_task(
        write.CompleteTaskInput(tasklist_id="tl1", task_id="task1"),
        ctx,
        session=None,
    )
    assert service.tasks_resource.patch_kwargs == {
        "tasklist": "tl1",
        "task": "task1",
        "body": {"status": "completed"},
    }
    assert result["status"] == "completed"


def test_delete_task_calls_delete(monkeypatch):
    service = _FakeTasksService()
    monkeypatch.setattr(write, "tasks_service", lambda _session, _ctx: service)
    ctx = CallContext(user_id=uuid.uuid4(), external_user_id="alice", request_id="r1")

    result = write.delete_task(
        write.DeleteTaskInput(tasklist_id="tl1", task_id="task1"), ctx, session=None
    )

    assert service.tasks_resource.delete_kwargs == {"tasklist": "tl1", "task": "task1"}
    assert result == {"status": "deleted", "tasklist_id": "tl1", "task_id": "task1"}


def test_delete_task_requires_then_accepts_confirmation():
    settings = _settings()
    spec = _registered()["google_tasks_delete_task"]
    args = {"tasklist_id": "tl1", "task_id": "task1"}
    ctx = CallContext(user_id=uuid.uuid4(), external_user_id="alice", request_id="r1")

    first = confirm.check(
        settings,
        spec,
        args,
        ctx,
        preview_builder=lambda _: spec.preview_builder(spec.input_model(**args)),
    )
    assert isinstance(first, confirm.NeedsConfirmation)
    assert "task1" in first.preview

    second = confirm.check(
        settings, spec, args, ctx, confirmation_token=first.confirmation_token
    )
    assert isinstance(second, confirm.Allow)
