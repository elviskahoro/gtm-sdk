"""Pydantic models for Octolens mention webhook payloads.

Hookdeck wraps deliveries in a ``{"body": "<json string>"}`` envelope. The
top-level ``Webhook`` model accepts either shape — if ``body`` is present and
the dict does not already look unwrapped, ``body`` is parsed and used as the
source dict; otherwise the dict is validated directly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

import orjson
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# Known Octolens source platforms. If Octolens ships a new integration,
# webhook validation will reject the payload — we want to hear about it
# (loudly) rather than silently route mentions from an unknown platform.
# Update this list deliberately after confirming the new platform's shape.
# "youtube" was added deliberately for the historical CSV backfill
# (scripts/octolens-mentions-backfill.py), which surfaced real dlt/dlthub
# mentions from YouTube; the live webhook accepts it going forward too.
Source = Literal[
    "bluesky",
    "dev",
    "github",
    "hackernews",
    "linkedin",
    "podcasts",
    "reddit",
    "twitter",
    "youtube",
]

# "unknown" exists only for the historical CSV backfill: those exports carry no
# relevance score, so backfilled mentions are stamped "unknown". It is NOT in
# DEFAULT_FILTERS' excluded_scores (only "low" is dropped), so these mentions
# still reach Attio. Live Octolens webhooks only ever send low/medium/high.
RelevanceScore = Literal["low", "medium", "high", "unknown"]


class Mention(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    url: str = Field(validation_alias=AliasChoices("url", "URL"))
    title: str | None = Field(
        default=None,
        validation_alias=AliasChoices("title", "Title"),
    )
    body: str = Field(validation_alias=AliasChoices("body", "Body"))
    timestamp: datetime = Field(validation_alias=AliasChoices("timestamp", "Timestamp"))
    image_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("image_url", "Image URL", "imageUrl"),
    )
    source: Source = Field(validation_alias=AliasChoices("source", "Source"))
    source_id: str = Field(
        validation_alias=AliasChoices("source_id", "Source ID", "sourceId"),
    )
    author: str = Field(validation_alias=AliasChoices("author", "Author"))
    author_avatar_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "author_avatar_url",
            "Author Avatar URL",
            "authorAvatarUrl",
        ),
    )
    author_profile_link: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "author_profile_link",
            "Author Profile Link",
            "authorProfileLink",
        ),
    )
    relevance_score: RelevanceScore = Field(
        validation_alias=AliasChoices(
            "relevance_score",
            "Relevance Score",
            "relevanceScore",
        ),
    )
    relevance_comment: str = Field(
        validation_alias=AliasChoices(
            "relevance_comment",
            "Relevance Comment",
            "relevanceComment",
        ),
    )
    language: str | None = Field(
        default=None,
        validation_alias=AliasChoices("language", "Language"),
    )
    keyword: str = Field(validation_alias=AliasChoices("keyword", "Keyword"))
    keywords: list[str] = Field(default_factory=list)
    sentiment_label: str | None = Field(
        default=None,
        validation_alias=AliasChoices("sentiment_label", "sentimentLabel"),
    )
    tags: list[Any] = Field(default_factory=list)
    view_id: int | None = Field(
        default=None,
        validation_alias=AliasChoices("view_id", "viewId"),
    )
    view_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("view_name", "viewName"),
    )
    view_keywords: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("view_keywords", "viewKeywords"),
    )
    subreddit: str | None = None
    bookmarked: bool = False

    @field_validator("timestamp", mode="before")
    @classmethod
    def parse_timestamp(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        if not isinstance(value, str):
            error_msg: str = f"Invalid timestamp format: {value}"
            raise TypeError(error_msg)

        # Live Octolens form, e.g. "2026-05-10 11:55:53.000"
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            pass

        # Legacy GMT form, e.g. "Mon Jan 15 2024 10:30:00 GMT+0000"
        try:
            return datetime.strptime(value, "%a %b %d %Y %H:%M:%S GMT%z")
        except ValueError:
            pass

        # ISO 8601 fallback
        try:
            return datetime.fromisoformat(value)
        except ValueError as e:
            error_msg = f"Invalid timestamp format: {value}"
            raise ValueError(error_msg) from e


class ApiMentionKeyword(BaseModel):
    """A monitored keyword on an :class:`ApiMention` (id + display text).

    Both fields are nullable: a single malformed/null nested keyword must not
    fail validation of the whole mention (consistent with the lenient
    :class:`ApiMention`). ``api_mention_to_row`` already drops empty keywords.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: int | None = None
    keyword: str | None = None


class ApiMention(BaseModel):
    """One mention from the Octolens v2 REST API (``POST /api/v2/mentions``).

    Distinct from :class:`Mention` (the *inbound webhook* payload): the API uses
    camelCase, carries a real relevance verdict (``relevanceScore`` 0=high /
    1=medium / 2=low), and spans a superset of source platforms. Every field is
    optional with a safe default and ``extra="allow"`` is set, so a single
    malformed or newly-added field never aborts a multi-page bulk pull — the
    backfill validates the essentials (url/source/source_id) downstream and skips
    rows that don't map. ``src.octolens.backfill.api_mention_to_row`` converts an
    instance into the CSV-shaped row the rest of the backfill consumes.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # Identity fields are nullable (not just defaulted) so a null in the payload
    # is absorbed rather than raising ValidationError — the backfill filters rows
    # with an empty url/source/source_id downstream and counts them, instead of
    # silently dropping the record at parse time.
    id: int | None = None
    source_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("sourceId", "source_id"),
    )
    url: str | None = None
    title: str | None = None
    body: str | None = None
    source: str | None = None
    timestamp: str | None = None
    author: str | None = None
    author_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("authorName", "author_name"),
    )
    author_avatar: str | None = Field(
        default=None,
        validation_alias=AliasChoices("authorAvatar", "author_avatar"),
    )
    author_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("authorUrl", "author_url"),
    )
    relevance: str | None = None
    relevance_comment: str | None = Field(
        default=None,
        validation_alias=AliasChoices("relevanceComment", "relevance_comment"),
    )
    relevance_score: float | None = Field(
        default=None,
        validation_alias=AliasChoices("relevanceScore", "relevance_score"),
    )
    sentiment: str | None = None
    language: str | None = None
    tags: list[Any] = Field(default_factory=list)
    keywords: list[ApiMentionKeyword] = Field(default_factory=list)
    image_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("imageUrl", "image_url"),
    )


class Webhook(BaseModel):
    model_config = ConfigDict(extra="allow")

    action: str = "mention_created"
    data: Mention

    @model_validator(mode="before")
    @classmethod
    def _unwrap_body(cls, data: Any) -> Any:
        if isinstance(data, dict) and "body" in data and "action" not in data:
            body = data["body"]
            if isinstance(body, (bytes, bytearray, memoryview)):
                return orjson.loads(body)
            if isinstance(body, str):
                return orjson.loads(body)
        return data
