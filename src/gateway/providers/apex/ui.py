"""MCP Apps wiring shared by the Apex invoicing and Apex Wealth tools."""

from __future__ import annotations

BILLING_UI = "ui://apex/billing"
WEALTH_UI = "ui://wealth/overview"

BILLING_NOTE = (
    "Shows an interactive Apex billing view in the chat (clients, timesheets, a timesheet "
    "and invoice editor): do not repeat its contents in text; summarise or answer follow-ups "
    "instead."
)
WEALTH_NOTE = (
    "Shows an interactive Apex Wealth view in the chat (accounts, positions, net-worth "
    "chart): do not repeat its contents in text; summarise or answer follow-ups instead."
)
