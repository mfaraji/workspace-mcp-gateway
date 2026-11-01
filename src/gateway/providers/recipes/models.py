"""Recipe schema, text normalization, and document (de)serialization.

Ownership, ids and timestamps are never part of any client-facing input model
(inputs use ``extra="forbid"``); they are stamped server-side from
``CallContext`` in :mod:`gateway.providers.recipes.write`.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from fractions import Fraction
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

SCHEMA_VERSION = 1
DOC_TYPE = "recipe"

_UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_LANGUAGE_PATTERN = r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$"

RecipeId = Field(
    ..., pattern=_UUID_PATTERN, description="Recipe id (UUID) returned by recipes_save."
)

# ---------------------------------------------------------------------------
# Text normalization (shared by stored search fields and query terms)
# ---------------------------------------------------------------------------

# Arabic code points commonly typed in Persian text -> canonical Persian letters.
_LETTER_MAP = str.maketrans(
    {
        "ي": "ی",  # ي ARABIC YEH -> ی FARSI YEH
        "ى": "ی",  # ى ALEF MAKSURA -> ی
        "ك": "ک",  # ك ARABIC KAF -> ک KEHEH
        "ة": "ه",  # ة TEH MARBUTA -> ه
        "أ": "ا",  # أ -> ا
        "إ": "ا",  # إ -> ا
        "آ": "ا",  # آ -> ا
        "ؤ": "و",  # ؤ -> و
    }
)
# ZWNJ/ZWJ, tatweel, and Arabic diacritics (harakat) are dropped for matching.
_DROP = re.compile("[‌‍ـً-ٰٟ]")
_SPACES = re.compile(r"\s+")


def normalize_text(value: str | None) -> str:
    """Normalize text for case-insensitive, Persian-aware substring matching.

    NFKC -> casefold -> unify Arabic/Persian letter variants -> drop ZWNJ,
    tatweel and diacritics -> collapse whitespace. Stored only in the derived
    ``searchable`` object; the user's original text is kept verbatim.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).casefold().translate(_LETTER_MAP)
    text = _DROP.sub("", text)
    return _SPACES.sub(" ", text).strip()


# ---------------------------------------------------------------------------
# Quantities
# ---------------------------------------------------------------------------

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_NUMBER = re.compile(r"^\d+(?:\.\d+)?$")
_FRACTION = re.compile(r"^(\d+)/(\d+)$")


def parse_quantity(text: str) -> float | None:
    """Parse ``"2"``, ``"1.5"``, ``"1,5"``, ``"1/2"``, ``"1 1/2"``, ``"1½"``, ``"۱/۲"``.

    Returns ``None`` for anything else (ranges, "a pinch"); callers keep the
    original text so nothing is lost.
    """
    s = text.translate(_DIGITS).strip()
    # Arabic decimal separator and fraction slash.
    s = s.replace("٫", ".").replace("⁄", "/").replace("∕", "/")
    # Expand vulgar fractions: "1½" -> "1 1/2", "½" -> " 1/2".
    expanded = []
    for ch in s:
        value = None if ch.isascii() else unicodedata.numeric(ch, None)
        if value is not None and value < 1:
            frac = Fraction(value).limit_denominator(64)
            expanded.append(f" {frac.numerator}/{frac.denominator}")
        else:
            expanded.append(ch)
    s = re.sub(r"^(\d+),(\d+)$", r"\1.\2", "".join(expanded).strip())
    total = Fraction(0)
    parts = s.split()
    if not parts or len(parts) > 2:
        return None
    for part in parts:
        if _NUMBER.match(part):
            total += Fraction(part)
        elif m := _FRACTION.match(part):
            if int(m.group(2)) == 0:
                return None
            total += Fraction(int(m.group(1)), int(m.group(2)))
        else:
            return None
    return float(total)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Ingredient(BaseModel):
    """One ingredient line; the original unit and quantity text are preserved."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(..., min_length=1, max_length=200, description="Ingredient name.")
    quantity: float | str | None = Field(
        None,
        description=(
            "Amount as a number (0.5) or text ('1 1/2', '½', '۱/۲'). Text is parsed to a "
            "number when possible and the original is kept in quantity_text."
        ),
    )
    quantity_text: str | None = Field(
        None, max_length=50, description="Original amount text, e.g. '1 1/2' or 'a pinch'."
    )
    unit: str | None = Field(
        None, max_length=50, description="Unit exactly as written (cup, g, tbsp, پیمانه)."
    )
    brand: str | None = Field(
        None, max_length=100, description="Optional brand, e.g. 'Heinz' or 'Barilla'."
    )
    notes: str | None = Field(None, max_length=300, description="e.g. 'finely chopped'.")

    @model_validator(mode="after")
    def _normalize_quantity(self) -> Ingredient:
        if isinstance(self.quantity, str):
            raw = self.quantity.strip()
            if raw and self.quantity_text is None:
                self.quantity_text = raw[:50]
            self.quantity = parse_quantity(raw) if raw else None
        if isinstance(self.quantity, float | int):
            if self.quantity < 0 or self.quantity > 1_000_000:
                raise ValueError("quantity must be between 0 and 1,000,000")
            self.quantity = float(self.quantity)
        return self


class RecipeFields(BaseModel):
    """The user-editable content of a recipe."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(..., min_length=1, max_length=200, description="Recipe title.")
    description: str | None = Field(None, max_length=2000)
    language: str = Field(
        "en",
        pattern=_LANGUAGE_PATTERN,
        description="Language code of the recipe text, e.g. 'en' or 'fa' (Persian).",
    )
    cuisine: str | None = Field(None, max_length=100, description="e.g. 'Persian', 'Italian'.")
    category: str | None = Field(
        None, max_length=100, description="e.g. 'dessert', 'main', 'soup'."
    )
    servings: int | None = Field(None, ge=1, le=1000)
    ingredients: list[Ingredient] = Field(default_factory=list, max_length=200)
    instructions: list[str] = Field(
        default_factory=list, max_length=100, description="Ordered preparation steps."
    )
    prep_time_minutes: int | None = Field(None, ge=0, le=10080)
    cook_time_minutes: int | None = Field(None, ge=0, le=10080)
    tags: list[str] = Field(default_factory=list, max_length=30)
    source_url: HttpUrl | None = None
    personal_notes: str | None = Field(None, max_length=10000)

    @field_validator("language")
    @classmethod
    def _language(cls, value: str) -> str:
        primary, _, rest = value.partition("-")
        return primary.lower() + (f"-{rest}" if rest else "")

    @field_validator("instructions")
    @classmethod
    def _instructions(cls, value: list[str]) -> list[str]:
        steps = [step.strip() for step in value]
        if any(not step or len(step) > 4000 for step in steps):
            raise ValueError("each instruction must be 1-4000 characters")
        return steps

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        tags: list[str] = []
        for raw in value:
            tag = _SPACES.sub(" ", raw).strip()
            if not tag or len(tag) > 50:
                raise ValueError("each tag must be 1-50 characters")
            key = normalize_text(tag)
            if key not in seen:
                seen.add(key)
                tags.append(tag)
        return tags


