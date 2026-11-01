"""Unit tests for the reusable Capella client and owner-scoped repository."""

from __future__ import annotations

import logging
import uuid

import pytest
from couchbase import exceptions as cbx

from gateway.storage.capella import client as client_mod
from gateway.storage.capella import errors
from gateway.storage.capella.client import CapellaClient, translate_exception
from gateway.storage.capella.models import Page
from gateway.storage.capella.repository import MAX_PAGE_SIZE, OwnedDocumentRepository
from tests.capella_fakes import capella_settings, fake_client

SECRETS = ("TopSecretPassw0rd!", "example-secret-host", "svc-user")

ALICE = uuid.uuid4()
BOB = uuid.uuid4()


def _repo(client: CapellaClient) -> OwnedDocumentRepository:
    return OwnedDocumentRepository(client, scope="recipes", collection="recipes", doc_type="note")


def _doc(owner: uuid.UUID, doc_id: uuid.UUID, **extra) -> dict:
    return {"id": str(doc_id), "owner_user_id": str(owner), **extra}


# -- keys & ownership ---------------------------------------------------------


def test_key_embeds_owner_and_type():
    client, _ = fake_client()
    doc_id = uuid.uuid4()
    assert _repo(client).key(ALICE, doc_id) == f"user::{ALICE}::note::{doc_id}"


@pytest.mark.parametrize("bad", ["x", "::", f"{uuid.uuid4()}::recipe", "", "../etc"])
def test_non_uuid_ids_are_not_found_not_injected(bad):
    client, _ = fake_client()
    with pytest.raises(errors.DocumentNotFound):
        _repo(client).key(ALICE, bad)


def test_get_hides_document_whose_body_owner_differs():
    client, cluster = fake_client()
    repo = _repo(client)
    doc_id = uuid.uuid4()
    # A document stored under Alice's key but claiming Bob as owner is not returned.
    cluster.collection_.docs[repo.key(ALICE, doc_id)] = (_doc(BOB, doc_id), 1)
    with pytest.raises(errors.DocumentNotFound):
        repo.get(ALICE, doc_id)


def test_insert_get_replace_remove_roundtrip_with_cas():
    client, _ = fake_client()
    repo = _repo(client)
    doc_id = uuid.uuid4()
    cas = repo.insert(ALICE, doc_id, _doc(ALICE, doc_id, v=1))
    doc, got_cas = repo.get(ALICE, doc_id)
    assert doc["v"] == 1 and got_cas == cas

    new_cas = repo.replace(ALICE, doc_id, _doc(ALICE, doc_id, v=2), cas)
    with pytest.raises(errors.VersionConflict):
        repo.replace(ALICE, doc_id, _doc(ALICE, doc_id, v=3), cas)  # stale CAS
    assert repo.get(ALICE, doc_id)[0]["v"] == 2
    assert repo.get(ALICE, doc_id)[1] == new_cas

    repo.remove(ALICE, doc_id)
    with pytest.raises(errors.DocumentNotFound):
        repo.get(ALICE, doc_id)


def test_insert_duplicate_raises_already_exists():
    client, _ = fake_client()
    repo = _repo(client)
    doc_id = uuid.uuid4()
    repo.insert(ALICE, doc_id, _doc(ALICE, doc_id))
    with pytest.raises(errors.DocumentAlreadyExists):
        repo.insert(ALICE, doc_id, _doc(ALICE, doc_id))


def test_writes_refuse_body_that_disagrees_with_key():
    client, _ = fake_client()
    doc_id = uuid.uuid4()
    with pytest.raises(ValueError):
        _repo(client).insert(ALICE, doc_id, _doc(BOB, doc_id))


def test_other_owner_cannot_address_document():
    client, _ = fake_client()
    repo = _repo(client)
    doc_id = uuid.uuid4()
    repo.insert(ALICE, doc_id, _doc(ALICE, doc_id))
    with pytest.raises(errors.DocumentNotFound):
        repo.get(BOB, doc_id)
    with pytest.raises(errors.DocumentNotFound):
        repo.remove(BOB, doc_id)
    assert repo.get(ALICE, doc_id)


# -- queries ------------------------------------------------------------------


def test_owned_query_always_scopes_owner_and_parameterizes():
    client, _ = fake_client()
    statement, params = _repo(client).build_owned_query(
        select="d.id",
        where="d.searchable.category = $category",
        order_by="d.updated_at DESC, d.id",
        params={"category": "x'); DROP"},
        owner=ALICE,
        limit=10_000,
        offset=-5,
    )
    assert statement.startswith("SELECT d.id FROM `personal`.`recipes`.`recipes` AS d WHERE ")
    assert "d.owner_user_id = $owner AND (d.searchable.category = $category)" in statement
    assert statement.endswith("LIMIT $limit OFFSET $offset")
    assert "DROP" not in statement  # values never interpolated
    assert params == {
        "category": "x'); DROP",
        "owner": str(ALICE),
        "limit": MAX_PAGE_SIZE,
        "offset": 0,
    }


def test_owned_query_rejects_reserved_param_override():
    client, _ = fake_client()
    with pytest.raises(ValueError):
        _repo(client).build_owned_query(
            select="d.id", order_by="d.id", params={"owner": str(BOB)}, owner=ALICE, limit=1
        )


