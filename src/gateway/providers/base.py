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


@dataclass(frozen=True)
class CallContext:
    """Resolved per-call context handed to every tool handler."""

    user_id: uuid.UUID
    external_user_id: str
    request_id: str


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
