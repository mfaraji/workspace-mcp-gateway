"""The five Calendar MCP prompts (names match Google's Calendar MCP server).

Each prompt returns one user message telling the model how to use the Calendar tools,
which on the calendar mount have Google's bare names. They never create anything
themselves: the message tells the model to confirm with the user before ``create_event``.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

_CONFIRM = (
    "Before calling create_event, show the user the proposed title, date and time (with "
    "time zone), duration and attendees, and wait for their explicit yes. Use "
    "system_get_current_time to resolve relative dates such as 'tomorrow' or 'next Monday'; "
    "never infer today's date from existing events."
)


def schedule_meeting(
    title: str, attendees: str = "", duration_minutes: str = "30", timeframe: str = ""
) -> str:
    who = f" with {attendees}" if attendees else ""
    when = f" within {timeframe}" if timeframe else " in the next 7 days"
    return (
        f"Schedule a meeting titled '{title}'{who}, {duration_minutes} minutes long{when}.\n"
        "1. Call list_calendars if you need to choose a calendar; otherwise use 'primary'.\n"
        "2. Call suggest_time with the duration, the window and the attendees' email "
        "addresses to find free slots. Mention any attendee listed in `unavailable`, since "
        "their availability could not be checked.\n"
        "3. Offer the best two or three slots and let the user pick one.\n"
        f"4. {_CONFIRM}"
    )


def quick_meeting_today(title: str, attendees: str = "", duration_minutes: str = "30") -> str:
    who = f" with {attendees}" if attendees else ""
    return (
        f"Set up a quick '{title}'{who} today, {duration_minutes} minutes long.\n"
        "1. Call system_get_current_time for the current time and today's date.\n"
        "2. Call suggest_time with time_min set to now and time_max set to the end of "
        "today, using the attendees' email addresses. If nothing is free today, say so and "
        "offer the next business day instead.\n"
        f"3. {_CONFIRM}"
    )


def recurring_1on1(
    attendee: str, cadence: str = "weekly", duration_minutes: str = "30", start_after: str = ""
) -> str:
    after = f" starting after {start_after}" if start_after else ""
    return (
        f"Set up a {cadence} 1:1 with {attendee}, {duration_minutes} minutes long{after}.\n"
        "1. Call search_events with a query for the person's name or email to see whether a "
        "1:1 already exists, and tell the user if it does instead of creating a duplicate.\n"
        "2. Call suggest_time (with the attendee) over the next two weeks and propose a "
        "slot that works as a repeating time.\n"
        "3. Use list_events on the following weeks to check the same time stays free, and "
        "flag any conflicts.\n"
        f"4. {_CONFIRM} Ask how long the series should run before creating it."
    )


def team_meeting_scheduler(
    title: str, team_members: str, duration_minutes: str = "60", timeframe: str = ""
) -> str:
    when = f" within {timeframe}" if timeframe else " in the next 7 days"
    return (
        f"Find the best time for the team meeting '{title}' with {team_members}, "
        f"{duration_minutes} minutes long{when}.\n"
        "1. Call suggest_time with every team member's email address as an attendee and "
        "request several suggestions.\n"
        "2. List members whose calendars could not be read (`unavailable`) so the user can "
        "check with them directly.\n"
        "3. Recommend one slot and explain why, with one fallback.\n"
        f"4. {_CONFIRM}"
    )


def analyze_availability(timeframe: str = "this week", attendees: str = "") -> str:
    who = f" and for {attendees}" if attendees else ""
    return (
        f"Analyze my availability for {timeframe}{who}.\n"
        "1. Call system_get_current_time, then list_events for the period to see what is "
        "already booked.\n"
        "2. Call suggest_time over the same period (with a short duration such as 30 "
        "minutes and several suggestions) to find open blocks.\n"
        "3. Summarize busy and free time by day, point out the longest uninterrupted blocks "
        "and any back-to-back runs, and note anything that looks double-booked.\n"
        "This is read-only: do not create, change or delete any event."
    )


def register(mcp: FastMCP) -> None:
    """Register the Calendar prompts on a FastMCP server."""
    for fn, description in (
        (schedule_meeting, "Find a time and schedule a meeting with attendees."),
        (quick_meeting_today, "Set up a short meeting later today."),
        (recurring_1on1, "Set up a recurring 1:1 meeting with one person."),
        (team_meeting_scheduler, "Find the best time for a team meeting."),
        (analyze_availability, "Summarize free and busy time over a period (read-only)."),
    ):
        mcp.prompt(name=fn.__name__, description=description)(fn)
