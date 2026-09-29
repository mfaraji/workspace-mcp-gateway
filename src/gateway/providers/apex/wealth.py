"""Apex wealth read-only tools: accounts, cash, investments, positions, net worth."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gateway.config import get_settings
from gateway.providers.apex.client import APEX_WEALTH_API_KEY, WEALTH_PROVIDER, apex_request
from gateway.providers.base import CallContext, RiskLevel, ToolSpec

_AMOUNTS = "Amounts are in the account's native currency; summaries also include CAD totals."


class NoInput(BaseModel):
    pass


class GetPositionsInput(BaseModel):
    account_id: str | None = Field(None, description="Restrict to one account, by account id.")
    as_of_date: date | None = Field(
        None, description="Snapshot date (YYYY-MM-DD). Defaults to the latest snapshot."
    )


class ListManualHoldingsInput(BaseModel):
    account_id: str | None = Field(None, description="Restrict to one account, by account id.")


class GetNetWorthHistoryInput(BaseModel):
    months: int = Field(12, ge=1, le=60, description="Number of months of history (1-60).")


def _get(ctx: CallContext, session: Session, path: str, params: dict[str, Any] | None = None):
    settings = get_settings()
    api_key = APEX_WEALTH_API_KEY.get_credentials(session, ctx, settings)
    return apex_request(settings, api_key, "GET", path, params=params or None)


def list_accounts(_args: NoInput, ctx: CallContext, session: Session) -> Any:
    return _get(ctx, session, "/api/wealth/accounts")


def get_accounts_summary(_args: NoInput, ctx: CallContext, session: Session) -> Any:
    return _get(ctx, session, "/api/wealth/accounts-summary")


def get_investments_summary(_args: NoInput, ctx: CallContext, session: Session) -> Any:
    return _get(ctx, session, "/api/wealth/investments")


def get_positions(args: GetPositionsInput, ctx: CallContext, session: Session) -> Any:
    params = {
        "accountId": args.account_id,
        "asOfDate": args.as_of_date.isoformat() if args.as_of_date else None,
    }
    return _get(ctx, session, "/api/wealth/positions", {k: v for k, v in params.items() if v})


def list_manual_holdings(args: ListManualHoldingsInput, ctx: CallContext, session: Session) -> Any:
    params = {"accountId": args.account_id} if args.account_id else None
    return _get(ctx, session, "/api/wealth/manual-holdings", params)


def get_net_worth_history(args: GetNetWorthHistoryInput, ctx: CallContext, session: Session) -> Any:
    return _get(ctx, session, "/api/wealth/net-worth", {"months": args.months})


def register(registry) -> None:
    """Register Apex wealth read tools with the tool registry."""
    specs = [
        (
            "apex_wealth_list_accounts",
            "List Apex accounts with institution, registration, subtype, currency and current "
            f"balance (liabilities are negative). {_AMOUNTS}",
            NoInput,
            list_accounts,
        ),
        (
            "apex_wealth_get_accounts_summary",
            "Get the cash and liabilities summary: cash accounts, debts, and net liquid "
            f"position. {_AMOUNTS}",
            NoInput,
            get_accounts_summary,
        ),
        (
            "apex_wealth_get_investments_summary",
            "Get investments: per-account value, cash, gains, positions, consolidated "
            f"holdings and allocation. {_AMOUNTS}",
            NoInput,
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
    for name, description, input_model, handler in specs:
        registry.add(
            ToolSpec(
                name=name,
                provider=WEALTH_PROVIDER,
                risk=RiskLevel.READ,
                description=description,
                input_model=input_model,
                handler=handler,
            )
        )
