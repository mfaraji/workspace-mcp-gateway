"""Lazily-connected, process-wide Couchbase Capella client.

One ``Cluster`` connection is opened on first use and reused by every tool call
(the sync SDK's cluster object is thread-safe; tool handlers run in worker
threads). Nothing connects at import or startup, so the gateway boots and every
non-storage connector works when Capella is unconfigured or unreachable.

Safety rules enforced here:

* TLS only — the connection string must start with ``couchbases://``.
* No provisioning — buckets, scopes, collections and indexes are never created.
* SDK exceptions are translated into :mod:`gateway.storage.capella.errors`;
  only the exception *class name* is logged, never the connection string,
  credentials, keys, or document contents.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from gateway.config import Settings
from gateway.logging import get_logger
from gateway.storage.capella import errors

log = get_logger(__name__)

# After a failed connect, calls fail fast for this long instead of each one
# paying the full connect timeout.
CONNECT_RETRY_COOLDOWN_SECONDS = 5.0

ClusterFactory = Callable[[Settings], Any]


def _default_cluster_factory(settings: Settings) -> Any:
    """Open and wait for a real Capella cluster connection."""
    from couchbase.auth import PasswordAuthenticator
    from couchbase.cluster import Cluster
    from couchbase.options import ClusterOptions, ClusterTimeoutOptions

    assert settings.capella_password is not None  # guarded by capella_configured
    timeouts = ClusterTimeoutOptions(
        connect_timeout=timedelta(seconds=settings.capella_connect_timeout_seconds),
        kv_timeout=timedelta(seconds=settings.capella_kv_timeout_seconds),
        query_timeout=timedelta(seconds=settings.capella_query_timeout_seconds),
    )
    options = ClusterOptions(
        PasswordAuthenticator(
            settings.capella_username, settings.capella_password.get_secret_value()
        ),
        timeout_options=timeouts,
    )
    cluster = Cluster(settings.capella_connection_string, options)
    cluster.wait_until_ready(timedelta(seconds=settings.capella_connect_timeout_seconds))
    return cluster


def translate_exception(exc: BaseException, *, write: bool = False) -> errors.StorageError:
    """Map a Couchbase SDK exception to a stable, credential-free storage error."""
    if isinstance(exc, errors.StorageError):
        return exc
    try:
        from couchbase import exceptions as cbx
    except ImportError:  # pragma: no cover - dependency is required
        return errors.StorageUnavailable()

    if isinstance(exc, cbx.DocumentNotFoundException):
        return errors.DocumentNotFound()
    if isinstance(exc, cbx.DocumentExistsException):
        return errors.DocumentAlreadyExists()
    if isinstance(exc, cbx.CasMismatchException):
        return errors.VersionConflict()
    if isinstance(
        exc, (cbx.AmbiguousTimeoutException, cbx.DurabilitySyncWriteAmbiguousException)
    ):
        return errors.AmbiguousWrite() if write else errors.StorageUnavailable()
    if isinstance(
        exc,
        (
            cbx.AuthenticationException,
            cbx.BucketNotFoundException,
            cbx.ScopeNotFoundException,
            cbx.CollectionNotFoundException,
            cbx.KeyspaceNotFoundException,
            cbx.QueryIndexNotFoundException,
            cbx.ParsingFailedException,
            cbx.InvalidArgumentException,
        ),
    ):
        return errors.StorageMisconfigured()
    if isinstance(
        exc,
        (
            cbx.UnAmbiguousTimeoutException,
            cbx.TimeoutException,
            cbx.ServiceUnavailableException,
            cbx.RequestCanceledException,
            cbx.TemporaryFailException,
            cbx.RateLimitedException,
            cbx.QuotaLimitedException,
        ),
    ):
        return errors.StorageUnavailable()
    # "No index available on keyspace" (query error 4000) surfaces as a generic
    # query failure in some SDK versions.
    if isinstance(exc, cbx.CouchbaseException) and "no index available" in str(exc).lower():
        return errors.StorageMisconfigured()
    return errors.StorageError()


class CapellaClient:
    """Owns the single lazily-opened cluster connection and collection handles."""

    def __init__(
        self,
        settings: Settings,
        *,
        cluster_factory: ClusterFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._factory = cluster_factory or _default_cluster_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._cluster: Any = None
        self._collections: dict[tuple[str, str], Any] = {}
        self._failed_at: float | None = None
        self._failure: errors.StorageError | None = None

    @property
    def bucket_name(self) -> str:
        return self.settings.capella_bucket

    def _check_config(self) -> None:
        if not self.settings.capella_configured:
            raise errors.StorageNotConfigured()
        conn = (self.settings.capella_connection_string or "").strip()
        if not conn.lower().startswith("couchbases://"):
            # TLS is mandatory; never fall back to plaintext couchbase://.
            raise errors.StorageMisconfigured(
                "the Capella connection string must use TLS (couchbases://)"
            )

    def cluster(self) -> Any:
        """Return the shared cluster, connecting on first use."""
        if self._cluster is not None:
            return self._cluster
        self._check_config()
        with self._lock:
            if self._cluster is not None:
                return self._cluster
            if (
                self._failed_at is not None
                and self._clock() - self._failed_at < CONNECT_RETRY_COOLDOWN_SECONDS
            ):
                raise type(self._failure or errors.StorageUnavailable())()
            try:
                cluster = self._factory(self.settings)
            except Exception as exc:  # translated below, never leaked
                failure = translate_exception(exc)
                if type(failure) is errors.StorageError:
                    failure = errors.StorageUnavailable()
                self._failed_at = self._clock()
                self._failure = failure
                log.warning("capella connect failed (%s)", type(exc).__name__)
                raise failure from None
            self._cluster = cluster
            self._failed_at = None
            self._failure = None
            log.info("capella cluster connected")
            return cluster

    def collection(self, scope: str, name: str) -> Any:
        """Return a cached handle to an existing ``bucket.scope.collection``."""
        key = (scope, name)
        handle = self._collections.get(key)
        if handle is not None:
            return handle
        cluster = self.cluster()
        try:
            handle = cluster.bucket(self.bucket_name).scope(scope).collection(name)
        except Exception as exc:
            raise translate_exception(exc) from None
        self._collections[key] = handle
        return handle

    def query(self, statement: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Run a parameterized SQL++ statement and return all rows.

        ``statement`` must be composed only from code constants; every value is
        passed as a named parameter.
        """
        from couchbase.options import QueryOptions

        cluster = self.cluster()
        try:
            result = cluster.query(
                statement,
                QueryOptions(
                    named_parameters=params,
                    timeout=timedelta(seconds=self.settings.capella_query_timeout_seconds),
                ),
            )
            return list(result.rows())
        except Exception as exc:
            failure = translate_exception(exc)
            log.warning("capella query failed (%s)", type(exc).__name__)
            raise failure from None

    def close(self) -> None:
        """Close the cluster connection, if one was opened."""
        with self._lock:
            cluster, self._cluster = self._cluster, None
            self._collections.clear()
        if cluster is not None:
            try:
                cluster.close()
            except Exception as exc:  # best effort at shutdown
                log.warning("capella close failed (%s)", type(exc).__name__)


_client: CapellaClient | None = None
_client_lock = threading.Lock()


def get_client(settings: Settings) -> CapellaClient:
    """Return the process-wide client (created lazily, without connecting)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = CapellaClient(settings)
    return _client


def set_client(client: CapellaClient | None) -> None:
    """Replace the process-wide client (tests, scripts)."""
    global _client
    with _client_lock:
        _client = client


def close_client() -> None:
    """Close and drop the process-wide client; safe to call when never used."""
    global _client
    with _client_lock:
        client, _client = _client, None
    if client is not None:
        client.close()