def test_invalid_keyspace_identifiers_rejected():
    client, _ = fake_client()
    with pytest.raises(errors.StorageMisconfigured):
        OwnedDocumentRepository(client, scope="re`cipes", collection="r", doc_type="recipe")
    bad_bucket, _ = fake_client(capella_settings(capella_bucket="a`b"))
    with pytest.raises(errors.StorageMisconfigured):
        _ = _repo(bad_bucket).keyspace


def test_page_from_rows_sets_next_offset():
    assert Page.from_rows([{"i": 1}, {"i": 2}, {"i": 3}], limit=2, offset=4).next_offset == 6
    assert Page.from_rows([{"i": 1}], limit=2, offset=0).next_offset is None


# -- client: config, connection lifecycle, errors -----------------------------


def test_unconfigured_client_raises_not_configured_without_connecting():
    calls = []
    settings = capella_settings(capella_connection_string=None)
    client = CapellaClient(settings, cluster_factory=lambda s: calls.append(s))
    with pytest.raises(errors.StorageNotConfigured):
        client.cluster()
    assert calls == []


def test_plaintext_connection_string_rejected():
    settings = capella_settings(capella_connection_string="couchbase://cb.example.com")
    client = CapellaClient(settings, cluster_factory=lambda _s: pytest.fail("connected"))
    with pytest.raises(errors.StorageMisconfigured) as exc:
        client.cluster()
    assert "couchbases://" in str(exc.value)


def test_cluster_connects_once_and_is_reused():
    calls = []

    def factory(_settings):
        calls.append(1)
        return object()

    client = CapellaClient(capella_settings(), cluster_factory=factory)
    assert client.cluster() is client.cluster()
    assert len(calls) == 1


def test_connect_failure_is_translated_logged_safely_and_cooled_down(caplog):
    now = [0.0]
    calls = []

    def factory(_settings):
        calls.append(1)
        raise cbx.AuthenticationException(
            message=f"bad creds {SECRETS[0]} for {SECRETS[1]}"
        )

    client = CapellaClient(capella_settings(), cluster_factory=factory, clock=lambda: now[0])
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(errors.StorageMisconfigured) as first:
            client.cluster()
        with pytest.raises(errors.StorageMisconfigured):
            client.cluster()  # within cooldown: no new connect attempt
    assert len(calls) == 1
    now[0] = client_mod.CONNECT_RETRY_COOLDOWN_SECONDS + 1
    with pytest.raises(errors.StorageMisconfigured):
        client.cluster()
    assert len(calls) == 2

    surfaced = str(first.value) + caplog.text
    assert first.value.__cause__ is None and first.value.__suppress_context__
    for secret in SECRETS:
        assert secret not in surfaced


def test_unreachable_cluster_maps_to_unavailable():
    def factory(_settings):
        raise cbx.UnAmbiguousTimeoutException()

    client = CapellaClient(capella_settings(), cluster_factory=factory)
    with pytest.raises(errors.StorageUnavailable):
        client.cluster()


def test_unknown_connect_error_maps_to_unavailable():
    def factory(_settings):
        raise RuntimeError("socket error to " + SECRETS[1])

    client = CapellaClient(capella_settings(), cluster_factory=factory)
    with pytest.raises(errors.StorageUnavailable) as exc:
        client.cluster()
    assert SECRETS[1] not in str(exc.value)


@pytest.mark.parametrize(
    ("exc", "write", "expected"),
    [
        (cbx.DocumentNotFoundException(), False, errors.DocumentNotFound),
        (cbx.DocumentExistsException(), True, errors.DocumentAlreadyExists),
        (cbx.CasMismatchException(), True, errors.VersionConflict),
        (cbx.AmbiguousTimeoutException(), True, errors.AmbiguousWrite),
        (cbx.AmbiguousTimeoutException(), False, errors.StorageUnavailable),
        (cbx.UnAmbiguousTimeoutException(), False, errors.StorageUnavailable),
        (cbx.ServiceUnavailableException(), False, errors.StorageUnavailable),
        (cbx.AuthenticationException(), False, errors.StorageMisconfigured),
        (cbx.KeyspaceNotFoundException(), False, errors.StorageMisconfigured),
        (cbx.CollectionNotFoundException(), False, errors.StorageMisconfigured),
        (cbx.QueryIndexNotFoundException(), False, errors.StorageMisconfigured),
        (RuntimeError("boom"), False, errors.StorageError),
    ],
)
def test_exception_translation(exc, write, expected):
    assert type(translate_exception(exc, write=write)) is expected


def test_query_failure_translated_and_kv_errors_carry_codes():
    client, cluster = fake_client()
    cluster.query_fail_with = cbx.KeyspaceNotFoundException()
    with pytest.raises(errors.StorageMisconfigured) as exc:
        _repo(client).query_owned(select="d.id", order_by="d.id", owner=ALICE, limit=1)
    assert exc.value.error_code == "storage_misconfigured"


def test_close_and_module_singleton_lifecycle(monkeypatch):
    client, cluster = fake_client()
    client.cluster()
    client.close()
    assert cluster.closed

    client_mod.set_client(None)
    settings = capella_settings(capella_connection_string=None)
    first = client_mod.get_client(settings)
    assert client_mod.get_client(settings) is first
    client_mod.close_client()  # never connected: no-op
    assert client_mod.get_client(settings) is not first
    client_mod.close_client()
