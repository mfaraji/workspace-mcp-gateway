"""Apex read-only tools: clients and timesheets."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gateway.config import get_settings
from gateway.providers.apex.client import apex_request
from gateway.providers.base import CallContext, RiskLevel, ToolSpec

PROVIDER = "apex"


class ListClientsInput(BaseModel):
    pass


class ListTimesheetsInput(BaseModel):
    client_id: str | None = Field(
        None, description="Filter to one client's timesheets, by client UUID."
    )


class GetLastTimesheetInput(BaseModel):
    client_id: str = Field(..., description="Client UUID to look up the latest timesheet for.")


def list_clients(_args: ListClientsInput, _ctx: CallContext, _session: Session) -> Any:
    return apex_request(get_settings(), "GET", "/api/clients")


def list_timesheets(args: ListTimesheetsInput, _ctx: CallContext, _session: Session) -> Any:
    params = {"clientId": args.client_id} if args.client_id else None
    return apex_request(get_settings(), "GET", "/api/timesheets", params=params)


def get_last_timesheet(args: GetLastTimesheetInput, _ctx: CallContext, _session: Session) -> Any:
    return apex_request(
        get_settings(), "GET", "/api/timesheets/last", params={"clientId": args.client_id}
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
                "id and billing mode before creating an invoice."
            ),
            input_model=ListClientsInput,
            handler=list_clients,
        )
    )
    registry.add(
        ToolSpec(
            name="apex_list_timesheets",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description="List Apex timesheets, optionally filtered to one client.",
            input_model=ListTimesheetsInput,
            handler=list_timesheets,
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
                "period's end)."
            ),
            input_model=GetLastTimesheetInput,
            handler=get_last_timesheet,
        )
    )
