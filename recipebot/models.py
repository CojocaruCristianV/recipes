"""Pydantic models: Gemini output (Extraction) and the stored record (Recipe)."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Field order here is the key order written to recipes.json.
RECIPE_FIELDS = (
    "name", "cuisine", "type", "time", "ingredients", "steps", "url", "needs_review", "added",
)


class Extraction(BaseModel):
    """Structured output requested from Gemini (also used as the response schema)."""

    is_recipe: bool = Field(description="False only if the post has no ingredients and no method.")
    name: str = Field(description="Short descriptive English name, Title Case.")
    cuisine: str = Field(description="Cuisine, Title Case, singular. Reuse an existing value if one fits.")
    type: str = Field(description="Recipe type, Title Case, singular. Reuse an existing value if one fits.")
    time: str = Field(description='Total time, e.g. "25 min", "1 h 15 min", "20 min + 24 h".')
    ingredients: list[str] = Field(description="One ingredient per item, quantity first.")
    steps: list[str] = Field(description="One action per item, in order, no numbering.")
    caption_has_ingredients: bool = Field(description="True if the caption text itself lists the ingredients.")
    caption_has_steps: bool = Field(description="True if the caption text itself describes the method.")
    needs_review: bool = Field(description="True only if the recipe is not usable as is.")
    review_reason: str = Field(description="Short reason when needs_review is true, else empty.")


class Recipe(BaseModel):
    """One entry of recipes.json. No extra fields are allowed."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1)
    cuisine: str = Field(min_length=1)
    type: str = Field(min_length=1)
    time: str
    ingredients: list[str] = Field(min_length=1)
    steps: list[str] = Field(min_length=1)
    url: str = Field(pattern=r"^https://www\.instagram\.com/p/[A-Za-z0-9_-]{5,}/$")
    needs_review: bool
    added: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("ingredients", "steps")
    @classmethod
    def _non_empty_items(cls, v: list[str]) -> list[str]:
        items = [s.strip() for s in v if s and s.strip()]
        if not items:
            raise ValueError("must contain at least one non-empty item")
        return items

    def to_json(self) -> dict:
        data = self.model_dump()
        return {k: data[k] for k in RECIPE_FIELDS}
