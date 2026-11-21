"""Apex read-only tools: clients and timesheets."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gateway.config import get_settings
from gateway.providers.apex.client import (
    SCOPE_CLIENTS_READ,
    SCOPE_INVOICING_READ,
    WORKSPACE_ID_DESC,
    apex_call,
    apex_credentials,
)
from gateway.providers.apex.ui import BILLING_NOTE, BILLING_UI
from gateway.providers.base import CallContext, RiskLevel, ToolSpec

PROVIDER = "apex"


class ListClientsInput(BaseModel):
    workspace_id: str = Field(..., description=WORKSPACE_ID_DESC)


class ListTimesheetsInput(BaseModel):
    workspace_id: str = Field(..., description=WORKSPACE_ID_DESC)
    client_id: str | None = Field(
        None, description="Filter to one client's timesheets, by client UUID."
    )


class GetLastTimesheetInput(BaseModel):
    workspace_id: str = Field(..., description=WORKSPACE_ID_DESC)
    client_id: str = Field(..., description="Client UUID to look up the latest timesheet for.")


def list_clients(args: ListClientsInput, ctx: CallContext, session: Session) -> Any:
    settings = get_settings()
    creds = apex_credentials(
        settings,
        ctx,
        scopes=[SCOPE_CLIENTS_READ],
        workspace_id=args.workspace_id,
        capability="clients",
    )
    return apex_call(settings, creds, "GET", "/api/clients")


def list_timesheets(args: ListTimesheetsInput, ctx: CallContext, session: Session) -> Any:
    settings = get_settings()
    creds = apex_credentials(
        settings,
        ctx,
        scopes=[SCOPE_INVOICING_READ],
        workspace_id=args.workspace_id,
        capability="invoicing",
    )
    params = {"clientId": args.client_id} if args.client_id else None
    return apex_call(settings, creds, "GET", "/api/timesheets", params=params)


def get_last_timesheet(args: GetLastTimesheetInput, ctx: CallContext, session: Session) -> Any:
    settings = get_settings()
    creds = apex_credentials(
        settings,
        ctx,
        scopes=[SCOPE_INVOICING_READ],
        workspace_id=args.workspace_id,
        capability="invoicing",
    )
    return apex_call(
        settings, creds, "GET", "/api/timesheets/last", params={"clientId": args.client_id}
    )


def register(registry) -> None:
    """Register Apex read tools with the tool registry."""
    registry.add(
        ToolSpec(
            name="apex_list_clients",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "List clients in the Apex workspace, including id, billing mode (DIRECT "
                "or TIMESHEET), hourly rate, and HST setting. Use this to find a client's "
                "id and billing mode before creating an invoice. " + BILLING_NOTE
            ),
            input_model=ListClientsInput,
            handler=list_clients,
            required_capability="clients",
            ui_resource=BILLING_UI,
            ui_app_callable=True,
        )
    )
    registry.add(
        ToolSpec(
            name="apex_list_timesheets",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description="List Apex timesheets, optionally filtered to one client. "
            + BILLING_NOTE,
            input_model=ListTimesheetsInput,
            handler=list_timesheets,
            required_capability="invoicing",
            ui_resource=BILLING_UI,
            ui_app_callable=True,
        )
    )
    registry.add(
        ToolSpec(
            name="apex_get_last_timesheet",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "Get the most recent timesheet for a client, by latest period end. Useful "
                "for defaulting the next billing period's start date (day after the last "
                "period's end). " + BILLING_NOTE
            ),
            input_model=GetLastTimesheetInput,
            handler=get_last_timesheet,
            required_capability="invoicing",
            ui_resource=BILLING_UI,
            ui_app_callable=True,
        )
    )
