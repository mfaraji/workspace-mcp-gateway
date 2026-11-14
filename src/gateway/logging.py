"""Logging setup.

Plain stdlib logging is sufficient for V1. Audit records are written to the
database (see ``gateway.audit.log``), not emitted here, so this module only
configures human-readable operational logs.
"""

from __future__ import annotations

import logging
import os


class _NoQueryAccessLog(logging.Filter):
    """OAuth query strings can contain authorization codes and browser credentials."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, path, version, status = record.args
            if isinstance(path, str):
                record.args = (client, method, path.split("?", 1)[0], version, status)
        return True


def configure_logging() -> None:
    """Configure root logging once, honoring the ``LOG_LEVEL`` env var."""
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _NoQueryAccessLog) for f in access_logger.filters):
        access_logger.addFilter(_NoQueryAccessLog())
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )


def get_logger(name: str) -> logging.Logger:
    """Return a named logger."""
    return logging.getLogger(name)
