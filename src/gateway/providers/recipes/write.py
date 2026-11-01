"""Recipe mutating tools: save, update, delete.

Confirmation policy: saving and editing a recipe only touches the caller's own
recipe book and is fully recoverable, so ``recipes_save`` / ``recipes_update``
opt out of the two-step confirmation (``requires_confirmation=False``, the same
mechanism ``telegram_send_message`` uses). ``recipes_delete`` is irreversible
and keeps the gateway's default confirmation flow.

Retry safety (Capella and the PostgreSQL audit log don't share a transaction;
the Capella write happens first, then the audit row commits):

* ``recipes_save`` with an ``idempotency_key`` derives the recipe id
  deterministically, so a retry returns the already-saved recipe
  (``created: false``) instead of creating a duplicate.
* ``recipes_update`` is CAS-checked; a retry whose change is already present
  succeeds with ``already_applied: true`` instead of reporting a conflict.
* ``recipes_delete`` retried after success reports ``not_found``.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError
from sqlalchemy.orm import Session

from gateway.providers.base import CallContext, RiskLevel, ToolSpec
from gateway.providers.recipes.common import PROVIDER, get_repository, storage_errors
from gateway.providers.recipes.models import (
    MAX_FEEDBACK_ENTRIES,
    ClearableField,
    Ingredient,
    RecipeFields,
    RecipeId,
    build_document,
    clear_value,
    feedback_summary,
    fields_from_document,
)
from gateway.providers.registry import ToolError
from gateway.storage.capella import errors as storage
from gateway.storage.capella.models import utc_now_iso

# Fixed namespace for idempotency-key-derived recipe ids. Never change it:
# doing so would make retries of earlier saves create duplicates.
_IDEMPOTENCY_NAMESPACE = uuid.UUID("6f1d3c2e-8a4b-5c7d-9e0f-1a2b3c4d5e6f")


class SaveRecipeInput(RecipeFields):
    idempotency_key: str | None = Field(
        None,
        min_length=1,
        max_length=128,
        description=(
            "Optional client-chosen key. Retrying a save with the same key returns the "
            "recipe saved the first time instead of creating a duplicate."
        ),
    )


class UpdateRecipeInput(BaseModel):
    """Partial update: omitted/null fields are left unchanged."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    recipe_id: str = RecipeId
    version: str = Field(
        ...,
        pattern=r"^\d{1,20}$",
        description="The recipe's current `version` from recipes_get/recipes_save.",
    )
    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    language: str | None = Field(None, max_length=35)
    cuisine: str | None = Field(None, max_length=100)
    category: str | None = Field(None, max_length=100)
    servings: int | None = Field(None, ge=1, le=1000)
    ingredients: list[Ingredient] | None = Field(
        None, max_length=200, description="Replaces the whole ingredient list."
    )
    instructions: list[str] | None = Field(
        None, max_length=100, description="Replaces all steps (ordered)."
    )
    prep_time_minutes: int | None = Field(None, ge=0, le=10080)
    cook_time_minutes: int | None = Field(None, ge=0, le=10080)
    tags: list[str] | None = Field(None, max_length=30, description="Replaces all tags.")
    source_url: HttpUrl | None = None
    personal_notes: str | None = Field(None, max_length=10000)
    clear_fields: list[ClearableField] = Field(
        default_factory=list, description="Optional fields to remove from the recipe."
    )


class DeleteRecipeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipe_id: str = RecipeId


def _recipe_id(ctx: CallContext, idempotency_key: str | None) -> uuid.UUID:
    if idempotency_key is None:
        return uuid.uuid4()
    return uuid.uuid5(_IDEMPOTENCY_NAMESPACE, f"{ctx.user_id}:{idempotency_key}")


def save_recipe(args: SaveRecipeInput, ctx: CallContext, _session: Session) -> Any:
    fields = RecipeFields.model_validate(args.model_dump(exclude={"idempotency_key"}))
    recipe_id = _recipe_id(ctx, args.idempotency_key)
    now = utc_now_iso()
    doc = build_document(
        fields, recipe_id=recipe_id, owner=ctx.user_id, created_at=now, updated_at=now
    )
    repo = get_repository()
    with storage_errors():
        try:
            cas = repo.insert(ctx.user_id, recipe_id, doc)
        except storage.DocumentAlreadyExists:
            if args.idempotency_key is None:
                raise
            existing, cas = repo.get(ctx.user_id, recipe_id)
            return {
                "id": str(recipe_id),
                "version": str(cas),
                "title": existing.get("title"),
                "created": False,
                "message": "A recipe was already saved with this idempotency_key; "
                "returned it unchanged.",
            }
    return {"id": str(recipe_id), "version": str(cas), "title": fields.title, "created": True}


