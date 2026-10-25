"""Finance read-only tools: exchange rates (default quote currency is IRR)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gateway.providers.base import CallContext, RiskLevel, ToolSpec
from gateway.providers.finance import client

PROVIDER = "finance"

_EXCLUSION_FLAGS = (
    ("isOutlier", "outlier"),
    ("isExpired", "expired"),
    ("isFaulty", "faulty"),
    ("isLowPriority", "low priority"),
)


class CurrentRateInput(BaseModel):
    from_currency: str = Field(
        ..., alias="from", description="Currency code to convert from, e.g. 'USD' or 'CAD'."
    )
    to: str = Field("IRR", description="Currency code to convert to. Defaults to 'IRR'.")

    model_config = {"populate_by_name": True}


class RateChangeInput(CurrentRateInput):
    window: Literal["24h", "daily"] = Field(
        "24h",
        description=(
            "'24h' compares the latest hourly point with the one 24 hours earlier; "
            "'daily' compares the last two daily points."
        ),
    )


class RateHistoryInput(CurrentRateInput):
    view: Literal["1W", "all"] = Field(
        "1W", description="'1W' is hourly for the last 7 days; 'all' is daily since 2018-09-01."
    )
    limit: int = Field(
        30, ge=1, le=5000, description="For view='all' only: return the last N days."
    )


class ListCurrenciesInput(BaseModel):
    pass


def _pair(args: CurrentRateInput) -> tuple[str, int, int]:
    from_id = client.resolve_currency(args.from_currency)
    to_id = client.resolve_currency(args.to)
    return f"{args.from_currency.upper()}/{args.to.upper()}", from_id, to_id


def _num(value: Any) -> float | None:
    return None if value is None else float(value)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def get_current_rate(args: CurrentRateInput, _ctx: CallContext, _session: Session) -> Any:
    pair, from_id, to_id = _pair(args)
    data = client.get_sources(from_id, to_id)
    rows = data.get("directRateRows") or []
    if not data.get("hasMultiSources") or not rows:
        return {"pair": pair, "noData": True}

    sources = [
        {
            "sourceTitle": r.get("sourceTitle"),
            "buy": _num(r.get("buy")),
            "sell": _num(r.get("sell")),
            "thisToBase": _num(r.get("thisToBase")),
            "dateTime": r.get("dateTime"),
        }
        for r in rows
    ]
    excluded = []
    for r in data.get("invalids") or []:
        reasons = [label for flag, label in _EXCLUSION_FLAGS if r.get(flag)]
        excluded.append({"sourceTitle": r.get("sourceTitle"), "reason": ", ".join(reasons) or None})

    def field(key: str) -> list[float]:
        return [s[key] for s in sources if s[key] is not None]

    return {
        "pair": pair,
        "averageBuy": _mean(field("buy")),
        "averageSell": _mean(field("sell")),
        "averageRate": _mean(field("thisToBase")),
        "rowsUsed": len(sources),
        "sources": sources,
        "excluded": excluded,
    }


def get_rate_change(args: RateChangeInput, _ctx: CallContext, _session: Session) -> Any:
    pair, from_id, to_id = _pair(args)
    view, back = ("1W", 25) if args.window == "24h" else ("all", 2)
    data = client.get_rates(from_id, to_id, view)
    rates, dates = data.get("rates") or [], data.get("dates") or []
    if len(rates) < back:
        return {"pair": pair, "noData": True}
    current, previous = float(rates[-1]), float(rates[-back])
    change = current - previous
    return {
        "pair": pair,
        "window": args.window,
        "current": current,
        "previous": previous,
        "previousDate": dates[-back] if len(dates) >= back else None,
        "change": change,
        "changePercent": change / previous * 100 if previous else None,
        "note": (
            "Average rate only (no buy/sell history). The daily series can differ "
            "from the hourly one."
        ),
    }


def get_rate_history(args: RateHistoryInput, _ctx: CallContext, _session: Session) -> Any:
    pair, from_id, to_id = _pair(args)
    data = client.get_rates(from_id, to_id, args.view)
    points = [
        {"date": d, "rate": float(r)}
        for d, r in zip(data.get("dates") or [], data.get("rates") or [], strict=False)
    ]
    if args.view == "all":
        points = points[-args.limit :]
    return {"pair": pair, "view": args.view, "points": points}


def list_currencies(_args: ListCurrenciesInput, _ctx: CallContext, _session: Session) -> Any:
    return client.list_currencies()


def register(registry) -> None:
    """Register finance read tools with the tool registry."""
    registry.add(
        ToolSpec(
            name="finance_get_current_rate",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "Get the current exchange rate for a currency pair (default quote IRR, the "
                "Iranian rial): average buy, sell and mid rate across valid sources, the "
                "per-source quotes, and any excluded sources with reasons. Units are units "
                "of the quote currency per 1 unit of the base."
            ),
            input_model=CurrentRateInput,
            handler=get_current_rate,
        )
    )
    registry.add(
        ToolSpec(
            name="finance_get_rate_change",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "Get how the average exchange rate changed over the last 24 hours (hourly "
                "series) or the last day (daily series): current, previous, absolute and "
                "percent change. Covers the average rate only."
            ),
            input_model=RateChangeInput,
            handler=get_rate_change,
        )
    )
    registry.add(
        ToolSpec(
            name="finance_get_rate_history",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "Get average exchange-rate history: hourly for the last 7 days (view='1W') "
                "or daily back to 2018 (view='all', last `limit` days). Average rate only."
            ),
            input_model=RateHistoryInput,
            handler=get_rate_history,
        )
    )
    registry.add(
        ToolSpec(
            name="finance_list_currencies",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description="List supported currencies (id, abbreviation, title).",
            input_model=ListCurrenciesInput,
            handler=list_currencies,
        )
    )
