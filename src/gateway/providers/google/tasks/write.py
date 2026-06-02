"""Google Tasks mutating tools."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gateway.providers.base import CallContext, RiskLevel, ToolSpec
from gateway.providers.google.tasks.common import PROVIDER, tasks_service, trim_task

TaskStatus = Literal["needsAction", "completed"]


class CreateTaskInput(BaseModel):
    tasklist_id: str = Field("@default", description="Task list id, or '@default'.")
    title: str = Field(..., max_length=1024, description="Task title.")
    notes: str | None = Field(None, max_length=8192, description="Task notes.")
    due: str | None = Field(
        None,
        description=(
            "RFC3339 due date. Google Tasks stores only date information and discards "
            "the time portion."
        ),
    )
    parent: str | None = Field(None, description="Optional parent task id for a subtask.")
    previous: str | None = Field(None, description="Optional previous sibling task id.")


class UpdateTaskInput(BaseModel):
    tasklist_id: str = Field("@default", description="Task list id, or '@default'.")
    task_id: str = Field(..., description="Task id to update.")
    title: str | None = Field(None, max_length=1024, description="New task title.")
    notes: str | None = Field(None, max_length=8192, description="New task notes.")
    due: str | None = Field(
        None,
        description=(
            "New RFC3339 due date. Google Tasks stores only date information and "
            "discards the time portion."
        ),
    )
    status: TaskStatus | None = Field(None, description="'needsAction' or 'completed'.")


class CompleteTaskInput(BaseModel):
    tasklist_id: str = Field("@default", description="Task list id, or '@default'.")
    task_id: str = Field(..., description="Task id to complete.")


class DeleteTaskInput(BaseModel):
    tasklist_id: str = Field("@default", description="Task list id, or '@default'.")
    task_id: str = Field(..., description="Task id to delete.")


def create_task(args: CreateTaskInput, ctx: CallContext, session: Session) -> dict[str, Any]:
    service = tasks_service(session, ctx)
    body: dict[str, Any] = {"title": args.title}
    if args.notes is not None:
        body["notes"] = args.notes
    if args.due is not None:
        body["due"] = args.due

    params: dict[str, Any] = {"tasklist": args.tasklist_id, "body": body}
    if args.parent is not None:
        params["parent"] = args.parent
    if args.previous is not None:
        params["previous"] = args.previous

    task = service.tasks().insert(**params).execute()
    return {**trim_task(task), "status": "created"}


def update_task(args: UpdateTaskInput, ctx: CallContext, session: Session) -> dict[str, Any]:
    service = tasks_service(session, ctx)
    body: dict[str, Any] = {}
    if args.title is not None:
        body["title"] = args.title
    if args.notes is not None:
        body["notes"] = args.notes
    if args.due is not None:
        body["due"] = args.due
    if args.status is not None:
        body["status"] = args.status

    task = (
        service.tasks()
        .patch(tasklist=args.tasklist_id, task=args.task_id, body=body)
        .execute()
    )
    return {**trim_task(task), "status": "updated"}


def complete_task(
    args: CompleteTaskInput, ctx: CallContext, session: Session
) -> dict[str, Any]:
    service = tasks_service(session, ctx)
    task = (
        service.tasks()
        .patch(tasklist=args.tasklist_id, task=args.task_id, body={"status": "completed"})
        .execute()
    )
    return {**trim_task(task), "status": "completed"}


def delete_task(args: DeleteTaskInput, ctx: CallContext, session: Session) -> dict[str, Any]:
    service = tasks_service(session, ctx)
    service.tasks().delete(tasklist=args.tasklist_id, task=args.task_id).execute()
    return {"status": "deleted", "tasklist_id": args.tasklist_id, "task_id": args.task_id}


def _create_preview(args: CreateTaskInput) -> str:
    return f"Create task '{args.title}' in task list '{args.tasklist_id}'."


def _update_preview(args: UpdateTaskInput) -> str:
    return f"Update task '{args.task_id}' in task list '{args.tasklist_id}'."


def _complete_preview(args: CompleteTaskInput) -> str:
    return f"Complete task '{args.task_id}' in task list '{args.tasklist_id}'."


def _delete_preview(args: DeleteTaskInput) -> str:
    return f"Delete task '{args.task_id}' from task list '{args.tasklist_id}'."


def register(registry) -> None:
    """Register Google Tasks mutating tools with the tool registry."""
    registry.add(
        ToolSpec(
            name="google_tasks_create_task",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            description=(
                "Create a Google Tasks task. Requires confirmation. Google Tasks due "
                "dates only preserve date information; due times are discarded."
            ),
            input_model=CreateTaskInput,
            handler=create_task,
            preview_builder=_create_preview,
        )
    )
    registry.add(
        ToolSpec(
            name="google_tasks_update_task",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            description="Update fields of a Google Tasks task. Requires confirmation.",
            input_model=UpdateTaskInput,
            handler=update_task,
            preview_builder=_update_preview,
        )
    )
    registry.add(
        ToolSpec(
            name="google_tasks_complete_task",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            description="Mark a Google Tasks task completed. Requires confirmation.",
            input_model=CompleteTaskInput,
            handler=complete_task,
            preview_builder=_complete_preview,
        )
    )
    registry.add(
        ToolSpec(
            name="google_tasks_delete_task",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            description="Delete a Google Tasks task. Requires confirmation.",
            input_model=DeleteTaskInput,
            handler=delete_task,
            preview_builder=_delete_preview,
        )
    )
