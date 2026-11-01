"""In-memory stand-ins for the Couchbase SDK used by recipe/storage unit tests.

The fake collection mirrors KV semantics (insert/replace/remove with CAS) and
raises the SDK's real exception types, so the client's exception translation
is exercised. The fake cluster's ``query`` records every statement and its
named parameters, and evaluates the recipe queries' semantics in Python
(owner predicate, filters, ranking, pagination). Real SQL++ is exercised only
by the opt-in integration tests.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Any

from couchbase import exceptions as cbx

from gateway.config import Settings
from gateway.storage.capella.client import CapellaClient


@dataclass
class _GetResult:
    value: dict[str, Any]
    cas: int

    @property
    def content_as(self) -> dict:
        return {dict: self.value}


@dataclass
class _MutationResult:
    cas: int


class FakeCollection:
    def __init__(self) -> None:
        self.docs: dict[str, tuple[dict[str, Any], int]] = {}
        self._cas = itertools.count(1000)
        self.fail_with: Exception | None = None

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def get(self, key: str) -> _GetResult:
        self._maybe_fail()
        if key not in self.docs:
            raise cbx.DocumentNotFoundException()
        value, cas = self.docs[key]
        return _GetResult(dict(value), cas)

    def insert(self, key: str, value: dict[str, Any], *_opts) -> _MutationResult:
        self._maybe_fail()
        if key in self.docs:
            raise cbx.DocumentExistsException()
        cas = next(self._cas)
        self.docs[key] = (dict(value), cas)
        return _MutationResult(cas)

    def replace(self, key: str, value: dict[str, Any], options=None) -> _MutationResult:
        self._maybe_fail()
        if key not in self.docs:
            raise cbx.DocumentNotFoundException()
        expected = (options or {}).get("cas")
        if expected is not None and expected != self.docs[key][1]:
            raise cbx.CasMismatchException()
        cas = next(self._cas)
        self.docs[key] = (dict(value), cas)
        return _MutationResult(cas)

    def remove(self, key: str, options=None) -> _MutationResult:
        self._maybe_fail()
        if key not in self.docs:
            raise cbx.DocumentNotFoundException()
        expected = (options or {}).get("cas")
        if expected is not None and expected != self.docs[key][1]:
            raise cbx.CasMismatchException()
        del self.docs[key]
        return _MutationResult(next(self._cas))

    def bump(self, key: str, **changes: Any) -> int:
        """Simulate a concurrent writer modifying a document."""
        value, _ = self.docs[key]
        value = {**value, **changes}
        cas = next(self._cas)
        self.docs[key] = (value, cas)
        return cas


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def rows(self):
        return iter(self._rows)


_SUMMARY = ("id", "title", "language", "cuisine", "category", "tags", "servings", "updated_at")


def _score(doc: dict[str, Any], q: str) -> int:
    s = doc["searchable"]
    title = s["title"]
    score = 100 if title == q else 60 if title.startswith(q) else 40 if q in title else 0
    if q in s["tags"]:
        score += 30
    elif any(q in t for t in s["tags"]):
        score += 15
    if any(q in i for i in s["ingredients"]):
        score += 20
    if q in s["cuisine"] or q in s["category"]:
        score += 10
    return score


def _any_field(doc: dict[str, Any], term: str) -> bool:
    s = doc["searchable"]
    return (
        term in s["title"]
        or any(term in t for t in s["tags"])
        or any(term in i for i in s["ingredients"])
        or term in s["cuisine"]
        or term in s["category"]
    )


@dataclass
class FakeCluster:
    collection_: FakeCollection = field(default_factory=FakeCollection)
    queries: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    closed: bool = False
    query_fail_with: Exception | None = None

    def bucket(self, _name: str) -> FakeCluster:
        return self

    def scope(self, _name: str) -> FakeCluster:
        return self

    def collection(self, _name: str) -> FakeCollection:
        return self.collection_

    def close(self) -> None:
        self.closed = True

    def query(self, statement: str, options) -> _Result:
        params = dict(options["named_parameters"])
        self.queries.append((statement, params))
        if self.query_fail_with is not None:
            raise self.query_fail_with
        assert "d.owner_user_id = $owner" in statement, "owner predicate missing"
        docs = [v for v, _ in self.collection_.docs.values()]
        docs = [d for d in docs if d.get("owner_user_id") == params["owner"]]
        if "$category" in statement:
            docs = [d for d in docs if d["searchable"]["category"] == params["category"]]
        if "t = $tag" in statement:
            docs = [d for d in docs if params["tag"] in d["searchable"]["tags"]]
        for name in sorted(p for p in params if re.fullmatch(r"t\d+", p)):
            docs = [d for d in docs if _any_field(d, params[name])]
        for name in sorted(p for p in params if re.fullmatch(r"ing\d+", p)):
            docs = [
                d for d in docs if any(params[name] in i for i in d["searchable"]["ingredients"])
            ]
        if "$cuisine" in statement:
            docs = [d for d in docs if params["cuisine"] in d["searchable"]["cuisine"]]

        docs.sort(key=lambda d: d["id"])
        docs.sort(key=lambda d: d["updated_at"], reverse=True)
        rows = []
        for d in docs:
            row = {k: d.get(k) for k in _SUMMARY}
            if " AS score" in statement:
                row["score"] = _score(d, params["q"])
            rows.append(row)
        if " AS score" in statement:
            rows.sort(key=lambda r: r["score"], reverse=True)  # stable: keeps recency order
        start = params["offset"]
        return _Result(rows[start : start + params["limit"]])


def capella_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
        base_url="http://localhost:8000",
        google_client_id="cid",
        google_client_secret="secret",
        token_encryption_key="x" * 43 + "=",
        gateway_shared_secret="shared-secret-value",
        trusted_open_webui_origin="https://openwebui.internal",
        session_secret="sess",
        capella_connection_string="couchbases://cb.example-secret-host.cloud.couchbase.com",
        capella_username="svc-user",
        capella_password="TopSecretPassw0rd!",
    )
    base.update(overrides)
    return Settings(**base)


def fake_client(settings: Settings | None = None) -> tuple[CapellaClient, FakeCluster]:
    cluster = FakeCluster()
    client = CapellaClient(settings or capella_settings(), cluster_factory=lambda _s: cluster)
    return client, cluster
