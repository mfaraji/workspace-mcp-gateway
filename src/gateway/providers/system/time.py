"""Current date/time tools for resolving relative user requests."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from gateway.providers.base import CallContext, RiskLevel, ToolSpec

PROVIDER = "system"


class GetCurrentTimeInput(BaseModel):
    time_zone: str = Field(
        "UTC",
        description=(
            "IANA timezone for the answer, such as 'America/Los_Angeles'. "
            "Use the user's timezone when known; otherwise use UTC."
        ),
    )

    @field_validator("time_zone")
    @classmethod
    def validate_time_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {value}") from exc
        return value


def get_current_time(
    args: GetCurrentTimeInput, _ctx: CallContext, _session: Session
) -> dict[str, Any]:
    """Return current date context in a model-friendly shape."""
    return _time_snapshot(datetime.now(ZoneInfo(args.time_zone)))


def _time_snapshot(now: datetime) -> dict[str, Any]:
    upcoming_days = []
    for offset in range(8):
        day = now.date() + timedelta(days=offset)
        upcoming_days.append(
            {
                "date": day.isoformat(),
                "weekday": day.strftime("%A"),
                "relative": "today" if offset == 0 else f"+{offset} days",
            }
        )

    return {
        "iso_datetime": now.isoformat(),
        "date": now.date().isoformat(),
        "weekday": now.strftime("%A"),
        "time_zone": str(now.tzinfo),
        "utc_offset": now.strftime("%z"),
        "upcoming_days": upcoming_days,
    }


def register(registry) -> None:
    """Register system tools with the tool registry."""
    registry.add(
        ToolSpec(
            name="system_get_current_time",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "Get the current date, weekday, timezone offset, and upcoming dates. "
                "Use this to resolve relative scheduling phrases like today, tomorrow, "
                "this Monday, or next Monday before asking the user for a date."
            ),
            input_model=GetCurrentTimeInput,
            handler=get_current_time,
        )
    )

