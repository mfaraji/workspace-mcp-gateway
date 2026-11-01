"""Shared plumbing for recipe tools: repository access and error mapping."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from gateway.config import get_settings
from gateway.providers.recipes.models import DOC_TYPE
from gateway.providers.registry import ToolError
from gateway.storage.capella import errors as storage_errors_mod
from gateway.storage.capella.client import get_client
from gateway.storage.capella.repository import OwnedDocumentRepository

PROVIDER = "recipes"

NOT_FOUND_MESSAGE = "Recipe not found."


def get_repository() -> OwnedDocumentRepository:
    """Owner-scoped repository over ``<bucket>.<recipes scope>.<recipes collection>``."""
    settings = get_settings()
    return OwnedDocumentRepository(
        get_client(settings),
        scope=settings.capella_recipes_scope,
        collection=settings.capella_recipes_collection,
        doc_type=DOC_TYPE,
    )


@contextmanager
def storage_errors(not_found: str = NOT_FOUND_MESSAGE) -> Iterator[None]:
    """Re-raise storage errors as ``ToolError`` with stable codes and safe messages."""
    try:
        yield
    except storage_errors_mod.DocumentNotFound:
        raise ToolError("not_found", not_found) from None
    except storage_errors_mod.StorageError as exc:
        raise ToolError(exc.error_code, str(exc)) from None


def classify_storage_error(exc: Exception) -> str | None:
    """Connector error classifier: fallback for storage errors escaping a handler."""
    if isinstance(exc, storage_errors_mod.StorageError):
        return exc.error_code
    try:
        from couchbase.exceptions import CouchbaseException
    except ImportError:  # pragma: no cover
        return None
    if isinstance(exc, CouchbaseException):
        from gateway.storage.capella.client import translate_exception

        return translate_exception(exc).error_code
    return None
