"""Tests for the recipes connector, driven through the real tool pipeline.

The registry pipeline (identity -> validate -> confirm -> handler -> audit) runs
unmodified; only PostgreSQL (``session_scope`` / user resolution) and Capella
(an in-memory fake cluster) are replaced, so no live services are needed.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from couchbase import exceptions as cbx

from gateway.audit.log import summarize_input
from gateway.connectors.registry import by_slug, by_spec
from gateway.db.models import ToolAuditLog
from gateway.identity.models import AuthenticatedUser
from gateway.mcp import context as mcp_context
from gateway.providers import registry as registry_mod
from gateway.providers.base import RiskLevel
from gateway.providers.recipes import common, read, write
from gateway.providers.recipes.models import normalize_text, parse_quantity
from gateway.providers.registry import ToolError, ToolRegistry, _run_pipeline
from gateway.storage.capella import client as client_mod
from tests.capella_fakes import capella_settings, fake_client

CAKE = {
    "title": "Chocolate Cake",
    "language": "en",
    "cuisine": "French",
    "category": "Dessert",
    "servings": 6,
    "ingredients": [
        {"name": "Dark chocolate", "quantity": 200, "unit": "g"},
        {"name": "Flour", "quantity": "1 1/2", "unit": "cup"},
        {"name": "Eggs", "quantity": 3},
    ],
    "instructions": ["Melt chocolate.", "Fold in flour.", "Bake 30 minutes."],
    "tags": ["baking", "Chocolate"],
    "personal_notes": "SECRET-NOTE grandma's version",
    "source_url": "https://example.com/cake",
    "prep_time_minutes": 20,
    "cook_time_minutes": 30,
}

GHORMEH = {
    "title": "قورمه‌سبزی",
    "language": "fa",
    "cuisine": "ایرانی",
    "category": "خورش",
    "servings": 4,
    "ingredients": [
        {"name": "سبزی قورمه", "quantity": "۵۰۰", "unit": "گرم"},
        {"name": "لوبیا قرمز", "quantity": "۱/۲", "unit": "پیمانه"},
        {"name": "ليمو عمانی", "quantity": 4, "unit": "عدد"},  # Arabic yeh
    ],
    "instructions": ["سبزی را سرخ کنید.", "گوشت و لوبیا را اضافه کنید."],
    "tags": ["سنتی"],
}


class _FakeSession:
    def __init__(self, sink: list) -> None:
        self.sink = sink

    def add(self, obj) -> None:
        self.sink.append(obj)


@pytest.fixture
def env(monkeypatch):
    """Fake Capella + fake Postgres, with ``call(tool, user, **args)``."""
    settings = capella_settings()
    client, cluster = fake_client(settings)
    client_mod.set_client(client)
    monkeypatch.setattr(common, "get_settings", lambda: settings)

    audits: list[ToolAuditLog] = []
    users: dict[str, uuid.UUID] = {}

    @contextmanager
    def session_scope():
        yield _FakeSession(audits)

    def get_or_create_user(_session, auth):
        return SimpleNamespace(id=users.setdefault(auth.external_user_id, uuid.uuid4()))

    monkeypatch.setattr(registry_mod, "session_scope", session_scope)
    monkeypatch.setattr(registry_mod, "get_or_create_user", get_or_create_user)

    registry = ToolRegistry()
    read.register(registry)
    write.register(registry)
    specs = {spec.name: spec for spec in registry._specs}

    def call(tool: str, user: str, confirmation_token: str | None = None, **args):
        token = mcp_context._current_user.set(AuthenticatedUser(user))
        try:
            return _run_pipeline(specs[tool], settings, args, confirmation_token)
        finally:
            mcp_context._current_user.reset(token)

    yield SimpleNamespace(
        call=call, cluster=cluster, audits=audits, users=users, settings=settings, specs=specs
    )
    client_mod.set_client(None)


def _err(env, tool, user, **args) -> ToolError:
    with pytest.raises(ToolError) as exc:
        env.call(tool, user, **args)
    return exc.value


def _delete(env, user, recipe_id):
    first = env.call("recipes_delete", user, recipe_id=recipe_id)
    assert first["status"] == "confirmation_required"
    return env.call(
        "recipes_delete", user, confirmation_token=first["confirmation_token"], recipe_id=recipe_id
    )


# 1. create and retrieve -------------------------------------------------------


def test_create_and_get_recipe(env):
    saved = env.call("recipes_save", "alice", **CAKE)
    assert saved["created"] is True and saved["title"] == "Chocolate Cake"
    uuid.UUID(saved["id"])

    got = env.call("recipes_get", "alice", recipe_id=saved["id"])
    assert got["id"] == saved["id"]
    assert got["version"] == saved["version"]
    assert got["owner_user_id"] == str(env.users["alice"])
    assert got["schema_version"] == 1
    assert got["created_at"] == got["updated_at"]
    assert got["instructions"] == CAKE["instructions"]
    flour = got["ingredients"][1]
    assert flour == {
        "name": "Flour",
        "quantity": 1.5,
        "quantity_text": "1 1/2",
        "unit": "cup",
        "brand": None,
        "notes": None,
    }
    assert got["source_url"] == "https://example.com/cake"
    assert "searchable" not in got

    key = f"user::{env.users['alice']}::recipe::{saved['id']}"
    assert key in env.cluster.collection_.docs


def test_save_is_insert_without_confirmation(env):
    spec = env.specs["recipes_save"]
    assert spec.risk is RiskLevel.MUTATING and spec.requires_confirmation is False
    assert env.specs["recipes_update"].requires_confirmation is False
    assert env.specs["recipes_delete"].requires_confirmation is None
    assert env.specs["recipes_get"].risk is RiskLevel.READ


# 2. update + CAS ------------------------------------------------------------


def test_partial_update_preserves_owner_and_created_at(env):
    saved = env.call("recipes_save", "alice", **CAKE)
    before = env.call("recipes_get", "alice", recipe_id=saved["id"])

    updated = env.call(
        "recipes_update",
        "alice",
        recipe_id=saved["id"],
        version=saved["version"],
        servings=8,
        clear_fields=["personal_notes"],
    )
    assert updated["updated"] is True and updated["already_applied"] is False

    after = env.call("recipes_get", "alice", recipe_id=saved["id"])
    assert after["servings"] == 8
    assert after["personal_notes"] is None
    assert after["title"] == CAKE["title"] and after["ingredients"] == before["ingredients"]
    assert after["created_at"] == before["created_at"]
    assert after["owner_user_id"] == before["owner_user_id"]
    assert after["version"] == updated["version"] != before["version"]


def test_update_with_stale_version_conflicts(env):
    saved = env.call("recipes_save", "alice", **CAKE)
    key = f"user::{env.users['alice']}::recipe::{saved['id']}"
    env.cluster.collection_.bump(key, servings=10)  # concurrent writer

    err = _err(env, "recipes_update", "alice", recipe_id=saved["id"],
               version=saved["version"], servings=2)
    assert err.error_code == "version_conflict"
    assert env.call("recipes_get", "alice", recipe_id=saved["id"])["servings"] == 10


def test_update_cas_race_between_read_and_replace(env, monkeypatch):
    saved = env.call("recipes_save", "alice", **CAKE)
    key = f"user::{env.users['alice']}::recipe::{saved['id']}"
    coll = env.cluster.collection_
    real_replace = coll.replace

    def racing_replace(k, value, options=None):
        coll.bump(k, servings=99)
        return real_replace(k, value, options)

    monkeypatch.setattr(coll, "replace", racing_replace)
    err = _err(env, "recipes_update", "alice", recipe_id=saved["id"],
               version=saved["version"], servings=2)
    assert err.error_code == "version_conflict"
    assert coll.docs[key][0]["servings"] == 99


def test_update_retry_after_success_is_already_applied(env):
    saved = env.call("recipes_save", "alice", **CAKE)
    args = dict(recipe_id=saved["id"], version=saved["version"], title="Best Cake")
    env.call("recipes_update", "alice", **args)
    retry = env.call("recipes_update", "alice", **args)  # same stale version
    assert retry["already_applied"] is True


def test_update_requires_a_change_and_rejects_set_and_clear(env):
    saved = env.call("recipes_save", "alice", **CAKE)
    base = dict(recipe_id=saved["id"], version=saved["version"])
    assert _err(env, "recipes_update", "alice", **base).error_code == "invalid_input"
    err = _err(env, "recipes_update", "alice", **base, cuisine="X", clear_fields=["cuisine"])
    assert err.error_code == "invalid_input"


# 3. delete with confirmation --------------------------------------------------


def test_delete_requires_confirmation(env):
    saved = env.call("recipes_save", "alice", **CAKE)
    first = env.call("recipes_delete", "alice", recipe_id=saved["id"])
    assert first["status"] == "confirmation_required"
    assert saved["id"] in first["preview"]
    # Not deleted yet.
    assert env.call("recipes_get", "alice", recipe_id=saved["id"])

    # A token is bound to the user and arguments.
    other = _err(env, "recipes_get", "alice", recipe_id=str(uuid.uuid4()))
    assert other.error_code == "not_found"
    replay = env.call(
        "recipes_delete", "bob", confirmation_token=first["confirmation_token"],
        recipe_id=saved["id"],
    )
    assert replay["status"] == "confirmation_required"

    done = env.call(
        "recipes_delete", "alice", confirmation_token=first["confirmation_token"],
        recipe_id=saved["id"],
    )
    assert done == {"deleted": True, "id": saved["id"], "title": "Chocolate Cake"}
    assert _err(env, "recipes_get", "alice", recipe_id=saved["id"]).error_code == "not_found"

    again = _delete_err(env, "alice", saved["id"])
    assert again.error_code == "not_found" and "already" in str(again)


def _delete_err(env, user, recipe_id) -> ToolError:
    with pytest.raises(ToolError) as exc:
        _delete(env, user, recipe_id)
    return exc.value


# 4/15. list, search, pagination ---------------------------------------------


def _seed(env, user: str, n: int, **overrides) -> list[str]:
    ids = []
    for i in range(n):
        ids.append(
            env.call("recipes_save", user, **{**CAKE, "title": f"Cake {i:02d}", **overrides})["id"]
        )
    return ids


def test_list_paginates_deterministically_and_only_own(env):
    mine = set(_seed(env, "alice", 5))
    _seed(env, "bob", 3)

    seen: list[str] = []
    offset = 0
    while offset is not None:
        page = env.call("recipes_list", "alice", limit=2, offset=offset)
        assert len(page["items"]) <= 2
        seen += [item["id"] for item in page["items"]]
        offset = page["next_offset"]
    assert len(seen) == len(set(seen)) == 5
    assert set(seen) == mine
    assert {"title", "updated_at", "tags"} <= set(page["items"][0])
    assert "ingredients" not in page["items"][0]

    for statement, params in env.cluster.queries:
        assert "d.owner_user_id = $owner" in statement
        assert params["owner"] == str(env.users["alice"])


def test_list_filters_by_category_and_tag(env):
    env.call("recipes_save", "alice", **CAKE)
    env.call("recipes_save", "alice", **{**CAKE, "title": "Soup", "category": "Main",
                                         "tags": ["winter"]})
    assert [r["title"] for r in env.call("recipes_list", "alice", category="dessert")["items"]] \
        == ["Chocolate Cake"]
    assert [r["title"] for r in env.call("recipes_list", "alice", tag="WINTER")["items"]] \
        == ["Soup"]


def test_search_by_title_ingredient_cuisine_tag_with_ranking(env):
    env.call("recipes_save", "alice", **{**CAKE, "title": "Banana Bread", "tags": ["cake"],
                                         "ingredients": [{"name": "banana"}]})
    env.call("recipes_save", "alice", **CAKE)
    env.call("recipes_save", "alice", **{**CAKE, "title": "Cake", "tags": []})

    titles = [r["title"] for r in env.call("recipes_search", "alice", query="CAKE")["items"]]
    assert titles == ["Cake", "Chocolate Cake", "Banana Bread"]  # exact > substring > tag

    by_ing = env.call("recipes_search", "alice", ingredients=["banana"])["items"]
    assert [r["title"] for r in by_ing] == ["Banana Bread"]
    assert env.call("recipes_search", "alice", cuisine="fren")["items"]
    assert not env.call("recipes_search", "alice", cuisine="thai")["items"]
    tagged = env.call("recipes_search", "alice", tag="baking")["items"]
    assert [r["title"] for r in tagged] == ["Chocolate Cake"]
    multi = env.call("recipes_search", "alice", query="chocolate flour")["items"]
    assert {r["title"] for r in multi} == {"Chocolate Cake", "Cake"}


def test_search_requires_a_criterion(env):
    assert _err(env, "recipes_search", "alice").error_code == "invalid_input"
    assert _err(env, "recipes_search", "alice", query="  ").error_code == "invalid_input"


def test_search_is_owner_scoped_and_parameterized(env):
    env.call("recipes_save", "bob", **CAKE)
    assert env.call("recipes_search", "alice", query="cake")["items"] == []
    statement, params = env.cluster.queries[-1]
    assert "cake" not in statement.lower().replace("$", "")  # value only in params
    assert params["q"] == "cake" and params["owner"] == str(env.users["alice"])


# 5/6. cross-user isolation ----------------------------------------------------


def test_user_cannot_read_update_or_delete_another_users_recipe(env):
    saved = env.call("recipes_save", "alice", **CAKE)
    rid, version = saved["id"], saved["version"]

    assert _err(env, "recipes_get", "bob", recipe_id=rid).error_code == "not_found"
    err = _err(env, "recipes_update", "bob", recipe_id=rid, version=version, title="pwned")
    assert err.error_code == "not_found"
    assert _delete_err(env, "bob", rid).error_code == "not_found"
    assert env.call("recipes_list", "bob")["items"] == []
    assert env.call("recipes_search", "bob", query="chocolate")["items"] == []

    still = env.call("recipes_get", "alice", recipe_id=rid)
    assert still["title"] == "Chocolate Cake" and still["version"] == version


def test_not_found_message_does_not_reveal_ownership(env):
    saved = env.call("recipes_save", "alice", **CAKE)
    other_users = _err(env, "recipes_get", "bob", recipe_id=saved["id"])
    missing = _err(env, "recipes_get", "bob", recipe_id=str(uuid.uuid4()))
    assert (other_users.error_code, str(other_users)) == (missing.error_code, str(missing))


def test_client_cannot_supply_owner_or_id(env):
    err = _err(env, "recipes_save", "alice", **CAKE, owner_user_id=str(uuid.uuid4()))
    assert err.error_code == "invalid_input"
    err = _err(env, "recipes_save", "alice", **CAKE, id=str(uuid.uuid4()))
    assert err.error_code == "invalid_input"


# 7/8. missing + duplicates ----------------------------------------------------


def test_missing_and_malformed_ids(env):
    assert _err(env, "recipes_get", "alice", recipe_id=str(uuid.uuid4())).error_code == \
        "not_found"
    assert _err(env, "recipes_get", "alice", recipe_id="user::x").error_code == "invalid_input"


def test_duplicate_insert_and_idempotent_retry(env):
    first = env.call("recipes_save", "alice", **CAKE, idempotency_key="msg-1")
    retry = env.call("recipes_save", "alice", **CAKE, idempotency_key="msg-1")
    assert first["created"] is True and retry["created"] is False
    assert retry["id"] == first["id"]
    assert len(env.cluster.collection_.docs) == 1

    # Same key for a different user is a different recipe.
    bob = env.call("recipes_save", "bob", **CAKE, idempotency_key="msg-1")
    assert bob["created"] is True and bob["id"] != first["id"]


def test_duplicate_insert_without_key_reports_already_exists(env, monkeypatch):
    fixed = uuid.uuid4()
    monkeypatch.setattr(write.uuid, "uuid4", lambda: fixed)
    env.call("recipes_save", "alice", **CAKE)
    assert _err(env, "recipes_save", "alice", **CAKE).error_code == "already_exists"


# 9. invalid input ----------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"title": ""},
        {"servings": 0},
        {"source_url": "not a url"},
        {"language": "english!"},
        {"ingredients": [{"name": "x", "quantity": -1}]},
        {"ingredients": [{"name": "x", "colour": "red"}]},
        {"instructions": ["ok", "   "]},
        {"tags": ["x" * 51]},
    ],
)
def test_invalid_recipe_input_rejected(env, bad):
    assert _err(env, "recipes_save", "alice", **{**CAKE, **bad}).error_code == "invalid_input"
    assert env.cluster.collection_.docs == {}


# 10. Persian / Unicode ---------------------------------------------------------


def test_persian_recipe_roundtrip_and_normalized_search(env):
    saved = env.call("recipes_save", "alice", **GHORMEH)
    got = env.call("recipes_get", "alice", recipe_id=saved["id"])
    assert got["title"] == "قورمه‌سبزی"  # original text preserved verbatim
    assert got["ingredients"][0]["quantity"] == 500.0
    assert got["ingredients"][1]["quantity"] == 0.5
    assert got["ingredients"][1]["quantity_text"] == "۱/۲"
    assert got["ingredients"][1]["unit"] == "پیمانه"

    # Without ZWNJ, and Persian yeh searching an Arabic-yeh ingredient.
    assert env.call("recipes_search", "alice", query="قورمه سبزی")["items"]
    assert env.call("recipes_search", "alice", query="قورمهسبزی")["items"]
    assert env.call("recipes_search", "alice", ingredients=["لیمو"])["items"]
    assert env.call("recipes_list", "alice", category="خورش")["items"]


@pytest.mark.parametrize(
    ("text", "value"),
    [("2", 2.0), ("1.5", 1.5), ("1,5", 1.5), ("1/2", 0.5), ("1 1/2", 1.5), ("½", 0.5),
     ("1½", 1.5), ("۱/۲", 0.5), ("۲٫۵", 2.5), ("a pinch", None), ("2-3", None), ("1/0", None)],
)
def test_parse_quantity(text, value):
    assert parse_quantity(text) == value


def test_normalize_text_unifies_persian_variants():
    assert normalize_text("كيك‌ها") == normalize_text("کیکها")
    assert normalize_text("  Chocolate   CAKE ") == "chocolate cake"


# 11/12. Capella unavailable / unconfigured -----------------------------------


def test_capella_not_configured_returns_stable_error(env, monkeypatch):
    settings = capella_settings(capella_connection_string=None)
    monkeypatch.setattr(common, "get_settings", lambda: settings)
    client_mod.set_client(None)
    err = _err(env, "recipes_list", "alice")
    assert err.error_code == "storage_not_configured"


def test_capella_unavailable_and_misconfigured(env):
    env.cluster.collection_.fail_with = cbx.UnAmbiguousTimeoutException()
    assert _err(env, "recipes_get", "alice", recipe_id=str(uuid.uuid4())).error_code == \
        "storage_unavailable"
    env.cluster.collection_.fail_with = cbx.AmbiguousTimeoutException()
    assert _err(env, "recipes_save", "alice", **CAKE).error_code == "storage_outcome_unknown"
    env.cluster.query_fail_with = cbx.QueryIndexNotFoundException()
    assert _err(env, "recipes_list", "alice").error_code == "storage_misconfigured"


def test_connect_failure_does_not_leak_credentials(env, monkeypatch):
    def factory(_settings):
        raise cbx.AuthenticationException(message="auth failed for TopSecretPassw0rd!")

    client_mod.set_client(client_mod.CapellaClient(env.settings, cluster_factory=factory))
    err = _err(env, "recipes_list", "alice")
    assert err.error_code == "storage_misconfigured"
    for secret in ("TopSecretPassw0rd!", "example-secret-host", "svc-user"):
        assert secret not in str(err)


def test_error_classifier_maps_raw_sdk_exceptions():
    connector = by_slug("recipes")
    assert connector.classify_error(cbx.DocumentNotFoundException()) == "not_found"
    assert connector.classify_error(cbx.ServiceUnavailableException()) == "storage_unavailable"
    assert connector.classify_error(ValueError("x")) is None


# 14. audit ------------------------------------------------------------------


def test_audit_rows_exclude_recipe_contents(env):
    saved = env.call("recipes_save", "alice", **CAKE, idempotency_key="secret-key")
    env.call("recipes_update", "alice", recipe_id=saved["id"], version=saved["version"],
             personal_notes="SECRET-NOTE-2", ingredients=[{"name": "SECRET-INGREDIENT"}])
    env.call("recipes_search", "alice", query="SECRET-QUERY", ingredients=["SECRET-ING"])
    env.call("recipes_list", "alice", tag="SECRET-TAG")
    _delete(env, "alice", saved["id"])

    summaries = [row.input_summary for row in env.audits]
    assert {row.tool_name for row in env.audits} >= {
        "recipes_save", "recipes_update", "recipes_search", "recipes_list", "recipes_delete"
    }
    blob = " ".join(summaries)
    for secret in ("SECRET", "Chocolate", "Melt", "grandma", "baking", "secret-key", "Flour"):
        assert secret not in blob
    assert f"recipe_id='{saved['id']}'" in blob
    assert "query=<set>" in blob


def test_audit_summary_for_each_recipe_tool():
    assert summarize_input("recipes_save", {**CAKE}) == "language='en'"
    assert summarize_input("recipes_list", {"limit": 5, "offset": 0, "tag": "t"}) == \
        "limit=5, offset=0"


# 13. discovery ----------------------------------------------------------------


def test_recipe_tools_belong_to_recipes_connector():
    registry = ToolRegistry()
    read.register(registry)
    write.register(registry)
    assert {s.name for s in registry._specs} == {
        "recipes_get", "recipes_list", "recipes_search",
        "recipes_save", "recipes_update", "recipes_delete",
    }
    for spec in registry._specs:
        assert by_spec(spec).slug == "recipes"


def test_ingredient_brand_is_optional_and_roundtrips(env):
    saved = env.call(
        "recipes_save",
        "alice",
        **{
            **CAKE,
            "ingredients": [
                {"name": "Ketchup", "quantity": 2, "unit": "tbsp", "brand": "  Heinz "},
                {"name": "Flour"},
            ],
        },
    )
    got = env.call("recipes_get", "alice", recipe_id=saved["id"])
    assert got["ingredients"][0]["brand"] == "Heinz"  # trimmed
    assert got["ingredients"][1]["brand"] is None

    # Updating the ingredient list can add/change a brand.
    env.call(
        "recipes_update", "alice", recipe_id=saved["id"], version=got["version"],
        ingredients=[{"name": "Ketchup", "brand": "Hunt's"}],
    )
    after = env.call("recipes_get", "alice", recipe_id=saved["id"])
    assert after["ingredients"] == [
        {"name": "Ketchup", "quantity": None, "quantity_text": None, "unit": None,
         "brand": "Hunt's", "notes": None}
    ]


def test_ingredient_brand_too_long_rejected(env):
    bad = {**CAKE, "ingredients": [{"name": "x", "brand": "b" * 101}]}
    assert _err(env, "recipes_save", "alice", **bad).error_code == "invalid_input"


def test_summary_select_quotes_sqlpp_reserved_words():
    """`language` is reserved in SQL++; unquoted it fails to parse on a real cluster."""
    assert "d.`language`" in read.SUMMARY_SELECT
    assert "d.language" not in read.SUMMARY_SELECT
