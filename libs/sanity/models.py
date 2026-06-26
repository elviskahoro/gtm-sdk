"""Pydantic models for Sanity blog content.

Models are intentionally tolerant (``extra="ignore"``): the Sanity schema can
grow fields without breaking the download path, and ``body`` is kept as raw
Portable Text (a list of opaque block dicts) so :mod:`libs.sanity.portable_text`
owns all rendering logic.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Author(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = Field(default=None, alias="_id")
    name: str | None = None


class Category(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = Field(default=None, alias="_id")
    title: str | None = None


class BlogPost(BaseModel):
    """A single ``blog.post`` document, flattened by the GROQ projection."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str | None = Field(default=None, alias="_id")
    slug: str | None = None
    title: str | None = None
    description: str | None = None
    publish_date: str | None = Field(default=None, alias="publishDate")
    created_at: str | None = Field(default=None, alias="_createdAt")
    updated_at: str | None = Field(default=None, alias="_updatedAt")
    authors: list[Author] = Field(default_factory=list)
    categories: list[Category] = Field(default_factory=list)
    body: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("authors", "categories", "body", mode="before")
    @classmethod
    def _drop_nulls(cls, value: Any) -> Any:
        """Dereferenced refs can come back as ``null`` for deleted targets.

        GROQ ``authors[]->{...}`` yields ``None`` entries when a referenced
        document no longer exists; strip them so validation never trips over a
        null where an object is expected.
        """
        if value is None:
            return []
        if isinstance(value, list):
            return [item for item in value if item is not None]
        return value
