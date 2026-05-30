"""Logging setup.

Plain stdlib logging is sufficient for V1. Audit records are written to the
database (see ``gateway.audit.log``), not emitted here, so this module only
configures human-readable operational logs.
"""

from __future__ import annotations

import logging
import os


def configure_logging() -> None:
    """Configure root logging once, honoring the ``LOG_LEVEL`` env var."""
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )


def get_logger(name: str) -> logging.Logger:
    """Return a named logger."""
    return logging.getLogger(name)
