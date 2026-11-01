"""Couchbase Capella document storage shared by storage-backed providers.

Layout: one cluster, one bucket (``CAPELLA_BUCKET``), one scope per domain
(e.g. ``recipes``). Providers build an :class:`OwnedDocumentRepository` over
their collection; it enforces per-user isolation for every operation.
"""

from gateway.storage.capella.client import (
    CapellaClient,
    close_client,
    get_client,
    set_client,
)
from gateway.storage.capella.errors import StorageError
from gateway.storage.capella.repository import OwnedDocumentRepository

__all__ = [
    "CapellaClient",
    "OwnedDocumentRepository",
    "StorageError",
    "close_client",
    "get_client",
    "set_client",
]
