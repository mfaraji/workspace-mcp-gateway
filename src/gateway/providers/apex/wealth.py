"""Apex wealth read-only tools: accounts, cash, investments, positions, net worth."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gateway.config import get_settings
from gateway.providers.apex.client import (
    CAP_WEALTH_WORKSPACE,
    SCOPE_WEALTH_READ,
    WEALTH_PROVIDER,
    WEALTH_WORKSPACE_ID_DESC,
    apex_call,
    apex_credentials,
)
from gateway.providers.apex.ui import WEALTH_NOTE, WEALTH_UI
from gateway.providers.base import CallContext, RiskLevel, ToolSpec

_AMOUNTS = "Amounts are in the account's native currency; summaries also include CAD totals."


class WorkspaceInput(BaseModel):
    workspace_id: str = Field(..., description=WEALTH_WORKSPACE_ID_DESC)


class GetPositionsInput(WorkspaceInput):
    account_id: str | None = Field(None, description="Restrict to one account, by account id.")
    as_of_date: date | None = Field(
        None, description="Snapshot date (YYYY-MM-DD). Defaults to the latest snapshot."
    )


class ListManualHoldingsInput(WorkspaceInput):
    account_id: str | None = Field(None, description="Restrict to one account, by account id.")


class GetNetWorthHistoryInput(WorkspaceInput):
    months: int = Field(12, ge=1, le=60, description="Number of months of history (1-60).")


def _get(
    ctx: CallContext,
    workspace_id: str,
    capability: str,
    path: str,
    params: dict[str, Any] | None = None,
):
    settings = get_settings()
    creds = apex_credentials(
        settings,
        ctx,
        scopes=[SCOPE_WEALTH_READ],
        workspace_id=workspace_id,
        capability=capability,
        list_tool="apex_wealth_list_workspaces",
    )
    return apex_call(settings, creds, "GET", path, params=params or None)


def list_accounts(args: WorkspaceInput, ctx: CallContext, session: Session) -> Any:
    return _get(ctx, args.workspace_id, CAP_WEALTH_WORKSPACE, "/api/wealth/accounts")


def get_accounts_summary(args: WorkspaceInput, ctx: CallContext, session: Session) -> Any:
    return _get(ctx, args.workspace_id, "assets", "/api/wealth/accounts-summary")


def get_investments_summary(args: WorkspaceInput, ctx: CallContext, session: Session) -> Any:
    return _get(ctx, args.workspace_id, "assets", "/api/wealth/investments")


def get_positions(args: GetPositionsInput, ctx: CallContext, session: Session) -> Any:
    params = {
        "accountId": args.account_id,
        "asOfDate": args.as_of_date.isoformat() if args.as_of_date else None,
    }
    return _get(
        ctx,
        args.workspace_id,
        "investments",
        "/api/wealth/positions",
        {k: v for k, v in params.items() if v},
    )


def list_manual_holdings(args: ListManualHoldingsInput, ctx: CallContext, session: Session) -> Any:
    params = {"accountId": args.account_id} if args.account_id else None
    return _get(
        ctx, args.workspace_id, "investments", "/api/wealth/manual-holdings", params
    )


def get_net_worth_history(args: GetNetWorthHistoryInput, ctx: CallContext, session: Session) -> Any:
    return _get(
        ctx,
        args.workspace_id,
        CAP_WEALTH_WORKSPACE,
        "/api/wealth/net-worth",
        {"months": args.months},
    )


# Manual holdings are not drawn by the widget.
_NO_WIDGET = {"apex_wealth_list_manual_holdings"}


def register(registry) -> None:
    """Register Apex wealth read tools with the tool registry."""
    specs = [
        (
            "apex_wealth_list_accounts",
            "List Apex accounts with institution, registration, subtype, currency and current "
            f"balance (liabilities are negative). {_AMOUNTS}",
            WorkspaceInput,
            list_accounts,
        ),
        (
            "apex_wealth_get_accounts_summary",
            "Get the cash and liabilities summary: cash accounts, debts, and net liquid "
            f"position. {_AMOUNTS}",
            WorkspaceInput,
            get_accounts_summary,
        ),
        (
            "apex_wealth_get_investments_summary",
            "Get investments: per-account value, cash, gains, positions, consolidated "
            f"holdings and allocation. {_AMOUNTS}",
            WorkspaceInput,
            get_investments_summary,
        ),
        (
            "apex_wealth_get_positions",
            "Get investment positions (symbol, quantity, market value, gain) plus an "
            "aggregate summary, optionally for one account or a snapshot date. "
            f"{_AMOUNTS}",
            GetPositionsInput,
            get_positions,
        ),
        (
            "apex_wealth_list_manual_holdings",
            f"List manually tracked holdings, optionally for one account. {_AMOUNTS}",
            ListManualHoldingsInput,
            list_manual_holdings,
        ),
        (
            "apex_wealth_get_net_worth_history",
            "Get the monthly net-worth series (total assets, liabilities, net worth) for "
            "the last N months, in CAD.",
            GetNetWorthHistoryInput,
            get_net_worth_history,
        ),
    ]
    capabilities = {
        "apex_wealth_list_accounts": CAP_WEALTH_WORKSPACE,
        "apex_wealth_get_accounts_summary": "assets",
        "apex_wealth_get_investments_summary": "assets",
        "apex_wealth_get_positions": "investments",
        "apex_wealth_list_manual_holdings": "investments",
        "apex_wealth_get_net_worth_history": CAP_WEALTH_WORKSPACE,
    }
    for name, description, input_model, handler in specs:
        renders = name not in _NO_WIDGET
        registry.add(
            ToolSpec(
                name=name,
                provider=WEALTH_PROVIDER,
                risk=RiskLevel.READ,
                description=f"{description} {WEALTH_NOTE}" if renders else description,
                input_model=input_model,
                handler=handler,
                required_capability=capabilities[name],
                ui_resource=WEALTH_UI if renders else None,
                ui_app_callable=True,
            )
        )