_UPDATABLE = tuple(RecipeFields.model_fields)


def _merged_fields(args: UpdateRecipeInput, current: dict[str, Any]) -> RecipeFields:
    changes = {
        name: getattr(args, name) for name in _UPDATABLE if getattr(args, name) is not None
    }
    both = set(changes) & set(args.clear_fields)
    if both:
        raise ToolError("invalid_input", f"cannot both set and clear: {sorted(both)}")
    merged = fields_from_document(current)
    for name in args.clear_fields:
        merged[name] = clear_value(name)
    merged.update(changes)
    try:
        return RecipeFields.model_validate(merged)
    except ValidationError as exc:
        raise ToolError("invalid_input", f"invalid recipe: {exc}") from exc


def _content(fields: RecipeFields) -> dict[str, Any]:
    return fields.model_dump(mode="json")


def _already_applied(args: UpdateRecipeInput, current: dict[str, Any]) -> bool:
    """Whether the requested change is already reflected in ``current``."""
    stored = RecipeFields.model_validate(fields_from_document(current))
    return _content(_merged_fields(args, current)) == _content(stored)


def update_recipe(args: UpdateRecipeInput, ctx: CallContext, _session: Session) -> Any:
    has_change = args.clear_fields or any(getattr(args, n) is not None for n in _UPDATABLE)
    if not has_change:
        raise ToolError("invalid_input", "nothing to update: provide at least one field")

    repo = get_repository()
    with storage_errors():
        current, cas = repo.get(ctx.user_id, args.recipe_id)
        expected = int(args.version)

        def applied_result(version: int) -> dict[str, Any]:
            return {
                "id": args.recipe_id,
                "version": str(version),
                "updated": True,
                "already_applied": True,
            }

        if cas != expected:
            if _already_applied(args, current):
                return applied_result(cas)
            raise storage.VersionConflict(
                "the recipe changed since that version was read; call recipes_get and retry"
            )

        fields = _merged_fields(args, current)
        doc = build_document(
            fields,
            recipe_id=uuid.UUID(current["id"]),
            owner=ctx.user_id,
            created_at=current["created_at"],
            updated_at=utc_now_iso(),
            feedback=current.get("feedback"),  # feedback is never touched by update
        )
        try:
            new_cas = repo.replace(ctx.user_id, args.recipe_id, doc, expected)
        except storage.VersionConflict:
            latest, latest_cas = repo.get(ctx.user_id, args.recipe_id)
            if _already_applied(args, latest):
                return applied_result(latest_cas)
            raise
    return {
        "id": args.recipe_id,
        "version": str(new_cas),
        "updated": True,
        "already_applied": False,
    }


def delete_recipe(args: DeleteRecipeInput, ctx: CallContext, _session: Session) -> Any:
    repo = get_repository()
    with storage_errors(not_found="Recipe not found (it may already have been deleted)."):
        current, _cas = repo.get(ctx.user_id, args.recipe_id)
        repo.remove(ctx.user_id, args.recipe_id)
    return {"deleted": True, "id": args.recipe_id, "title": current.get("title")}


class AddFeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    recipe_id: str = RecipeId
    rating: int = Field(..., ge=1, le=5, description="Rating from 1 (poor) to 5 (excellent).")
    comment: str | None = Field(
        None, max_length=2000, description="How it turned out, what to change next time."
    )
    cooked_on: date | None = Field(None, description="Date cooked (YYYY-MM-DD), if known.")
    idempotency_key: str | None = Field(
        None,
        min_length=1,
        max_length=128,
        description="Optional key; retrying with the same key does not add a duplicate entry.",
    )


class DeleteFeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_id: str = RecipeId
    feedback_id: str = Field(..., pattern=r"^[0-9a-fA-F-]{36}$", description="Feedback entry id.")


_FEEDBACK_ATTEMPTS = 4


def _modify_feedback(ctx: CallContext, recipe_id: str, change) -> dict[str, Any]:
    """Read-modify-write the recipe's feedback list under CAS, retrying on conflict.

    Feedback tools don't take a ``version`` (the user didn't edit the recipe
    itself), so a concurrent change is retried against the fresh copy.
    """
    repo = get_repository()
    for attempt in range(_FEEDBACK_ATTEMPTS):
        doc, cas = repo.get(ctx.user_id, recipe_id)
        feedback = list(doc.get("feedback") or [])
        result = change(feedback)
        if result.get("unchanged"):
            return {**result, **feedback_summary(feedback), "version": str(cas)}
        doc = {**doc, "feedback": feedback, **feedback_summary(feedback)}
        try:
            new_cas = repo.replace(ctx.user_id, recipe_id, doc, cas)
        except storage.VersionConflict:
            if attempt == _FEEDBACK_ATTEMPTS - 1:
                raise
            continue
        return {**result, **feedback_summary(feedback), "version": str(new_cas)}
    raise storage.VersionConflict()  # pragma: no cover - loop always returns or raises


