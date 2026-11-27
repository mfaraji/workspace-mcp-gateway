"""Apex transaction questions (read-only): how much was spent on X, and the rows behind it.

These tools wrap the Apex transactions query REST API
(``docs/api/transactions-query.openapi.yaml`` in the apex repo). Apex does no
natural-language parsing: the model turns the user's words, in any language, into the
typed filters below, and every response echoes how Apex interpreted them
(``interpretation``). The usual flow is ``find_categories`` -> ``summarize`` (and
``search`` for the rows behind a number); ``find_merchants`` covers payees that are
uncategorised or miscategorised, and ``list_properties`` is for rental workspaces.

One tool set serves PERSONAL, BUSINESS and RENTAL workspaces; Apex answers from the
right books for each (the response says which amount basis it used).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from gateway.config import get_settings
from gateway.providers.apex.client import (
    CAP_RENTAL_BOOKS,
    CAP_TRANSACTIONS,
    SCOPE_TRANSACTIONS_READ,
    TRANSACTIONS_PROVIDER,
    apex_call,
    apex_credentials,
)
from gateway.providers.base import CallContext, RiskLevel, ToolSpec

WORKSPACE_ID_DESC = "Apex workspace id (from apex_transactions_list_workspaces)."

Period = Literal[
    "ytd",
    "this_month",
    "last_month",
    "this_quarter",
    "last_quarter",
    "this_year",
    "last_year",
    "last_30_days",
    "last_90_days",
    "last_12_months",
    "fiscal_ytd",
]
Direction = Literal["spending", "income", "net"]
GroupBy = Literal[
    "category",
    "parent_category",
    "merchant",
    "account",
    "month",
    "quarter",
    "year",
    "property",
    "t776_line",
    "treatment",
]
Compare = Literal["previous_period", "same_period_last_year"]
Treatment = Literal["INCOME", "CURRENT_EXPENSE", "CAPITAL", "PERSONAL_USE"]
T776Line = Literal[
    "GROSS_RENTS",
    "OTHER_RENTAL_INCOME",
    "ADVERTISING",
    "INSURANCE",
    "INTEREST_BANK_CHARGES",
    "OFFICE_EXPENSES",
    "PROFESSIONAL_FEES",
    "MANAGEMENT_ADMIN_FEES",
    "REPAIRS_MAINTENANCE",
    "SALARIES_WAGES",
    "PROPERTY_TAXES",
    "TRAVEL",
    "UTILITIES",
    "MOTOR_VEHICLE",
    "OTHER_EXPENSES",
]

# Tool-API group names are snake_case; the Apex query parameters are camelCase.
_GROUP_BY_API = {"parent_category": "parentCategory", "t776_line": "t776Line"}

_AMOUNTS = (
    "Spending comes back positive and net of refunds, converted to CAD (native totals are in "
    "totals.byCurrency). A BUSINESS workspace reports the business share of each expense "
    "(fullAmount is the whole transaction) and only has expenses. A RENTAL workspace reports "
    "allocated slices; capital spend and personal-use slices are never in the totals and "
    "show up under leftOut."
)
_ANSWERING = (
    "When you answer, state interpretation.period (and the categories used) so the user "
    "knows what was counted, mention leftOut when it is non-empty, and report amounts "
    "exactly as returned; do not recompute them."
)


class WorkspaceInput(BaseModel):
    workspace_id: str = Field(..., description=WORKSPACE_ID_DESC)


class FindCategoriesInput(WorkspaceInput):
    query: str | None = Field(
        None,
        max_length=100,
        description=(
            "Words to look for in the category name or path, in ENGLISH (translate the "
            "user's words first, e.g. 'factures d'eau' -> 'water'). Omit to list every category."
        ),
    )


class TransactionFilterInput(WorkspaceInput):
    """Filters shared by summarize, search and find_merchants."""

    period: Period | None = Field(
        None,
        description=(
            "Preset window; defaults to ytd (this year so far). 'last_month', 'last_quarter' "
            "and 'last_year' are the full previous calendar period; 'last_30_days' etc. end "
            "today; 'fiscal_ytd' is for BUSINESS workspaces. Ignored when from_date is given."
        ),
    )
    from_date: date | None = Field(
        None, description="Inclusive start date (YYYY-MM-DD). Overrides period."
    )
    to_date: date | None = Field(
        None, description="Inclusive end date (YYYY-MM-DD); defaults to today. Needs from_date."
    )
    category_ids: list[str] | None = Field(
        None,
        max_length=20,
        description=(
            "Category ids from apex_transactions_find_categories. Subcategories are included "
            "(a parent covers its children)."
        ),
    )
    exclude_category_ids: list[str] | None = Field(
        None, max_length=20, description="Category ids to subtract, with their subcategories."
    )
    include_subcategories: bool | None = Field(
        None, description="Default true. False keeps to exactly the given categories."
    )
    merchants: list[str] | None = Field(
        None,
        max_length=10,
        description=(
            "Payee text to match (case-insensitive 'contains' on the merchant, or the bank "
            "description when there is no merchant). Several values match any of them."
        ),
    )
    text: str | None = Field(
        None, max_length=100, description="Contains match on merchant or description."
    )
    account_ids: list[str] | None = Field(
        None, max_length=20, description="Limit to these account ids."
    )
    tags: list[str] | None = Field(None, max_length=10, description="Limit to these tags.")
    direction: Direction | None = Field(
        None,
        description=(
            "spending (default; positive, net of refunds), income, or net (income minus "
            "spending). A BUSINESS workspace only has spending."
        ),
    )
    min_amount: float | None = Field(
        None, ge=0, description="Smallest transaction size, on the absolute amount."
    )
    max_amount: float | None = Field(
        None, ge=0, description="Largest transaction size, on the absolute amount."
    )
    include_excluded_from_budget: bool | None = Field(
        None, description="Also count transactions marked 'exclude from budget' (PERSONAL)."
    )
    include_hidden: bool | None = Field(
        None, description="Also count transactions hidden from Personal (PERSONAL)."
    )
    include_transfers: bool | None = Field(
        None, description="Also count transfers between accounts (PERSONAL)."
    )
    property_ids: list[str] | None = Field(
        None,
        max_length=20,
        description="RENTAL only: property ids from apex_transactions_list_properties.",
    )
    t776_lines: list[T776Line] | None = Field(
        None,
        max_length=15,
        description="RENTAL only: CRA T776 lines, e.g. REPAIRS_MAINTENANCE, UTILITIES.",
    )
    treatments: list[Treatment] | None = Field(
        None,
        max_length=4,
        description=(
            "RENTAL only: which allocation treatments to read, overriding the direction. Use "
            "CAPITAL or PERSONAL_USE to see what the totals leave out."
        ),
    )

    @model_validator(mode="after")
    def _check_dates(self) -> TransactionFilterInput:
        if self.to_date and not self.from_date:
            raise ValueError("to_date needs from_date")
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("from_date must not be after to_date")
        return self


class FindMerchantsInput(TransactionFilterInput):
    query: str | None = Field(
        None, max_length=100, description="Payee text to search for (e.g. 'toronto water')."
    )
    limit: int = Field(20, ge=1, le=50, description="How many payees to return (1-50).")


class SummarizeInput(TransactionFilterInput):
    group_by: list[GroupBy] | None = Field(
        None,
        max_length=2,
        description=(
            "Break the total down by up to two of: category, parent_category, merchant, "
            "account, month, quarter, year (and, in RENTAL workspaces, property, t776_line, "
            "treatment). Time groups come back in order; others largest first."
        ),
    )
    compare: Compare | None = Field(
        None,
        description=(
            "Also total an earlier window and report the change: previous_period (the equal "
            "window just before) or same_period_last_year."
        ),
    )
    limit: int = Field(
        25,
        ge=1,
        le=100,
        description="Most groups to return (not for time groups); the rest fold into 'other'.",
    )


class SearchInput(TransactionFilterInput):
    sort: Literal["date", "amount"] = Field(
        "date", description="date = newest first; amount = largest first."
    )
    limit: int = Field(25, ge=1, le=100, description="Rows per page (1-100).")
    cursor: str | None = Field(
        None,
        max_length=200,
        description="nextCursor from the previous page, used with the same filters and sort.",
    )


def _filter_params(args: TransactionFilterInput) -> dict[str, Any]:
    params: dict[str, Any] = {
        "period": args.period,
        "from": args.from_date.isoformat() if args.from_date else None,
        "to": args.to_date.isoformat() if args.to_date else None,
        "categoryId": args.category_ids,
        "excludeCategoryId": args.exclude_category_ids,
        "includeSubcategories": args.include_subcategories,
        "merchant": args.merchants,
        "text": args.text,
        "accountId": args.account_ids,
        "tag": args.tags,
        "direction": args.direction,
        "minAmount": args.min_amount,
        "maxAmount": args.max_amount,
        "includeExcludedFromBudget": args.include_excluded_from_budget,
        "includeHidden": args.include_hidden,
        "includeTransfers": args.include_transfers,
        "assetId": args.property_ids,
        "t776Line": args.t776_lines,
        "treatment": args.treatments,
    }
    return _compact(params)


def _compact(params: dict[str, Any]) -> dict[str, Any]:
    """Drop unset values (False and 0 are real values and stay)."""
    return {k: v for k, v in params.items() if v is not None and v != []}


def _get(
    ctx: CallContext,
    workspace_id: str,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    capability: str = CAP_TRANSACTIONS,
) -> Any:
    settings = get_settings()
    creds = apex_credentials(
        settings,
        ctx,
        scopes=[SCOPE_TRANSACTIONS_READ],
        workspace_id=workspace_id,
        capability=capability,
    )
    return apex_call(settings, creds, "GET", path, params=params or None)


def find_categories(args: FindCategoriesInput, ctx: CallContext, session: Session) -> Any:
    params = _compact({"q": args.query.strip() if args.query else None})
    return _get(ctx, args.workspace_id, "/api/transactions/categories", params)


def list_properties(args: WorkspaceInput, ctx: CallContext, session: Session) -> Any:
    return _get(
        ctx,
        args.workspace_id,
        "/api/transactions/properties",
        capability=CAP_RENTAL_BOOKS,
    )


def find_merchants(args: FindMerchantsInput, ctx: CallContext, session: Session) -> Any:
    params = _filter_params(args)
    params.update(_compact({"q": args.query.strip() if args.query else None}))
    params["limit"] = args.limit
    return _get(ctx, args.workspace_id, "/api/transactions/merchants", params)


def summarize(args: SummarizeInput, ctx: CallContext, session: Session) -> Any:
    params = _filter_params(args)
    params.update(
        _compact(
            {
                "groupBy": [_GROUP_BY_API.get(g, g) for g in args.group_by]
                if args.group_by
                else None,
                "compare": args.compare,
            }
        )
    )
    params["limit"] = args.limit
    return _get(ctx, args.workspace_id, "/api/transactions/summary", params)


def search(args: SearchInput, ctx: CallContext, session: Session) -> Any:
    params = _filter_params(args)
    params.update(_compact({"cursor": args.cursor}))
    params["sort"] = args.sort
    params["limit"] = args.limit
    return _get(ctx, args.workspace_id, "/api/transactions/search", params)


def register(registry) -> None:
    """Register the Apex transaction query tools with the tool registry."""
    specs = [
        (
            "apex_transactions_find_categories",
            (
                "Find Apex category ids by name. Call this FIRST for any question about "
                "spending on something ('water bills', 'clothing', 'groceries'): categories "
                "are named in English, so translate the user's words (any language) to English "
                "keywords, then pass the returned id as category_ids to "
                "apex_transactions_summarize or apex_transactions_search. Each result has id, "
                "path (e.g. 'Bills & Utilities > Water'), group (SPENDING, INCOME, TRANSFER, "
                "INVESTMENT or RENTAL) and a recent transaction count. When several categories "
                "match, prefer the most specific path the user means, or use more than one; "
                "when none fit, use apex_transactions_find_merchants instead."
            ),
            FindCategoriesInput,
            find_categories,
            CAP_TRANSACTIONS,
        ),
        (
            "apex_transactions_list_properties",
            (
                "List the rental properties of a RENTAL workspace (id, name, address, status). "
                "Use the id as property_ids to ask about one property, or group_by "
                "property to compare them. Sold properties are included so past years can be "
                "queried. Only for RENTAL workspaces."
            ),
            WorkspaceInput,
            list_properties,
            CAP_RENTAL_BOOKS,
        ),
        (
            "apex_transactions_find_merchants",
            (
                "Find payees and what they total, largest first (merchant, count, total, top "
                "category, first and last date). Use it when the category is unknown or the "
                "spending may be uncategorised ('how much at Toronto Water?'); then pass the "
                "merchant text as merchants to apex_transactions_summarize. Takes the same "
                "filters as summarize. " + _ANSWERING
            ),
            FindMerchantsInput,
            find_merchants,
            CAP_TRANSACTIONS,
        ),
        (
            "apex_transactions_summarize",
            (
                "Answer 'how much' for any filter: total, count and optional breakdown. With "
                "only workspace_id it is total spending year to date. Typical call: category_ids "
                "from apex_transactions_find_categories plus a period; add group_by=['month'] "
                "for a trend or compare='same_period_last_year' for a year-over-year change. "
                + _AMOUNTS
                + " "
                + _ANSWERING
                + " Default exclusions (transfers, investment moves, income, budget-excluded "
                "and hidden transactions in PERSONAL) are listed in interpretation.excluded and "
                "their size in leftOut. The Apex query API does the arithmetic; never add up "
                "rows yourself."
            ),
            SummarizeInput,
            summarize,
            CAP_TRANSACTIONS,
        ),
        (
            "apex_transactions_search",
            (
                "List the individual transactions behind a number, with the same filters as "
                "apex_transactions_summarize (so the rows are exactly what it summed): date, "
                "signed amount (purchases are negative), currency, merchant, category path, "
                "account, and for BUSINESS the business share and deductible amount, for RENTAL "
                "the property, treatment and T776 line. Page with cursor/nextCursor. Use it to "
                "show or verify what makes up a total, not to compute one."
            ),
            SearchInput,
            search,
            CAP_TRANSACTIONS,
        ),
    ]
    for name, description, input_model, handler, capability in specs:
        registry.add(
            ToolSpec(
                name=name,
                provider=TRANSACTIONS_PROVIDER,
                risk=RiskLevel.READ,
                description=description,
                input_model=input_model,
                handler=handler,
                required_capability=capability,
            )
        )
