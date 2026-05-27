"""Pydantic models for Exa API with strict validation.

Key design forces:
1. Snake_case all the way down, including nested dicts (prevents typos).
2. Deprecated parameters (useAutoprompt, numSentences, etc.) absent + extra="forbid" rejects them.
3. Category-conditional invariants enforced at model construction.
4. outputSchema is first-class (structured output support).
5. Per-call cost bubbled up (costDollars.total in every response).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# --- Content options (nested under contents) ---


class TextOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HighlightsOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    highlights_per_page: int | None = None


class SummaryOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    length: Literal["short", "long"] | None = None


class ContentsOptions(BaseModel):
    """Wrapper for contents field — can be bool, options object, or None."""

    model_config = ConfigDict(extra="forbid")
    text: bool | TextOptions | None = None
    highlights: bool | HighlightsOptions | None = None
    summary: bool | SummaryOptions | None = None


# --- Input model ---


class SearchInput(BaseModel):
    """Exa search query with strict validation.

    Category-conditional invariants:
    - If category in {"company", "people"}, start_published_date, end_published_date,
      and exclude_domains must not be set (Exa API silently disables them).
    - num_results must be between 1 and 100 inclusive.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    # NB: ``min_length=1`` rejects ``""`` but not ``"   "``; the trim-and-reject
    # logic for whitespace-only inputs lives in ``validate_query`` below so
    # every entry point (SearchInput / SearchQuery / FindCompaniesQuery /
    # FindPeopleQuery) blocks it consistently.
    type: Literal[
        "auto",
        "fast",
        "instant",
        "deep-lite",
        "deep",
        "deep-reasoning",
    ] = "auto"
    num_results: int = 10
    category: (
        Literal[
            "general",
            "company",
            "research",
            "news",
            "twitter",
            "people",
        ]
        | None
    ) = None
    user_location: str | None = None
    include_domains: list[str] | None = None
    exclude_domains: list[str] | None = None
    start_published_date: str | None = None
    end_published_date: str | None = None
    contents: bool | ContentsOptions | None = None
    output_schema: dict[str, Any] | None = None
    system_prompt: str | None = None
    moderation: bool | None = None

    @field_validator("query")
    @classmethod
    def validate_query(cls, v: str) -> str:
        """Strip whitespace and reject blank queries (roborev finding).

        ``Field(min_length=1)`` alone accepts ``"   "``; this validator catches
        the whitespace-only case and returns the trimmed form so downstream
        consumers see a normalized value.
        """
        stripped = v.strip()
        if not stripped:
            raise ValueError("query must be a non-empty / non-whitespace string")
        return stripped

    @field_validator("num_results")
    @classmethod
    def validate_num_results(cls, v: int) -> int:
        if not (1 <= v <= 100):
            raise ValueError("num_results must be between 1 and 100 inclusive")
        return v

    @field_validator("start_published_date", "end_published_date", "exclude_domains")
    @classmethod
    def validate_category_restrictions(cls, v: Any, info) -> Any:
        # Only validate if category is set to company or people
        category = info.data.get("category")
        if category in {"company", "people"} and v is not None:
            field_name = info.field_name
            raise ValueError(
                f"Field '{field_name}' cannot be set when category is '{category}' "
                "(Exa API disables it for category=company/people)",
            )
        return v

    @field_validator("include_domains", "exclude_domains")
    @classmethod
    def strip_and_reject_blank_domains(cls, v: list[str] | None) -> list[str] | None:
        """Strip whitespace and reject blank / all-empty domain lists.

        Centralizing this here (rather than only in the CLI flag-parser) means
        ``--json`` payloads and direct ``SearchInput`` construction get the
        same normalization. Empty/blank entries raise; an all-blank input
        (e.g. ``[""]`` or what ``"," .split(",")`` produces) raises rather
        than silently becoming ``[]`` (roborev finding).

        Returns ``None`` unchanged so unset fields stay unset.
        """
        if v is None:
            return v
        if not v:
            raise ValueError("domain list must be non-empty when set")
        normalized: list[str] = []
        for idx, entry in enumerate(v):
            stripped = entry.strip()
            if not stripped:
                raise ValueError(
                    f"domain entry [{idx}] must be a non-empty string",
                )
            normalized.append(stripped)
        return normalized


# --- Result models ---


class SearchResultItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    id: str | None = None
    title: str | None = None
    published_date: str | None = None
    author: str | None = None
    image: str | None = None
    favicon: str | None = None
    text: str | None = None
    highlights: list[str] | None = None
    highlight_scores: list[float] | None = None
    summary: str | None = None
    subpages: list[str] | None = None
    extras: dict[str, Any] | None = None


class GroundingCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    title: str | None = None
    published_date: str | None = None
    author: str | None = None
    text: str | None = None
    confidence: str | None = None


class OutputGrounding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citations: list[GroundingCitation] = []


class SearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # ``content`` is whatever the caller's ``output_schema`` declares — a
    # string (free-text), object/dict, array/list, number, or boolean (per
    # JSON Schema). Keep the field as a full JSON-value union so the adapter
    # never narrows or drops valid structured output that happens to be
    # falsey (roborev finding).
    content: str | int | float | bool | list[Any] | dict[str, Any] | None = None
    grounding: OutputGrounding | None = None


class CostDollars(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search: float = 0.0
    contents: float = 0.0
    total: float = 0.0


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str | None = None
    search_type: str | None = None
    results: list[SearchResultItem] = []
    output: SearchOutput | None = None
    cost_dollars: float = 0.0
