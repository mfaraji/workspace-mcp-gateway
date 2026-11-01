"""Recipe read tools: get, list, and search the caller's own recipes.

All queries go through :class:`OwnedDocumentRepository`, which injects the
``d.owner_user_id = $owner`` predicate (owner = ``CallContext.user_id``) and
binds every value as a named parameter. Query text below is code-constant;
client input only ever reaches Capella as parameter values.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from gateway.providers.base import CallContext, RiskLevel, ToolSpec
from gateway.providers.recipes.common import PROVIDER, get_repository, storage_errors
from gateway.providers.recipes.models import RecipeId, normalize_text, to_public
from gateway.providers.registry import ToolError
from gateway.storage.capella.models import Page

MAX_LIMIT = 50
MAX_TERMS = 5
MAX_INGREDIENT_FILTERS = 5

SUMMARY_SELECT = (
    "d.id, d.title, d.language, d.cuisine, d.category, d.tags, d.servings, d.updated_at"
)
RECENT_ORDER = "d.updated_at DESC, d.id ASC"

_IN_TAGS = "ANY t IN d.searchable.tags SATISFIES t = ${p} END"
_TAG_CONTAINS = "ANY t IN d.searchable.tags SATISFIES CONTAINS(t, ${p}) END"
_INGREDIENT_CONTAINS = "ANY i IN d.searchable.ingredients SATISFIES CONTAINS(i, ${p}) END"


class GetRecipeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipe_id: str = RecipeId


class ListRecipesInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    limit: int = Field(20, ge=1, le=MAX_LIMIT, description="Page size (max 50).")
    offset: int = Field(0, ge=0, le=5000, description="Use next_offset from the previous page.")
    category: str | None = Field(None, max_length=100, description="Exact category filter.")
    tag: str | None = Field(None, max_length=50, description="Exact tag filter.")


class SearchRecipesInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str | None = Field(
        None,
        max_length=200,
        description=(
            "Free text (English or Persian) matched against title, tags, ingredient names, "
            "cuisine and category. Every word must match somewhere."
        ),
    )
    ingredients: list[str] | None = Field(
        None,
        max_length=MAX_INGREDIENT_FILTERS,
        description="Recipes must contain every one of these ingredients (substring match).",
    )
    cuisine: str | None = Field(None, max_length=100, description="Cuisine substring.")
    category: str | None = Field(None, max_length=100, description="Exact category.")
    tag: str | None = Field(None, max_length=50, description="Exact tag.")
    limit: int = Field(20, ge=1, le=MAX_LIMIT)
    offset: int = Field(0, ge=0, le=5000)

    @model_validator(mode="after")
    def _needs_criterion(self) -> SearchRecipesInput:
        if not any(
            normalize_text(v)
            for v in [self.query, self.cuisine, self.category, self.tag, *(self.ingredients or [])]
        ):
            raise ValueError("provide at least one of query, ingredients, cuisine, category, tag")
        return self


def get_recipe(args: GetRecipeInput, ctx: CallContext, _session: Session) -> Any:
    repo = get_repository()
    with storage_errors():
        doc, cas = repo.get(ctx.user_id, args.recipe_id)
    return to_public(doc, cas)


def _filters(
    *, category: str | None, tag: str | None, params: dict[str, Any]
) -> list[str]:
    where: list[str] = []
    if category and normalize_text(category):
        params["category"] = normalize_text(category)
        where.append("d.searchable.category = $category")
    if tag and normalize_text(tag):
        params["tag"] = normalize_text(tag)
        where.append(_IN_TAGS.format(p="tag"))
    return where


def _run_page(
    ctx: CallContext,
    *,
    select: str,
    where: list[str],
    order_by: str,
    params: dict[str, Any],
    limit: int,
    offset: int,
) -> dict[str, Any]:
    repo = get_repository()
    with storage_errors():
        rows = repo.query_owned(
            select=select,
            where=" AND ".join(where) or None,
            order_by=order_by,
            params=params,
            owner=ctx.user_id,
            limit=limit + 1,  # one extra row tells us whether another page exists
            offset=offset,
        )
    return Page.from_rows(rows, limit=limit, offset=offset).model_dump()


def list_recipes(args: ListRecipesInput, ctx: CallContext, _session: Session) -> Any:
    params: dict[str, Any] = {}
    where = _filters(category=args.category, tag=args.tag, params=params)
    return _run_page(
        ctx,
        select=SUMMARY_SELECT,
        where=where,
        order_by=RECENT_ORDER,
        params=params,
        limit=args.limit,
        offset=args.offset,
    )


def _matches_any_field(param: str) -> str:
    return (
        f"(CONTAINS(d.searchable.title, ${param}) OR {_TAG_CONTAINS.format(p=param)} "
        f"OR {_INGREDIENT_CONTAINS.format(p=param)} OR CONTAINS(d.searchable.cuisine, ${param}) "
        f"OR CONTAINS(d.searchable.category, ${param}))"
    )


# Relevance: exact title > title prefix > title substring > exact tag > ingredient
# > tag substring > cuisine/category. Ties break on recency, then id.
_SCORE = (
    "(CASE WHEN d.searchable.title = $q THEN 100 "
    "WHEN POSITION(d.searchable.title, $q) = 0 THEN 60 "
    "WHEN CONTAINS(d.searchable.title, $q) THEN 40 ELSE 0 END"
    " + CASE WHEN " + _IN_TAGS.format(p="q") + " THEN 30 "
    "WHEN " + _TAG_CONTAINS.format(p="q") + " THEN 15 ELSE 0 END"
    " + CASE WHEN " + _INGREDIENT_CONTAINS.format(p="q") + " THEN 20 ELSE 0 END"
    " + CASE WHEN CONTAINS(d.searchable.cuisine, $q) OR CONTAINS(d.searchable.category, $q)"
    " THEN 10 ELSE 0 END)"
)


def search_recipes(args: SearchRecipesInput, ctx: CallContext, _session: Session) -> Any:
    params: dict[str, Any] = {}
    where = _filters(category=args.category, tag=args.tag, params=params)

    phrase = normalize_text(args.query)
    terms = phrase.split()
    if len(terms) > MAX_TERMS:
        raise ToolError("invalid_input", f"query may contain at most {MAX_TERMS} words")
    for i, term in enumerate(terms):
        params[f"t{i}"] = term
        where.append(_matches_any_field(f"t{i}"))

    ingredients = [normalize_text(v) for v in args.ingredients or [] if normalize_text(v)]
    for i, ingredient in enumerate(ingredients):
        params[f"ing{i}"] = ingredient
        where.append(_INGREDIENT_CONTAINS.format(p=f"ing{i}"))

    if args.cuisine and normalize_text(args.cuisine):
        params["cuisine"] = normalize_text(args.cuisine)
        where.append("CONTAINS(d.searchable.cuisine, $cuisine)")

    if phrase:
        params["q"] = phrase
        select = f"{SUMMARY_SELECT}, {_SCORE} AS score"
        order_by = f"score DESC, {RECENT_ORDER}"
    else:
        select = SUMMARY_SELECT
        order_by = RECENT_ORDER

    return _run_page(
        ctx,
        select=select,
        where=where,
        order_by=order_by,
        params=params,
        limit=args.limit,
        offset=args.offset,
    )


def register(registry) -> None:
    """Register recipe read tools with the tool registry."""
    registry.add(
        ToolSpec(
            name="recipes_get",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "Get one of the user's saved recipes by id: full ingredients, ordered "
                "instructions, notes, and its current `version` (needed by recipes_update)."
            ),
            input_model=GetRecipeInput,
            handler=get_recipe,
        )
    )
    registry.add(
        ToolSpec(
            name="recipes_list",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "List the user's saved recipes, most recently updated first, as lightweight "
                "summaries. Optional exact category/tag filters; paginate with offset/next_offset."
            ),
            input_model=ListRecipesInput,
            handler=list_recipes,
        )
    )
    registry.add(
        ToolSpec(
            name="recipes_search",
            provider=PROVIDER,
            risk=RiskLevel.READ,
            description=(
                "Search the user's saved recipes (English or Persian) by free text over title, "
                "tags, ingredient names, cuisine and category, and/or by required ingredients, "
                "cuisine, category or tag. Case-insensitive substring matching; results are "
                "ranked by relevance and paginated with offset/next_offset."
            ),
            input_model=SearchRecipesInput,
            handler=search_recipes,
        )
    )
