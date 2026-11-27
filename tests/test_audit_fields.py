"""Each audited input field has exactly one disposition: value, presence only, or dropped."""

from __future__ import annotations

from gateway.audit.log import summarize_input


def test_approved_field_is_recorded_once_by_value():
    summary = summarize_input(
        "google_calendar_suggest_time",
        {"time_min": "2030-01-01T09:00:00Z", "time_max": "2030-01-02T09:00:00Z"},
    )
    assert summary.count("time_min") == 1 and summary.count("time_max") == 1
    assert "time_min='2030-01-01T09:00:00Z'" in summary
    assert "<set>" not in summary


def test_presence_only_field_never_records_its_value():
    summary = summarize_input("google_calendar_list_events", {"time_min": "2030-01-01T09:00:00Z"})
    assert summary == "time_min=<set>"
    assert "2030" not in summary


def test_unlisted_free_text_is_dropped_and_query_is_presence_only():
    summary = summarize_input("google_calendar_search_events", {"query": "layoffs plan"})
    assert summary == "query=<set>"
    assert "layoffs" not in summary
