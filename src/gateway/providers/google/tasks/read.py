"""Google Tasks read tools."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gateway.providers.base import CallContext, RiskLevel, ToolSpec
from gateway.providers.google.tasks.common import (
    PROVIDER,
    tasks_service,
    trim_task,
    trim_tasklist,
)


class ListTasklistsInput(BaseModel):
    max_results: int = Field(100, ge=1, le=1000, description="Max task lists to return.")
    page_token: str | None = Field(None, description="Page token from a previous response.")


class ListTasksInput(BaseModel):
    tasklist_id: str = Field("@default", description="Task list id, or '@default'.")
    max_results: int = Field(25, ge=1, le=100, description="Max tasks to return.")
    page_token: str | None = Field(None, description="Page token from a previous response.")
    show_completed: bool = Field(False, description="Include completed tasks.")
    show_deleted: bool = Field(False, description="Include deleted tasks.")
    show_hidden: bool = Field(False, description="Include hidden tasks.")
    show_assigned: bool = Field(False, description="Include tasks assigned from Docs or Chat.")
    due_min: str | None = Field(None, description="RFC3339 lower bound for due date.")
    due_max: str | None = Field(None, description="RFC3339 upper bound for due date.")
    updated_min: str | None = Field(None, description="RFC3339 lower bound for update time.")


def list_tasklists(
    args: ListTasklistsInput, ctx: CallContext, session: Session
) -> dict[str, Any]:
    service = tasks_service(session, ctx)
    params: dict[str, Any] = {"maxResults": args.max_results}
    if args.page_token:
        params["pageToken"] = args.page_token

    resp = service.tasklists().list(**params).execute()
    return {
        "tasklists": [trim_tasklist(t) for t in resp.get("items", [])],
        "next_page_token": resp.get("nextPageToken"),
    }


def list_tasks(args: ListTasksInput, ctx: CallContext, session: Session) -> dict[str, Any]:
    service = tasks_service(session, ctx)
    params: dict[str, Any] = {
        "tasklist": args.tasklist_id,
        "maxResults": args.max_results,
        "showCompleted": args.show_completed,
        "showDeleted": args.show_deleted,
        "showHidden": args.show_hidden,
        "showAssigned": args.show_assigned,
    }
    if args.page_token:
        params["pageToken"] = args.page_token
    if args.due_min:
        params["dueMin"] = args.due_min
    if args.due_max:
        params["dueMax"] = args.due_max
    if args.updated_min:
        params["updatedMin"] = args.updated_min

    resp = service.tasks().list(**params).execute()
    return {
        "tasklist_id": args.tasklist_id,
        "tasks": [trim_task(t) for t in resp.get("items", [])],
        "next_page_token": resp.get("nextPageToken"),
    }


def register(registry) -> None:
    """Register Google Tasks read tools with the tool registry."""
    registry.add(
        ToolSpec(
            name="google_tasks_list_tasklists",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description="List the user's Google Tasks task lists.",
            input_model=ListTasklistsInput,
            handler=list_tasklists,
        )
    )
    registry.add(
        ToolSpec(
            name="google_tasks_list_tasks",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "List tasks in a Google Tasks task list. Google Tasks due dates only "
                "preserve date information; due times are not readable or writable."
            ),
            input_model=ListTasksInput,
            handler=list_tasks,
        )
    )

