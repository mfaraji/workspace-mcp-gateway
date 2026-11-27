"""Provider/tool abstractions shared across all provider modules.

A ``ToolSpec`` is the provider-agnostic description of one MCP tool. The registry
turns specs into registered MCP tools, wrapping each in a uniform pipeline
(identity -> policy -> execution -> audit). Read tools execute directly; the
seam for mutating tools' confirmation flow lives in :mod:`gateway.policy.confirm`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel
from sqlalchemy.orm import Session


class RiskLevel(StrEnum):
    """Whether a tool only reads or also mutates remote state."""

    READ = "read"
    MUTATING = "mutating"


class RiskTier(StrEnum):
    """How a mutating tool is gated. ``LOW`` runs directly; ``MEDIUM`` asks the user in chat."""

    LOW = "low"
    MEDIUM = "medium"


@dataclass(frozen=True)
class CallContext:
    """Resolved per-call context handed to every tool handler."""

    user_id: uuid.UUID
    external_user_id: str
    request_id: str
    chat_id: str | None = None
    message_id: str | None = None
    automation_id: str | None = None
    # Clerk subject of the caller, when known. Upstream strategies that act on
    # the user's behalf (Apex act-as) key only on this, not on how they signed in.
    clerk_subject: str | None = None
    # OAuth client the caller signed in with (recorded in gateway-minted downstream tokens).
    oauth_client_id: str | None = None
    # Which connected account (email) of a multi-account provider this call targets;
    # ``None`` means the product's default account. Set from the tool input's ``account``.
    account: str | None = None


# A handler receives validated input, the call context, and an open DB session,
# and returns a JSON-serializable result. Handlers are synchronous (blocking);
# the registry runs them in a worker thread.
ToolHandler = Callable[[BaseModel, CallContext, Session], object]

# Builds a short human-readable preview of a mutating call from its validated
# input, shown to the user in the confirmation step. Read tools leave it None.
PreviewBuilder = Callable[[BaseModel], str]


@dataclass(frozen=True)
class ToolSpec:
    """Provider-agnostic description of one MCP tool."""

    name: str
    provider: str
    risk: RiskLevel
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler
    preview_builder: PreviewBuilder | None = None
    requires_confirmation: bool | None = None
    # MCP Apps: ``ui_resource`` is the ``ui://`` widget that renders this tool's result.
    # ``ui_app_callable`` marks a tool a widget may call without a host-side modal (the
    # gateway's own preview/token handshake, shown inside the widget, is the confirmation).
    ui_resource: str | None = None
    ui_app_callable: bool = False
    # Upstream capability a workspace must have for the user to be permitted this tool
    # (Apex workspace capability, e.g. "invoicing"). None = no per-workspace requirement.
    required_capability: str | None = None
    # (other tool's internal name, sentence naming it). A sentence is appended to this tool's
    # description only on a mount that also serves that other tool, so guidance never points
    # at a tool the mount lacks. Write the sentence with internal names; mounts rewrite them.
    related_tools: tuple[tuple[str, str], ...] = ()

    @property
    def default_tier(self) -> RiskTier | None:
        """The built-in tier of a mutating tool (``None`` for read tools, which are not tunable)."""
        if self.risk is not RiskLevel.MUTATING:
            return None
        return RiskTier.LOW if self.requires_confirmation is False else RiskTier.MEDIUM

    @property
    def tool_meta(self) -> dict | None:
        """The MCP ``_meta`` block advertising this tool's widget, or None."""
        if not (self.ui_resource or self.ui_app_callable):
            return None
        ui: dict = {}
        if self.ui_resource:
            ui["resourceUri"] = self.ui_resource
        if self.ui_app_callable:
            ui["autoApprove"] = True
        meta: dict = {"ui": ui}
        if self.ui_resource:
            meta["ui/resourceUri"] = self.ui_resource  # legacy key used by older hosts
        return meta