def add_feedback(args: AddFeedbackInput, ctx: CallContext, _session: Session) -> Any:
    if args.idempotency_key is None:
        feedback_id = str(uuid.uuid4())
    else:
        name = f"{ctx.user_id}:{args.recipe_id}:{args.idempotency_key}"
        feedback_id = str(uuid.uuid5(_IDEMPOTENCY_NAMESPACE, name))

    def change(feedback: list[dict[str, Any]]) -> dict[str, Any]:
        if any(entry["id"] == feedback_id for entry in feedback):
            return {"feedback_id": feedback_id, "added": False, "unchanged": True}
        if len(feedback) >= MAX_FEEDBACK_ENTRIES:
            raise ToolError(
                "invalid_input",
                f"a recipe can hold at most {MAX_FEEDBACK_ENTRIES} feedback entries; "
                "delete an old one first",
            )
        feedback.append(
            {
                "id": feedback_id,
                "rating": args.rating,
                "comment": args.comment,
                "cooked_on": args.cooked_on.isoformat() if args.cooked_on else None,
                "created_at": utc_now_iso(),
            }
        )
        return {"feedback_id": feedback_id, "added": True}

    with storage_errors():
        result = _modify_feedback(ctx, args.recipe_id, change)
    return {"recipe_id": args.recipe_id, **result}


def delete_feedback(args: DeleteFeedbackInput, ctx: CallContext, _session: Session) -> Any:
    def change(feedback: list[dict[str, Any]]) -> dict[str, Any]:
        remaining = [e for e in feedback if e["id"] != args.feedback_id]
        if len(remaining) == len(feedback):
            raise ToolError("not_found", "Feedback entry not found.")
        feedback[:] = remaining
        return {"feedback_id": args.feedback_id, "deleted": True}

    with storage_errors():
        result = _modify_feedback(ctx, args.recipe_id, change)
    return {"recipe_id": args.recipe_id, **result}


def _delete_preview(args: BaseModel) -> str:
    return f"Permanently delete recipe {args.recipe_id}? This cannot be undone."


def register(registry) -> None:
    """Register recipe mutating tools with the tool registry."""
    registry.add(
        ToolSpec(
            name="recipes_save",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            description=(
                "Save a new recipe to the user's recipe book (English, Persian or any language). "
                "Ingredients keep their original units; quantities may be numbers or text like "
                "'1 1/2'. Returns the new recipe id and version. Pass an idempotency_key to make "
                "retries safe."
            ),
            input_model=SaveRecipeInput,
            handler=save_recipe,
            requires_confirmation=False,
        )
    )
    registry.add(
        ToolSpec(
            name="recipes_update",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            description=(
                "Update one of the user's recipes. Only the fields you pass change; list fields "
                "(ingredients, instructions, tags) are replaced whole; use clear_fields to remove "
                "optional fields. Requires the current `version` from recipes_get; a "
                "version_conflict means it changed meanwhile — re-read and retry."
            ),
            input_model=UpdateRecipeInput,
            handler=update_recipe,
            requires_confirmation=False,
        )
    )
    registry.add(
        ToolSpec(
            name="recipes_add_feedback",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            description=(
                "Add the user's feedback on a recipe they cooked: a 1-5 rating, an optional "
                "comment and the date cooked. A recipe can have several entries over time; "
                "they appear in recipes_get and the average rating in list/search results. "
                "Does not change the recipe itself."
            ),
            input_model=AddFeedbackInput,
            handler=add_feedback,
            requires_confirmation=False,
        )
    )
    registry.add(
        ToolSpec(
            name="recipes_delete_feedback",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            description=(
                "Remove one feedback entry (by feedback_id from recipes_get) from a recipe. "
                "Requires confirmation."
            ),
            input_model=DeleteFeedbackInput,
            handler=delete_feedback,
            preview_builder=lambda a: f"Delete feedback {a.feedback_id} on recipe {a.recipe_id}?",
        )
    )
    registry.add(
        ToolSpec(
            name="recipes_delete",
            provider=PROVIDER,
            risk=RiskLevel.MUTATING,
            description=(
                "Permanently delete one of the user's recipes. The first call returns a preview "
                "and confirmation_token; confirm with the user, then call again with the token."
            ),
            input_model=DeleteRecipeInput,
            handler=delete_recipe,
            preview_builder=_delete_preview,
        )
    )
