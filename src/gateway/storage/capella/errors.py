"""Backend-neutral storage exceptions with stable, credential-free error codes.

The Capella client translates Couchbase SDK exceptions into these types so
provider code never handles (or surfaces) raw SDK errors. Every message is a
fixed string: it never contains the connection string, credentials, document
keys, or document contents.
"""

from __future__ import annotations


class StorageError(Exception):
    """Base class; ``error_code`` is a stable code safe to return to MCP clients."""

    error_code = "storage_error"
    default_message = "document storage error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)


class StorageNotConfigured(StorageError):
    error_code = "storage_not_configured"
    default_message = "document storage (Couchbase Capella) is not configured on this gateway"


class StorageMisconfigured(StorageError):
    error_code = "storage_misconfigured"
    default_message = (
        "document storage is misconfigured (connection settings, credentials, "
        "keyspace, or index); ask the gateway operator to check the Capella setup"
    )


class StorageUnavailable(StorageError):
    error_code = "storage_unavailable"
    default_message = "document storage is temporarily unavailable; try again shortly"


class AmbiguousWrite(StorageError):
    """A write timed out and may or may not have been applied."""

    error_code = "storage_outcome_unknown"
    default_message = (
        "the write timed out and may or may not have been applied; "
        "re-read before retrying"
    )


class DocumentNotFound(StorageError):
    error_code = "not_found"
    default_message = "document not found"


class DocumentAlreadyExists(StorageError):
    error_code = "already_exists"
    default_message = "a document with this id already exists"


class VersionConflict(StorageError):
    error_code = "version_conflict"
    default_message = "the document was modified since it was read; re-read and retry"
