"""Tests for system date/time tools."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from gateway.providers.base import RiskLevel
from gateway.providers.registry import ToolRegistry
from gateway.providers.system import time


def _registered() -> dict[str, object]:
    registry = ToolRegistry()
    time.register(registry)
    return {spec.name: spec for spec in registry._specs}


def test_current_time_tool_is_registered_as_read_only():
    spec = _registered()["system_get_current_time"]
    assert spec.risk is RiskLevel.READ
    assert "relative scheduling phrases" in spec.description


def test_time_snapshot_includes_upcoming_weekday_context():
    now = datetime(2026, 5, 31, 9, 30, tzinfo=ZoneInfo("UTC"))

    result = time._time_snapshot(now)

    assert result["date"] == "2026-05-31"
    assert result["weekday"] == "Sunday"
    assert result["upcoming_days"][0] == {
        "date": "2026-05-31",
        "weekday": "Sunday",
        "relative": "today",
    }
    assert result["upcoming_days"][1] == {
        "date": "2026-06-01",
        "weekday": "Monday",
        "relative": "+1 days",
    }


def test_get_current_time_rejects_unknown_timezone():
    with pytest.raises(ValueError, match="unknown IANA timezone"):
        time.GetCurrentTimeInput(time_zone="not/a-zone")

