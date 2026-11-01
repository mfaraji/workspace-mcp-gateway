"""Opt-in tests against a real Capella cluster.

Skipped unless ``CAPELLA_INTEGRATION_TESTS=1`` and the ``CAPELLA_*`` settings
(environment or .env) point at a cluster whose recipes keyspace and index exist
(see README "Recipes storage"). Each test uses fresh random owner UUIDs and
deletes what it creates.
"""

from __future__ import annotations

import os
import uuid

import pytest

from gateway.config import get_settings
from gateway.providers.base import CallContext
from gateway.providers.recipes import common, read, write
from gateway.providers.registry import ToolError
from gateway.storage.capella import client as client_mod


def _enabled() -> bool:
    if os.getenv("CAPELLA_INTEGRATION_TESTS") != "1":
        return False
    try:
        return get_settings().capella_configured
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _enabled(), reason="set CAPELLA_INTEGRATION_TESTS=1 and CAPELLA_* to run"
)


@pytest.fixture
def users():
    client_mod.set_client(client_mod.CapellaClient(get_settings()))
    alice = CallContext(user_id=uuid.uuid4(), external_user_id="it-alice", request_id="r")
    bob = CallContext(user_id=uuid.uuid4(), external_user_id="it-bob", request_id="r")
    yield alice, bob
    repo = common.get_repository()
    for ctx in (alice, bob):
        for row in repo.query_owned(
            select="d.id", order_by="d.id", owner=ctx.user_id, limit=100
        ):
            repo.remove(ctx.user_id, row["id"])
    client_mod.close_client()


def test_crud_search_and_isolation_against_capella(users):
    alice, bob = users
    saved = write.save_recipe(
        write.SaveRecipeInput(
            title="کیک شکلاتی",
            language="fa",
            cuisine="Persian",
            ingredients=[{"name": "شكلات", "quantity": "۱/۲", "unit": "پیمانه"}],
            instructions=["مخلوط کنید", "بپزید"],
            tags=["dessert"],
        ),
        alice,
        None,
    )
    rid = saved["id"]

    got = read.get_recipe(read.GetRecipeInput(recipe_id=rid), alice, None)
    assert got["title"] == "کیک شکلاتی"

    found = read.search_recipes(read.SearchRecipesInput(query="شکلات"), alice, None)
    assert [r["id"] for r in found["items"]] == [rid]
    assert read.search_recipes(read.SearchRecipesInput(query="شکلات"), bob, None)["items"] == []
    assert read.list_recipes(read.ListRecipesInput(tag="DESSERT"), alice, None)["items"]

    with pytest.raises(ToolError) as exc:
        read.get_recipe(read.GetRecipeInput(recipe_id=rid), bob, None)
    assert exc.value.error_code == "not_found"

    updated = write.update_recipe(
        write.UpdateRecipeInput(recipe_id=rid, version=got["version"], servings=4), alice, None
    )
    with pytest.raises(ToolError) as exc:
        write.update_recipe(
            write.UpdateRecipeInput(recipe_id=rid, version=got["version"], servings=9),
            alice,
            None,
        )
    assert exc.value.error_code == "version_conflict"
    assert updated["version"] != got["version"]

    write.delete_recipe(write.DeleteRecipeInput(recipe_id=rid), alice, None)
    with pytest.raises(ToolError):
        read.get_recipe(read.GetRecipeInput(recipe_id=rid), alice, None)