# Optional fields that recipes_update can clear via ``clear_fields``.
ClearableField = Literal[
    "description",
    "cuisine",
    "category",
    "servings",
    "prep_time_minutes",
    "cook_time_minutes",
    "tags",
    "source_url",
    "personal_notes",
]
_CLEAR_VALUE: dict[str, Any] = {"tags": []}

CONTENT_FIELDS = tuple(RecipeFields.model_fields)
ENVELOPE_FIELDS = ("id", "owner_user_id", "schema_version", "created_at", "updated_at")


def clear_value(field: str) -> Any:
    return _CLEAR_VALUE.get(field)


def search_fields(fields: RecipeFields) -> dict[str, Any]:
    """Derived, normalized copies of searchable fields (never returned to clients)."""
    return {
        "title": normalize_text(fields.title),
        "cuisine": normalize_text(fields.cuisine),
        "category": normalize_text(fields.category),
        "tags": [normalize_text(t) for t in fields.tags],
        "ingredients": [normalize_text(i.name) for i in fields.ingredients],
    }


MAX_FEEDBACK_ENTRIES = 100


def feedback_summary(feedback: list[dict[str, Any]]) -> dict[str, Any]:
    """Denormalized rating stats stored on the recipe (used by list/search summaries)."""
    ratings = [entry["rating"] for entry in feedback if entry.get("rating") is not None]
    return {
        "feedback_count": len(feedback),
        "rating_avg": round(sum(ratings) / len(ratings), 2) if ratings else None,
    }


def build_document(
    fields: RecipeFields,
    *,
    recipe_id: uuid.UUID,
    owner: uuid.UUID,
    created_at: str,
    updated_at: str,
    feedback: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble the stored JSON document for a recipe.

    ``feedback`` is owned by the feedback tools; save starts it empty and update
    carries the existing entries over unchanged.
    """
    feedback = list(feedback or [])
    return {
        "id": str(recipe_id),
        "owner_user_id": str(owner),
        "schema_version": SCHEMA_VERSION,
        **fields.model_dump(mode="json"),
        "feedback": feedback,
        **feedback_summary(feedback),
        "created_at": created_at,
        "updated_at": updated_at,
        "searchable": search_fields(fields),
    }


def fields_from_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Extract the user-editable content from a stored document."""
    return {key: doc.get(key) for key in CONTENT_FIELDS if doc.get(key) is not None}


def to_public(doc: dict[str, Any], cas: int) -> dict[str, Any]:
    """Client-facing view: drop derived fields, expose the CAS as ``version``."""
    public = {key: value for key, value in doc.items() if key != "searchable"}
    public["version"] = str(cas)
    return public
